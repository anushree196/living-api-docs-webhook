# Living API Docs — Webhook Service

Always-on Flask server deployed on Railway.
Receives GitHub push webhooks → generates docs → sends email alerts.
Works even when Streamlit is sleeping.

## Deploy to Railway (Free)

### Step 1 — Create Railway account
Go to railway.app → Sign up with GitHub (free)

### Step 2 — Create new project
- Click "New Project"
- Select "Deploy from GitHub repo"
- Create a NEW repo called `living-api-docs-webhook`
- Push this webhook-service folder to it

### Step 3 — Set environment variables in Railway
Go to your Railway project → Variables → Add these:

```
WEBHOOK_SECRET=<generate below>
GROQ_API_KEY=<same as your main app>
GITHUB_TOKEN=<same as your main app>
SENDGRID_API_KEY=<same as your main app>
FROM_EMAIL=<your verified sender>
STREAMLIT_APP_URL=<your streamlit cloud URL>
```

Generate your WEBHOOK_SECRET:
```bash
python -c "import secrets; print(secrets.token_hex(32))"
```
Copy that output as your WEBHOOK_SECRET — save it, you need it for GitHub too.

### Step 4 — Get your Railway URL
After deploy, Railway gives you a URL like:
`https://living-api-docs-webhook-production.up.railway.app`

Test it: visit that URL in browser → should see {"status": "ok"}

### Step 5 — Add webhook to each GitHub repo you monitor
For EACH repo you want to monitor:

1. Go to: github.com/YOUR_USERNAME/YOUR_REPO/settings/hooks
2. Click "Add webhook"
3. Fill in:
   - Payload URL: `https://your-railway-url.railway.app/webhook`
   - Content type: `application/json`
   - Secret: paste your WEBHOOK_SECRET
   - Events: Just the push event
4. Click "Add webhook"
5. GitHub sends a ping → you should see a green tick ✅

### Step 6 — Add WEBHOOK_SECRET to your main app .env
```
WEBHOOK_SECRET=same_secret_as_railway
```

## How It Works

```
Dev pushes code
      ↓
GitHub POST → https://your-railway.railway.app/webhook
      ↓
Flask verifies HMAC signature (security)
      ↓
Checks changed files — API related?
      ↓
Runs: clone → parse → LLM generate → save draft
      ↓
SendGrid email → user with magic link
      ↓
User clicks → Streamlit wakes → sees updated docs
```

## Local Testing

```bash
pip install -r requirements.txt
cp .env.example .env  # fill in your values
python webhook_server.py
```

Test webhook locally with ngrok:
```bash
ngrok http 5000
# Copy the https URL → use as GitHub webhook URL for testing
```

Test email:
```bash
curl -X POST http://localhost:5000/test-email \
  -H "Content-Type: application/json" \
  -d '{"email": "your@email.com"}'
```
