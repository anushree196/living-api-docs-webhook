"""
webhook_server.py
Always-on Flask service deployed on Railway.
Receives GitHub push webhooks, runs doc pipeline, sends email alerts.
This runs INDEPENDENTLY of Streamlit — so it works even when Streamlit is sleeping.
"""

import os
import hmac
import hashlib
import json
import threading
import requests
from flask import Flask, request, jsonify
from dotenv import load_dotenv

load_dotenv()

app = Flask(__name__)

WEBHOOK_SECRET = os.getenv("WEBHOOK_SECRET")
GROQ_API_KEY = os.getenv("GROQ_API_KEY")
GITHUB_TOKEN = os.getenv("GITHUB_TOKEN")
SENDGRID_API_KEY = os.getenv("SENDGRID_API_KEY")
FROM_EMAIL = os.getenv("FROM_EMAIL", "noreply@livingapidocs.com")
STREAMLIT_APP_URL = os.getenv("STREAMLIT_APP_URL", "https://your-app.streamlit.app")
DATABASE_URL = os.getenv("DATABASE_URL")  # Postgres URL for Railway


# ─────────────────────────────────────────────
# SECURITY — Verify GitHub webhook signature
# ─────────────────────────────────────────────

def verify_signature(payload_body: bytes, signature_header: str) -> bool:
    """Verify that the webhook came from GitHub using HMAC SHA256."""
    if not WEBHOOK_SECRET:
        print("[Webhook] WARNING: No WEBHOOK_SECRET set — skipping verification")
        return True

    if not signature_header or not signature_header.startswith("sha256="):
        return False

    expected = "sha256=" + hmac.new(
        WEBHOOK_SECRET.encode("utf-8"),
        payload_body,
        hashlib.sha256
    ).hexdigest()

    return hmac.compare_digest(expected, signature_header)


# ─────────────────────────────────────────────
# FILE CHANGE ANALYSIS
# ─────────────────────────────────────────────

API_FILE_PATTERNS = [
    "route", "router", "controller", "api", "view",
    "endpoint", "main.py", "app.py", "urls.py",
    ".java", "Controller.java", "Resource.java"
]

SKIP_PATTERNS = [
    "test_", "_test.", ".md", ".txt", "static/",
    "migration", ".css", ".html", "requirements.txt",
    "package.json", "README"
]


def is_api_file(filename: str) -> bool:
    """Check if a changed file likely contains API definitions."""
    filename_lower = filename.lower()
    if any(skip in filename_lower for skip in SKIP_PATTERNS):
        return False
    return any(pattern in filename_lower for pattern in API_FILE_PATTERNS)


def get_changed_api_files(commits: list) -> list:
    """Extract API-related changed files from push commits."""
    changed = set()
    for commit in commits:
        for f in commit.get("added", []) + commit.get("modified", []):
            if is_api_file(f):
                changed.add(f)
    return list(changed)


# ─────────────────────────────────────────────
# DATABASE — SQLite (local) or Postgres (Railway)
# ─────────────────────────────────────────────

def get_db_connection():
    """
    Get DB connection.
    Uses Postgres on Railway (DATABASE_URL set), SQLite locally.
    """
    if DATABASE_URL and DATABASE_URL.startswith("postgres"):
        import psycopg2
        import psycopg2.extras
        conn = psycopg2.connect(DATABASE_URL)
        return conn, "postgres"
    else:
        import sqlite3
        db_path = os.path.join(os.path.dirname(__file__), "..", "data", "living_api_docs.db")
        os.makedirs(os.path.dirname(db_path), exist_ok=True)
        conn = sqlite3.connect(db_path, check_same_thread=False)
        conn.row_factory = sqlite3.Row
        return conn, "sqlite"


def get_repo_owner(repo_url: str) -> dict:
    """Fetch repo + user email from DB."""
    try:
        conn, db_type = get_db_connection()
        if db_type == "postgres":
            cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
            cur.execute("SELECT * FROM repos WHERE repo_url = %s", (repo_url,))
        else:
            cur = conn.execute("SELECT * FROM repos WHERE repo_url = ?", (repo_url,))
        row = cur.fetchone()
        conn.close()
        return dict(row) if row else None
    except Exception as e:
        print(f"[Webhook] DB error: {e}")
        return None


def get_user_token(email: str) -> str:
    """Fetch user auth token from DB."""
    try:
        conn, db_type = get_db_connection()
        if db_type == "postgres":
            cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
            cur.execute("SELECT token FROM users WHERE email = %s", (email,))
        else:
            cur = conn.execute("SELECT token FROM users WHERE email = ?", (email,))
        row = cur.fetchone()
        conn.close()
        return dict(row).get("token") if row else None
    except Exception as e:
        print(f"[Webhook] DB error fetching token: {e}")
        return None


# ─────────────────────────────────────────────
# LLM DOC GENERATION — same as main app
# ─────────────────────────────────────────────

GROQ_URL = "https://api.groq.com/openai/v1/chat/completions"


def generate_doc_for_endpoint(method: str, route: str, raw_code: str,
                               framework: str = "unknown") -> str:
    """Generate documentation via Groq API directly."""
    prompt = (
        "Generate complete API documentation for this endpoint.\n\n"
        "Endpoint: " + method.upper() + " " + route + "\n"
        "Framework: " + framework + "\n\n"
        "Source Code:\n" + (raw_code or "")[:1500] + "\n\n"
        "Generate docs with: Overview, Parameters, Response, Code Examples (Python + JS), Notes.\n"
        "Output ONLY markdown."
    )

    try:
        response = requests.post(
            GROQ_URL,
            headers={
                "Authorization": "Bearer " + GROQ_API_KEY,
                "Content-Type": "application/json"
            },
            json={
                "model": "llama-3.3-70b-versatile",
                "messages": [
                    {"role": "system", "content": "You are an expert API documentation writer. Output ONLY markdown."},
                    {"role": "user", "content": prompt}
                ],
                "temperature": 0.3,
                "max_tokens": 2048
            },
            timeout=60
        )
        response.raise_for_status()
        return response.json()["choices"][0]["message"]["content"].strip()
    except Exception as e:
        print(f"[Webhook] LLM failed for {method} {route}: {e}")
        return None


# ─────────────────────────────────────────────
# EMAIL NOTIFICATION
# ─────────────────────────────────────────────

def send_update_email(to_email: str, repo_name: str,
                       changed_files: list, user_token: str):
    """Send email notification via SendGrid when docs are updated."""
    if not SENDGRID_API_KEY:
        print(f"[Webhook] No SendGrid key — skipping email to {to_email}")
        return

    review_url = f"{STREAMLIT_APP_URL}/?token={user_token}&page=review"

    files_html = "".join([f"<li><code>{f}</code></li>" for f in changed_files[:8]])

    html = f"""
    <div style="font-family:sans-serif;max-width:520px;margin:0 auto;padding:2rem;">
        <div style="background:linear-gradient(135deg,#0d1117,#161b22);
                    padding:2rem;border-radius:12px;margin-bottom:1.5rem;text-align:center;">
            <h1 style="color:#00d4aa;font-size:1.6rem;margin:0;">📄 Living API Docs</h1>
            <p style="color:#8b949e;margin-top:0.4rem;font-size:0.9rem;">
                Your API changed — docs auto-updated!
            </p>
        </div>

        <div style="background:#f6f8fa;border-radius:8px;padding:1.25rem;margin-bottom:1.5rem;
                    border-left:4px solid #00d4aa;">
            <p style="margin:0;font-weight:600;color:#24292e;font-size:1rem;">
                📁 {repo_name}
            </p>
            <p style="margin:0.5rem 0 0.25rem;color:#57606a;font-size:0.88rem;">
                Changed API files detected:
            </p>
            <ul style="margin:0.25rem 0;padding-left:1.25rem;color:#24292e;font-size:0.88rem;">
                {files_html}
            </ul>
        </div>

        <p style="color:#24292e;font-size:0.95rem;">
            New documentation drafts are ready for your review. Click below to
            review, edit if needed, and approve:
        </p>

        <div style="text-align:center;margin:1.5rem 0;">
            <a href="{review_url}"
               style="background:#00d4aa;color:#0d1117;padding:0.8rem 2.5rem;
                      border-radius:8px;text-decoration:none;font-weight:700;
                      font-size:1rem;display:inline-block;letter-spacing:0.02em;">
                ✏️ Review Updated Docs
            </a>
        </div>

        <p style="color:#8b949e;font-size:0.78rem;text-align:center;margin-top:1.5rem;">
            You're receiving this because you monitor <strong>{repo_name}</strong>
            on Living API Docs.<br>
            The link above signs you in automatically — no password needed.
        </p>
    </div>
    """

    try:
        response = requests.post(
            "https://api.sendgrid.com/v3/mail/send",
            headers={
                "Authorization": f"Bearer {SENDGRID_API_KEY}",
                "Content-Type": "application/json"
            },
            json={
                "personalizations": [{"to": [{"email": to_email}]}],
                "from": {"email": FROM_EMAIL, "name": "Living API Docs"},
                "subject": f"📄 [{repo_name}] API changed — docs auto-updated",
                "content": [{"type": "text/html", "value": html}]
            },
            timeout=15
        )
        if response.status_code in (200, 202):
            print(f"[Webhook] Email sent to {to_email}")
        else:
            print(f"[Webhook] Email failed: {response.status_code} {response.text[:100]}")
    except Exception as e:
        print(f"[Webhook] Email error: {e}")


# ─────────────────────────────────────────────
# BACKGROUND PIPELINE
# ─────────────────────────────────────────────

def run_pipeline_in_background(repo_url: str, repo_name: str,
                                changed_files: list, pusher_email: str = None):
    """
    Run in a background thread so webhook responds instantly.
    1. Clone/pull repo
    2. Parse changed endpoints
    3. Generate docs
    4. Save drafts to DB
    5. Send email
    """
    print(f"[Webhook] Background pipeline started for {repo_name}")

    try:
        # Import parsers from main app
        import sys
        sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

        from parser.detect_framework import detect_framework, clone_or_pull_repo
        from parser.fastapi_parser import parse_fastapi
        from parser.flask_parser import parse_flask
        from parser.django_parser import parse_django
        from parser.express_parser import parse_express
        from parser.springboot_parser import parse_springboot
        from llm.consistency_checker import check_consistency
        import storage.db as db

        # Clone/pull latest
        local_path = clone_or_pull_repo(repo_url)
        if not local_path:
            print(f"[Webhook] Failed to clone {repo_url}")
            return

        framework = detect_framework(local_path)
        if not framework:
            print(f"[Webhook] Could not detect framework")
            return

        parsers = {
            "fastapi": parse_fastapi, "flask": parse_flask,
            "django": parse_django, "express": parse_express,
            "springboot": parse_springboot
        }
        endpoints = parsers.get(framework, lambda x: [])(local_path)
        print(f"[Webhook] Found {len(endpoints)} endpoints, framework: {framework}")

        updated_endpoints = []
        for ep in endpoints:
            method = ep.get("method", "GET")
            route = ep.get("route", "/")
            raw_code = ep.get("raw_code", "")

            # Skip if pending draft already exists
            if db.has_pending_draft(repo_url, method, route):
                continue

            doc = generate_doc_for_endpoint(method, route, raw_code, framework)
            if not doc:
                continue

            warnings = check_consistency(doc, raw_code)
            db.save_draft(repo_url, method, route, doc, warnings)
            updated_endpoints.append({"method": method, "route": route})
            print(f"[Webhook] Draft saved: {method} {route}")

        # Send email if we have new drafts and know the user
        if updated_endpoints and pusher_email:
            token = get_user_token(pusher_email)
            if token:
                send_update_email(pusher_email, repo_name, changed_files, token)

        print(f"[Webhook] Pipeline complete — {len(updated_endpoints)} drafts saved")

    except Exception as e:
        print(f"[Webhook] Pipeline error: {e}")


# ─────────────────────────────────────────────
# FLASK ROUTES
# ─────────────────────────────────────────────

@app.route("/", methods=["GET"])
def health():
    """Health check endpoint."""
    return jsonify({
        "status": "ok",
        "service": "Living API Docs Webhook Server",
        "version": "1.0.0"
    })


@app.route("/webhook", methods=["POST"])
def github_webhook():
    """
    Main webhook endpoint — receives GitHub push events.
    GitHub sends this whenever code is pushed to any monitored repo.
    """
    # 1. Verify signature
    signature = request.headers.get("X-Hub-Signature-256", "")
    if not verify_signature(request.data, signature):
        print("[Webhook] Invalid signature — rejected")
        return jsonify({"error": "Invalid signature"}), 401

    # 2. Only handle push events
    event_type = request.headers.get("X-GitHub-Event", "")
    if event_type == "ping":
        return jsonify({"message": "pong — webhook connected!"}), 200

    if event_type != "push":
        return jsonify({"message": f"Ignoring event: {event_type}"}), 200

    # 3. Parse payload
    payload = request.get_json()
    if not payload:
        return jsonify({"error": "No payload"}), 400

    repo_name = payload.get("repository", {}).get("name", "unknown")
    repo_full = payload.get("repository", {}).get("full_name", "")
    repo_url = f"https://github.com/{repo_full}"
    commits = payload.get("commits", [])
    pusher_email = payload.get("pusher", {}).get("email", "")

    print(f"[Webhook] Push received: {repo_url} by {pusher_email}")

    # 4. Check if this repo is monitored
    repo_record = get_repo_owner(repo_url)
    if not repo_record:
        print(f"[Webhook] Repo not monitored: {repo_url}")
        return jsonify({"message": "Repo not monitored — ignoring"}), 200

    # Use registered email if pusher email not available
    if not pusher_email:
        pusher_email = repo_record.get("user_email", "")

    # 5. Check for API-related file changes
    changed_api_files = get_changed_api_files(commits)
    if not changed_api_files:
        print(f"[Webhook] No API files changed in {repo_url}")
        return jsonify({"message": "No API files changed"}), 200

    print(f"[Webhook] API files changed: {changed_api_files}")

    # 6. Run pipeline in background — respond immediately to GitHub
    thread = threading.Thread(
        target=run_pipeline_in_background,
        args=(repo_url, repo_name, changed_api_files, pusher_email),
        daemon=True
    )
    thread.start()

    return jsonify({
        "message": "Pipeline triggered",
        "repo": repo_url,
        "changed_files": changed_api_files,
        "notifying": pusher_email
    }), 200


@app.route("/test-email", methods=["POST"])
def test_email():
    """Test endpoint to verify SendGrid is working."""
    data = request.get_json()
    email = data.get("email")
    if not email:
        return jsonify({"error": "email required"}), 400

    send_update_email(
        to_email=email,
        repo_name="test-repo",
        changed_files=["main.py", "routes.py"],
        user_token="test-token-123"
    )
    return jsonify({"message": f"Test email sent to {email}"})


if __name__ == "__main__":
    port = int(os.getenv("PORT", 5000))
    print(f"[Webhook] Server starting on port {port}")
    app.run(host="0.0.0.0", port=port, debug=False)
