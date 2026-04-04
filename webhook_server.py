"""
webhook_server.py - Final version
Key fix: Instead of trying to run the full pipeline on Railway
(which has no shared DB), Railway now calls back to the Streamlit
app's /notify endpoint which has the real database.
Railway's job: receive GitHub push → verify → call Streamlit → done.
"""

import os
import hmac
import hashlib
import json
import secrets
import threading
import requests
from flask import Flask, request, jsonify, redirect
from dotenv import load_dotenv

load_dotenv()

app = Flask(__name__)

WEBHOOK_SECRET       = os.getenv("WEBHOOK_SECRET", "")
SENDGRID_API_KEY     = os.getenv("SENDGRID_API_KEY", "")
FROM_EMAIL           = os.getenv("FROM_EMAIL", "")
STREAMLIT_APP_URL    = os.getenv("STREAMLIT_APP_URL", "https://living-api-docs-generator.streamlit.app")
RAILWAY_URL          = os.getenv("RAILWAY_URL", "")
GITHUB_CLIENT_ID     = os.getenv("GITHUB_CLIENT_ID", "")
GITHUB_CLIENT_SECRET = os.getenv("GITHUB_CLIENT_SECRET", "")
NOTIFY_SECRET        = os.getenv("NOTIFY_SECRET", "living-api-docs-notify-2024")

# In-memory OAuth state store
_oauth_states = {}


# ─────────────────────────────────────────────
# SECURITY
# ─────────────────────────────────────────────

def verify_signature(payload_body: bytes, sig_header: str) -> bool:
    if not WEBHOOK_SECRET:
        return True
    if not sig_header or not sig_header.startswith("sha256="):
        return False
    expected = "sha256=" + hmac.new(
        WEBHOOK_SECRET.encode(), payload_body, hashlib.sha256
    ).hexdigest()
    return hmac.compare_digest(expected, sig_header)


# ─────────────────────────────────────────────
# FILE ANALYSIS
# ─────────────────────────────────────────────

API_PATTERNS  = ["route", "router", "controller", "api", "view",
                 "endpoint", "main.py", "app.py", "urls.py",
                 "Controller.java", "Resource.java"]
SKIP_PATTERNS = ["test_", "_test.", ".md", ".txt", "static/",
                 "migration", ".css", ".html", "requirements.txt",
                 "package.json", "README"]

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
# NOTIFY STREAMLIT — The Key Fix
# ─────────────────────────────────────────────

def notify_streamlit(repo_url: str, changed_files: list, pusher_email: str):
    """
    Wake Streamlit and trigger scan via URL params.
    Visiting the Streamlit URL with ?action=notify&repo_url=... 
    triggers the scan on the machine that HAS the database.
    Also sends email directly from Railway if SendGrid configured.
    """
    import urllib.parse
    notify_url = (
        f"{STREAMLIT_APP_URL}?"
        f"action=notify"
        f"&repo_url={urllib.parse.quote(repo_url)}"
        f"&secret={NOTIFY_SECRET}"
    )
    try:
        # Wake Streamlit (GET request — works even if sleeping, wakes it up)
        resp = requests.get(notify_url, timeout=15)
        print(f"[Railway] Streamlit pinged: {resp.status_code}")
    except Exception as e:
        print(f"[Railway] Could not reach Streamlit: {e}")

    # Also send email directly from Railway
    # We need to store user email somewhere both can access
    # Simple solution: Railway gets pusher_email from GitHub payload
    if pusher_email and SENDGRID_API_KEY:
        repo_name = repo_url.split("/")[-1]
        send_email_from_railway(pusher_email, repo_name, changed_files)


def send_email_from_railway(to_email: str, repo_name: str, changed_files: list):
    """Send notification email directly from Railway — no DB needed."""
    if not SENDGRID_API_KEY or not FROM_EMAIL:
        print("[Railway] SendGrid not configured — skipping email")
        return

    review_url = f"{STREAMLIT_APP_URL}"
    files_html = "".join(f"<li><code>{f}</code></li>" for f in changed_files[:8])

    html = f"""
    <div style="font-family:sans-serif;max-width:520px;margin:0 auto;padding:2rem;">
        <div style="background:linear-gradient(135deg,#0d1117,#161b22);padding:2rem;
                    border-radius:12px;margin-bottom:1.5rem;text-align:center;">
            <h1 style="color:#00d4aa;font-size:1.6rem;margin:0;">📄 Living API Docs</h1>
            <p style="color:#8b949e;margin-top:0.4rem;">Your API changed — new docs ready for review!</p>
        </div>
        <div style="background:#f6f8fa;border-radius:8px;padding:1.25rem;
                    margin-bottom:1.5rem;border-left:4px solid #00d4aa;">
            <p style="margin:0;font-weight:600;">📁 {repo_name}</p>
            <p style="margin:0.5rem 0 0.25rem;color:#57606a;font-size:0.88rem;">Changed API files:</p>
            <ul style="margin:0.25rem 0;padding-left:1.25rem;font-size:0.88rem;">
                {files_html}
            </ul>
        </div>
        <div style="text-align:center;margin:1.5rem 0;">
            <a href="{review_url}" style="background:#00d4aa;color:#0d1117;padding:0.8rem 2.5rem;
               border-radius:8px;text-decoration:none;font-weight:700;font-size:1rem;
               display:inline-block;">✏️ Review Updated Docs</a>
        </div>
        <p style="color:#8b949e;font-size:0.78rem;text-align:center;">
            Go to Review Drafts page to approve your updated documentation.
        </p>
    </div>"""

    try:
        resp = requests.post(
            "https://api.sendgrid.com/v3/mail/send",
            headers={"Authorization": f"Bearer {SENDGRID_API_KEY}",
                     "Content-Type": "application/json"},
            json={"personalizations": [{"to": [{"email": to_email}]}],
                  "from": {"email": FROM_EMAIL, "name": "Living API Docs"},
                  "subject": f"📄 [{repo_name}] API changed — docs ready for review",
                  "content": [{"type": "text/html", "value": html}]},
            timeout=15
        )
        if resp.status_code in (200, 202):
            print(f"[Railway] Email sent to {to_email}")
        else:
            print(f"[Railway] Email failed: {resp.status_code} {resp.text[:80]}")
    except Exception as e:
        print(f"[Railway] Email error: {e}")


# ─────────────────────────────────────────────
# GITHUB OAUTH
# ─────────────────────────────────────────────

def install_webhook_on_repo(user_access_token: str, repo_full_name: str) -> bool:
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
    if resp.status_code == 201:
        print(f"[OAuth] Webhook installed on {repo_full_name}")
        return True
    if resp.status_code == 422:
        print(f"[OAuth] Webhook already exists on {repo_full_name}")
        return True
    print(f"[OAuth] Failed: {resp.status_code} {resp.text[:100]}")
    return False


@app.route("/auth/start")
def auth_start():
    email = request.args.get("email", "")
    repo  = request.args.get("repo", "")
    if not email:
        return jsonify({"error": "email required"}), 400

    state = secrets.token_urlsafe(16)
    _oauth_states[state] = {"email": email, "repo": repo}

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
    code  = request.args.get("code", "")
    state = request.args.get("state", "")

    if state not in _oauth_states:
        return "Invalid state. Please try again.", 400

    state_data = _oauth_states.pop(state)
    email = state_data.get("email", "")
    repo  = state_data.get("repo", "")

    if not code:
        return "GitHub authorization failed.", 400

    token_resp = requests.post(
        "https://github.com/login/oauth/access_token",
        headers={"Accept": "application/json"},
        json={"client_id": GITHUB_CLIENT_ID,
              "client_secret": GITHUB_CLIENT_SECRET,
              "code": code},
        timeout=15
    )

    token_data = token_resp.json()
    user_access_token = token_data.get("access_token", "")

    if not user_access_token:
        error = token_data.get("error_description", "Unknown error")
        return f"GitHub OAuth error: {error}", 400

    webhook_installed = False
    if repo:
        repo_full = repo.replace("https://github.com/", "").rstrip("/")
        webhook_installed = install_webhook_on_repo(user_access_token, repo_full)

    status = "webhook_installed" if webhook_installed else "authorized"
    return redirect(f"{STREAMLIT_APP_URL}/?oauth={status}&repo={repo}")


# ─────────────────────────────────────────────
# FLASK ROUTES
# ─────────────────────────────────────────────

@app.route("/", methods=["GET"])
def health():
    return jsonify({
        "status": "ok",
        "service": "Living API Docs Webhook Server",
        "version": "2.0.0",
        "oauth_configured": bool(GITHUB_CLIENT_ID)
    })


@app.route("/webhook", methods=["POST"])
def github_webhook():
    # Handle ping FIRST before signature check
    event_type = request.headers.get("X-GitHub-Event", "")
    if event_type == "ping":
        print("[Webhook] Ping received!")
        return jsonify({"message": "pong — webhook connected!"}), 200

    # Verify signature
    signature = request.headers.get("X-Hub-Signature-256", "")
    if not verify_signature(request.data, signature):
        print("[Webhook] Invalid signature — rejected")
        return jsonify({"error": "Invalid signature"}), 401

    if event_type != "push":
        return jsonify({"message": f"Ignoring: {event_type}"}), 200

    payload = request.get_json()
    if not payload:
        return jsonify({"error": "No payload"}), 400

    repo_full    = payload.get("repository", {}).get("full_name", "")
    repo_url     = f"https://github.com/{repo_full}"
    commits      = payload.get("commits", [])
    pusher_email = payload.get("pusher", {}).get("email", "")

    print(f"[Webhook] Push: {repo_url} by {pusher_email}")

    changed_api_files = get_changed_api_files(commits)
    if not changed_api_files:
        print("[Webhook] No API files changed")
        return jsonify({"message": "No API files changed"}), 200

    print(f"[Webhook] API files changed: {changed_api_files}")

    # Notify Streamlit in background — don't block GitHub response
    thread = threading.Thread(
        target=notify_streamlit,
        args=(repo_url, changed_api_files, pusher_email),
        daemon=True
    )
    thread.start()

    return jsonify({
        "message": "Change detected — Streamlit notified",
        "repo": repo_url,
        "changed_files": changed_api_files
    }), 200


if __name__ == "__main__":
    port = int(os.getenv("PORT", 5000))
    print(f"[Server] Starting on port {port}")
    app.run(host="0.0.0.0", port=port, debug=False)
