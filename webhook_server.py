"""
webhook_server.py
Always-on Flask service on Railway.
Handles:
1. GitHub push webhooks → generate docs → send email
2. GitHub OAuth flow → auto-install webhooks on user repos
3. Health check endpoint
"""

import os
import hmac
import hashlib
import json
import threading
import requests
from flask import Flask, request, jsonify, redirect
from dotenv import load_dotenv

load_dotenv()

app = Flask(__name__)

# ── Environment Variables ──
WEBHOOK_SECRET      = os.getenv("WEBHOOK_SECRET", "")
GROQ_API_KEY        = os.getenv("GROQ_API_KEY", "")
GITHUB_TOKEN        = os.getenv("GITHUB_TOKEN", "")
SENDGRID_API_KEY    = os.getenv("SENDGRID_API_KEY", "")
FROM_EMAIL          = os.getenv("FROM_EMAIL", "")
STREAMLIT_APP_URL   = os.getenv("STREAMLIT_APP_URL", "https://living-api-docs-generator.streamlit.app")
RAILWAY_URL         = os.getenv("RAILWAY_URL", "")
GITHUB_CLIENT_ID    = os.getenv("GITHUB_CLIENT_ID", "")
GITHUB_CLIENT_SECRET= os.getenv("GITHUB_CLIENT_SECRET", "")

GROQ_URL = "https://api.groq.com/openai/v1/chat/completions"

# ── In-memory store for OAuth state tokens (simple, sufficient for demo) ──
# Maps state_token -> user_email so we know who authorized
_oauth_states = {}


# ─────────────────────────────────────────────
# SECURITY
# ─────────────────────────────────────────────

def verify_signature(payload_body: bytes, signature_header: str) -> bool:
    """Verify GitHub webhook signature using HMAC SHA256."""
    if not WEBHOOK_SECRET:
        print("[Webhook] WARNING: No WEBHOOK_SECRET set")
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

API_PATTERNS  = ["route", "router", "controller", "api", "view", "endpoint",
                 "main.py", "app.py", "urls.py", "Controller.java", "Resource.java"]
SKIP_PATTERNS = ["test_", "_test.", ".md", ".txt", "static/", "migration",
                 ".css", ".html", "requirements.txt", "package.json", "README"]

def is_api_file(filename: str) -> bool:
    fl = filename.lower()
    if any(s in fl for s in SKIP_PATTERNS):
        return False
    return any(p in fl for p in API_PATTERNS)

def get_changed_api_files(commits: list) -> list:
    changed = set()
    for commit in commits:
        for f in commit.get("added", []) + commit.get("modified", []):
            if is_api_file(f):
                changed.add(f)
    return list(changed)


# ─────────────────────────────────────────────
# DATABASE HELPERS
# ─────────────────────────────────────────────

def get_db():
    """Get SQLite connection to shared DB."""
    import sqlite3, os
    db_path = os.path.join(os.path.dirname(__file__), "data", "living_api_docs.db")
    os.makedirs(os.path.dirname(db_path), exist_ok=True)
    conn = sqlite3.connect(db_path, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    return conn

def get_repo_record(repo_url: str) -> dict:
    try:
        conn = get_db()
        row = conn.execute("SELECT * FROM repos WHERE repo_url = ?", (repo_url,)).fetchone()
        conn.close()
        return dict(row) if row else None
    except Exception as e:
        print(f"[DB] get_repo error: {e}")
        return None

def get_user_token(email: str) -> str:
    try:
        conn = get_db()
        row = conn.execute("SELECT token FROM users WHERE email = ?", (email,)).fetchone()
        conn.close()
        return dict(row).get("token") if row else None
    except Exception as e:
        print(f"[DB] get_user_token error: {e}")
        return None

def save_draft_to_db(repo_url, method, route, doc, warnings):
    try:
        import json
        conn = get_db()
        conn.execute("""
            INSERT INTO drafts (repo_url, method, route, generated_doc, consistency_warnings)
            VALUES (?, ?, ?, ?, ?)
        """, (repo_url, method.upper(), route, doc, json.dumps(warnings or [])))
        conn.commit()
        conn.close()
    except Exception as e:
        print(f"[DB] save_draft error: {e}")

def has_pending_draft(repo_url, method, route) -> bool:
    try:
        conn = get_db()
        row = conn.execute("""
            SELECT id FROM drafts WHERE repo_url=? AND method=? AND route=? AND status='pending'
        """, (repo_url, method.upper(), route)).fetchone()
        conn.close()
        return row is not None
    except:
        return False


# ─────────────────────────────────────────────
# LLM DOC GENERATION
# ─────────────────────────────────────────────

def generate_doc_for_endpoint(method: str, route: str,
                               raw_code: str, framework: str = "unknown") -> str:
    prompt = (
        f"Generate complete API documentation for this endpoint.\n\n"
        f"Endpoint: {method.upper()} {route}\n"
        f"Framework: {framework}\n\n"
        f"Source Code:\n{(raw_code or '')[:1500]}\n\n"
        "Generate docs with: # METHOD ROUTE\n"
        "## Overview\n## Real-World Use Case\n## Authentication\n"
        "## Parameters\n## Response\n## Code Examples (Python + JavaScript)\n## Notes\n\n"
        "Output ONLY markdown."
    )
    try:
        resp = requests.post(
            GROQ_URL,
            headers={"Authorization": f"Bearer {GROQ_API_KEY}", "Content-Type": "application/json"},
            json={"model": "llama-3.3-70b-versatile",
                  "messages": [
                      {"role": "system", "content": "You are an expert API documentation writer. Output ONLY markdown."},
                      {"role": "user", "content": prompt}
                  ],
                  "temperature": 0.3, "max_tokens": 2048},
            timeout=60
        )
        resp.raise_for_status()
        return resp.json()["choices"][0]["message"]["content"].strip()
    except Exception as e:
        print(f"[LLM] Failed for {method} {route}: {e}")
        return None


# ─────────────────────────────────────────────
# EMAIL
# ─────────────────────────────────────────────

def send_update_email(to_email: str, repo_name: str,
                       changed_files: list, user_token: str):
    if not SENDGRID_API_KEY:
        print(f"[Email] No SendGrid key — skipping")
        return
    review_url = f"{STREAMLIT_APP_URL}/?token={user_token}&page=review"
    files_html = "".join(f"<li><code>{f}</code></li>" for f in changed_files[:8])
    html = f"""
    <div style="font-family:sans-serif;max-width:520px;margin:0 auto;padding:2rem;">
        <div style="background:linear-gradient(135deg,#0d1117,#161b22);padding:2rem;
                    border-radius:12px;margin-bottom:1.5rem;text-align:center;">
            <h1 style="color:#00d4aa;font-size:1.6rem;margin:0;">📄 Living API Docs</h1>
            <p style="color:#8b949e;margin-top:0.4rem;">Your API changed — docs auto-updated!</p>
        </div>
        <div style="background:#f6f8fa;border-radius:8px;padding:1.25rem;
                    margin-bottom:1.5rem;border-left:4px solid #00d4aa;">
            <p style="margin:0;font-weight:600;color:#24292e;">📁 {repo_name}</p>
            <p style="margin:0.5rem 0 0.25rem;color:#57606a;font-size:0.88rem;">Changed API files:</p>
            <ul style="margin:0.25rem 0;padding-left:1.25rem;color:#24292e;font-size:0.88rem;">
                {files_html}
            </ul>
        </div>
        <div style="text-align:center;margin:1.5rem 0;">
            <a href="{review_url}" style="background:#00d4aa;color:#0d1117;padding:0.8rem 2.5rem;
               border-radius:8px;text-decoration:none;font-weight:700;font-size:1rem;
               display:inline-block;">✏️ Review Updated Docs</a>
        </div>
        <p style="color:#8b949e;font-size:0.78rem;text-align:center;">
            Link signs you in automatically — no password needed.
        </p>
    </div>"""
    try:
        resp = requests.post(
            "https://api.sendgrid.com/v3/mail/send",
            headers={"Authorization": f"Bearer {SENDGRID_API_KEY}",
                     "Content-Type": "application/json"},
            json={"personalizations": [{"to": [{"email": to_email}]}],
                  "from": {"email": FROM_EMAIL, "name": "Living API Docs"},
                  "subject": f"📄 [{repo_name}] API changed — docs auto-updated",
                  "content": [{"type": "text/html", "value": html}]},
            timeout=15
        )
        if resp.status_code in (200, 202):
            print(f"[Email] Sent to {to_email}")
        else:
            print(f"[Email] Failed: {resp.status_code}")
    except Exception as e:
        print(f"[Email] Error: {e}")


# ─────────────────────────────────────────────
# GITHUB OAUTH — Auto-install webhooks
# ─────────────────────────────────────────────

def install_webhook_on_repo(user_access_token: str, repo_full_name: str) -> bool:
    """
    Programmatically install our webhook on a user's repo.
    repo_full_name = "username/reponame"
    """
    webhook_url = f"{RAILWAY_URL}/webhook"
    resp = requests.post(
        f"https://api.github.com/repos/{repo_full_name}/hooks",
        headers={"Authorization": f"token {user_access_token}",
                 "Accept": "application/vnd.github.v3+json"},
        json={"name": "web", "active": True, "events": ["push"],
              "config": {"url": webhook_url,
                         "content_type": "application/json",
                         "secret": WEBHOOK_SECRET}},
        timeout=15
    )
    success = resp.status_code == 201
    if success:
        print(f"[OAuth] Webhook installed on {repo_full_name}")
    else:
        # 422 means webhook already exists — that's fine
        if resp.status_code == 422:
            print(f"[OAuth] Webhook already exists on {repo_full_name}")
            return True
        print(f"[OAuth] Failed to install webhook: {resp.status_code} {resp.text[:100]}")
    return success


@app.route("/auth/start")
def auth_start():
    """
    Step 1 of OAuth — redirect user to GitHub authorization page.
    Called when user clicks 'Connect GitHub' in Streamlit.
    URL params: email (the user's registered email)
    """
    email = request.args.get("email", "")
    repo = request.args.get("repo", "")

    if not email:
        return jsonify({"error": "email parameter required"}), 400

    # Generate state token to prevent CSRF
    import secrets
    state = secrets.token_urlsafe(16)
    _oauth_states[state] = {"email": email, "repo": repo}

    # Redirect to GitHub OAuth
    github_auth_url = (
        f"https://github.com/login/oauth/authorize"
        f"?client_id={GITHUB_CLIENT_ID}"
        f"&scope=write:repo_hook,repo"
        f"&state={state}"
        f"&redirect_uri={RAILWAY_URL}/auth/callback"
    )
    return redirect(github_auth_url)


@app.route("/auth/callback")
def auth_callback():
    """
    Step 2 of OAuth — GitHub redirects here after user approves.
    Exchange the code for an access token, then install webhook.
    """
    code  = request.args.get("code", "")
    state = request.args.get("state", "")

    # Verify state to prevent CSRF attacks
    if state not in _oauth_states:
        return "Invalid state parameter. Please try again.", 400

    state_data = _oauth_states.pop(state)
    email = state_data.get("email", "")
    repo  = state_data.get("repo", "")

    if not code:
        return "GitHub authorization failed — no code received.", 400

    # Exchange code for access token
    token_resp = requests.post(
        "https://github.com/login/oauth/access_token",
        headers={"Accept": "application/json"},
        json={"client_id": GITHUB_CLIENT_ID,
              "client_secret": GITHUB_CLIENT_SECRET,
              "code": code},
        timeout=15
    )

    if token_resp.status_code != 200:
        return "Failed to get access token from GitHub.", 500

    token_data = token_resp.json()
    user_access_token = token_data.get("access_token", "")

    if not user_access_token:
        error = token_data.get("error_description", "Unknown error")
        return f"GitHub OAuth error: {error}", 400

    # Install webhook on the repo if provided
    webhook_installed = False
    if repo:
        # repo is full name like "username/reponame"
        repo_full = repo.replace("https://github.com/", "").rstrip("/")
        webhook_installed = install_webhook_on_repo(user_access_token, repo_full)

    # Redirect back to Streamlit with success message
    status = "webhook_installed" if webhook_installed else "authorized"
    return redirect(f"{STREAMLIT_APP_URL}/?oauth={status}&repo={repo}")


# ─────────────────────────────────────────────
# BACKGROUND PIPELINE
# ─────────────────────────────────────────────

def run_pipeline(repo_url: str, repo_name: str,
                 changed_files: list, user_email: str):
    """Run in background thread — generate docs for changed endpoints."""
    print(f"[Pipeline] Starting for {repo_name}")
    try:
        import sys
        sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

        from parser.detect_framework import detect_framework, clone_or_pull_repo
        from parser.fastapi_parser   import parse_fastapi
        from parser.flask_parser     import parse_flask
        from parser.django_parser    import parse_django
        from parser.express_parser   import parse_express
        from parser.springboot_parser import parse_springboot

        local_path = clone_or_pull_repo(repo_url)
        if not local_path:
            print(f"[Pipeline] Clone failed for {repo_url}")
            return

        framework = detect_framework(local_path)
        if not framework:
            print(f"[Pipeline] Framework not detected")
            return

        parsers = {"fastapi": parse_fastapi, "flask": parse_flask,
                   "django": parse_django, "express": parse_express,
                   "springboot": parse_springboot}
        endpoints = parsers.get(framework, lambda x: [])(local_path)
        print(f"[Pipeline] {len(endpoints)} endpoints found, framework: {framework}")

        updated = []
        for ep in endpoints:
            method   = ep.get("method", "GET")
            route    = ep.get("route", "/")
            raw_code = ep.get("raw_code", "")

            if has_pending_draft(repo_url, method, route):
                print(f"[Pipeline] Skipping {method} {route} — pending draft exists")
                continue

            doc = generate_doc_for_endpoint(method, route, raw_code, framework)
            if not doc:
                continue

            save_draft_to_db(repo_url, method, route, doc, [])
            updated.append({"method": method, "route": route})
            print(f"[Pipeline] Draft saved: {method} {route}")

        if updated and user_email:
            token = get_user_token(user_email)
            if token:
                send_update_email(user_email, repo_name, changed_files, token)

        print(f"[Pipeline] Complete — {len(updated)} drafts saved")

    except Exception as e:
        print(f"[Pipeline] Error: {e}")


# ─────────────────────────────────────────────
# FLASK ROUTES
# ─────────────────────────────────────────────

@app.route("/", methods=["GET"])
def health():
    return jsonify({"status": "ok",
                    "service": "Living API Docs Webhook Server",
                    "version": "1.0.0",
                    "oauth_configured": bool(GITHUB_CLIENT_ID)})


@app.route("/webhook", methods=["POST"])
def github_webhook():
    # 1. Handle ping FIRST — before signature check
    event_type = request.headers.get("X-GitHub-Event", "")
    if event_type == "ping":
        print("[Webhook] Ping received — connection confirmed!")
        return jsonify({"message": "pong — webhook connected!"}), 200

    # 2. Verify signature
    signature = request.headers.get("X-Hub-Signature-256", "")
    if not verify_signature(request.data, signature):
        print("[Webhook] Invalid signature — rejected")
        return jsonify({"error": "Invalid signature"}), 401

    # 3. Only handle push events
    if event_type != "push":
        return jsonify({"message": f"Ignoring event: {event_type}"}), 200

    # 4. Parse payload
    payload = request.get_json()
    if not payload:
        return jsonify({"error": "No payload"}), 400

    repo_name  = payload.get("repository", {}).get("name", "unknown")
    repo_full  = payload.get("repository", {}).get("full_name", "")
    repo_url   = f"https://github.com/{repo_full}"
    commits    = payload.get("commits", [])
    pusher_email = payload.get("pusher", {}).get("email", "")

    print(f"[Webhook] Push received: {repo_url} by {pusher_email}")

    # 5. Check if repo is monitored
    repo_record = get_repo_record(repo_url)
    if not repo_record:
        print(f"[Webhook] Repo not monitored: {repo_url}")
        return jsonify({"message": "Repo not monitored"}), 200

    if not pusher_email:
        pusher_email = repo_record.get("user_email", "")

    # 6. Check for API file changes
    changed_api_files = get_changed_api_files(commits)
    if not changed_api_files:
        print(f"[Webhook] No API files changed")
        return jsonify({"message": "No API files changed"}), 200

    print(f"[Webhook] API files changed: {changed_api_files}")

    # 7. Run pipeline in background — respond to GitHub immediately
    thread = threading.Thread(
        target=run_pipeline,
        args=(repo_url, repo_name, changed_api_files, pusher_email),
        daemon=True
    )
    thread.start()

    return jsonify({"message": "Pipeline triggered",
                    "repo": repo_url,
                    "changed_files": changed_api_files}), 200


@app.route("/test-email", methods=["POST"])
def test_email():
    """Test endpoint to verify SendGrid works."""
    data  = request.get_json() or {}
    email = data.get("email", "")
    if not email:
        return jsonify({"error": "email required"}), 400
    send_update_email(email, "test-repo", ["main.py", "routes.py"], "test-token-123")
    return jsonify({"message": f"Test email sent to {email}"})


if __name__ == "__main__":
    port = int(os.getenv("PORT", 5000))
    print(f"[Server] Starting on port {port}")
    app.run(host="0.0.0.0", port=port, debug=False)
