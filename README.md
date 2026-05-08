# Slack Knowledge Bot

Threaded Slack bot that answers `@mentions` using your workspace history + OpenAI.

## What it does
- Listens to Slack `app_mention` events on `/slack/events`
- Verifies Slack request signatures
- Searches public Slack history with `search.messages` (user token)
- Generates a concise answer with OpenAI and replies in the same thread

## Stack
- Python + Flask
- `slack_sdk`
- OpenAI Python SDK

## Quick start
```bash
python -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env
python app.py
```

Server runs on `http://localhost:3000` by default.

## Required environment variables
Copy `.env.example` to `.env` and set:

- `SLACK_BOT_TOKEN` (xoxb)
- `SLACK_USER_TOKEN` (xoxp, required for Slack search)
- `SLACK_SIGNING_SECRET`
- `OPENAI_API_KEY`
- `PORT` (optional, defaults to 3000)

## Slack app setup
Full setup is documented in `SETUP.md`. Minimum required scopes:

- Bot scopes: `app_mentions:read`, `chat:write`, `channels:read`
- User scopes: `search:read`

Event Subscriptions:
- Enable and set Request URL: `https://<your-domain>/slack/events`
- Subscribe to bot event: `app_mention`

## Deploy
Works on Railway, Render, or any host that can run `python app.py` with env vars set.

## Health check
`GET /health` returns:
```json
{"status":"ok"}
```

## WIP (v2)
- Make the bot model-agnostic (swap OpenAI/Anthropic/Ollama via config)
- Add embeddings + vector DB retrieval for true RAG
- Reduce dependency on Slack keyword search by using semantic retrieval
- Keep threaded Slack UX, but improve answer quality with cited context
- Planned vector DB options: Chroma (local), pgvector, and Qdrant

## Repo bootstrap + push to GitHub
Use this inside this project folder:

```bash
git init
git add .
git commit -m "Initial commit: Slack Knowledge Bot"
gh repo create slack-knowledge-bot --private --source . --remote origin --push
```

If you want it public, switch `--private` to `--public`.
