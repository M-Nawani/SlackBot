# Slack Knowledge Bot Setup Guide

## What you need
- A Slack workspace where you're an admin
- An Anthropic API key → https://console.anthropic.com
- A free Railway or Render account to host the server

---

## Step 1 — Create the Slack App

1. Go to https://api.slack.com/apps → **Create New App** → **From scratch**
2. Name it (e.g. "AskBot") and pick your workspace

### Bot Token Scopes (OAuth & Permissions → Scopes → Bot Token)
Add these:
- `app_mentions:read` — so it sees when someone @mentions it
- `chat:write` — so it can post replies
- `channels:read` — to list channels

### User Token Scopes (OAuth & Permissions → Scopes → User Token)
Add these (needed for Slack search):
- `search:read` — search all public messages

3. Click **Install to Workspace** and copy both tokens:
   - **Bot User OAuth Token** → `SLACK_BOT_TOKEN`
   - **User OAuth Token** → `SLACK_USER_TOKEN`

4. Go to **Basic Information** → copy **Signing Secret** → `SLACK_SIGNING_SECRET`

---

## Step 2 — Deploy the server

### Option A: Railway (easiest)
1. Push this folder to a GitHub repo
2. Go to https://railway.app → New Project → Deploy from GitHub
3. Add your environment variables (from `.env.example`) in the Railway dashboard
4. Railway gives you a public URL like `https://your-app.up.railway.app`

### Option B: Render
1. Push to GitHub, go to https://render.com → New Web Service
2. Set **Start Command** to `python app.py`
3. Add environment variables, deploy

---

## Step 3 — Wire up the Slack event subscription

1. In your Slack App settings → **Event Subscriptions** → toggle On
2. Set **Request URL** to: `https://your-deployed-url.com/slack/events`
   - Slack will send a verification challenge — your server handles this automatically
3. Under **Subscribe to bot events**, add: `app_mention`
4. Save changes

---

## Step 4 — Add the bot to your workspace

1. In Slack, go to any channel → **Add apps** → find your bot → Add
2. Or invite it: `/invite @AskBot`

---

## Step 5 — Test it

In any channel where the bot is present, type:
```
@AskBot why does the CSV export fail for enterprise customers?
```

The bot will search your Slack history and reply in the thread.

---

## Running locally (for development)

```bash
pip install -r requirements.txt
cp .env.example .env   # fill in your values
python app.py
```

Use [ngrok](https://ngrok.com) to expose your local server:
```bash
ngrok http 3000
```
Then set the ngrok URL as your Slack event subscription URL temporarily.
