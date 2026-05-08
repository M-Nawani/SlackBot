import os
import re
import hmac
import hashlib
import time
import threading
from flask import Flask, request, jsonify
from slack_sdk import WebClient
from slack_sdk.errors import SlackApiError
from openai import OpenAI
from dotenv import load_dotenv

load_dotenv()

app = Flask(__name__)

# ---------------------------------------------------------------------------
# Clients — set these in your .env file (see .env.example)
# ---------------------------------------------------------------------------
slack_bot_client = WebClient(token=os.environ.get("SLACK_BOT_TOKEN"))
slack_user_client = WebClient(token=os.environ.get("SLACK_USER_TOKEN"))  # needed for search
openai_client = OpenAI(api_key=os.environ.get("OPENAI_API_KEY"))
SLACK_SIGNING_SECRET = os.environ.get("SLACK_SIGNING_SECRET", "")

# ---------------------------------------------------------------------------
# Slack signature verification — proves the request came from Slack
# ---------------------------------------------------------------------------
def verify_slack_signature(req) -> bool:
    timestamp = req.headers.get("X-Slack-Request-Timestamp", "")
    try:
        if abs(time.time() - int(timestamp)) > 300:   # reject requests older than 5 min
            return False
    except ValueError:
        return False

    sig_base = f"v0:{timestamp}:{req.get_data(as_text=True)}"
    expected = "v0=" + hmac.new(
        SLACK_SIGNING_SECRET.encode(),
        sig_base.encode(),
        hashlib.sha256
    ).hexdigest()

    return hmac.compare_digest(expected, req.headers.get("X-Slack-Signature", ""))


# ---------------------------------------------------------------------------
# Search Slack history for relevant messages
# Uses a user token because Slack's search.messages API requires one
# ---------------------------------------------------------------------------
def search_slack_history(query: str, max_results: int = 10) -> list[dict]:
    try:
        response = slack_user_client.search_messages(
            query=query,
            count=max_results,
            sort="score",
            sort_dir="desc"
        )
        results = []
        for match in response["messages"]["matches"]:
            results.append({
                "text": match.get("text", ""),
                "channel": match.get("channel", {}).get("name", "unknown"),
                "user": match.get("username", "unknown"),
                "permalink": match.get("permalink", "")
            })
        return results
    except SlackApiError as e:
        print(f"[search_slack_history] Slack API error: {e}")
        return []


# ---------------------------------------------------------------------------
# Build the prompt and return an answer via OpenAI
# ---------------------------------------------------------------------------
def get_ai_answer(question: str, slack_messages: list[dict]) -> str:
    if slack_messages:
        context_lines = [
            f"[#{m['channel']} — {m['user']}]: {m['text']}"
            for m in slack_messages
        ]
        context = "\n\n".join(context_lines)

        prompt = f"""You are a helpful internal knowledge assistant for our company.
You answer questions by drawing from our Slack conversation history.

Relevant Slack messages:
{context}

---
Question: {question}

Give a clear, concise answer based on the messages above.
If the messages don't fully answer the question, say so and suggest where to look.
When useful, mention which Slack channel the information came from."""

    else:
        prompt = f"""You are a helpful internal knowledge assistant for our company.
I searched our Slack history but couldn't find relevant messages for this question.

Question: {question}

Let the user know you didn't find a match in Slack history.
Suggest they ask in a relevant channel or reach out to a team member."""

    response = openai_client.chat.completions.create(
        model="gpt-4o",
        max_tokens=1024,
        messages=[{"role": "user", "content": prompt}]
    )
    return response.choices[0].message.content


# ---------------------------------------------------------------------------
# Core handler — runs in a background thread so we reply to Slack in < 3s
# ---------------------------------------------------------------------------
def handle_mention(event_data: dict):
    event = event_data.get("event", {})
    channel = event.get("channel")
    thread_ts = event.get("thread_ts") or event.get("ts")   # reply in-thread

    # Strip the @BotName mention from the text
    raw_text = event.get("text", "")
    question = re.sub(r"<@[A-Z0-9]+>", "", raw_text).strip()

    if not question:
        slack_bot_client.chat_postMessage(
            channel=channel,
            thread_ts=thread_ts,
            text="Hi! Mention me with a question and I'll search our Slack history to help. 👋"
        )
        return

    # Acknowledge while we work
    slack_bot_client.chat_postMessage(
        channel=channel,
        thread_ts=thread_ts,
        text="🔍 Searching our Slack history..."
    )

    messages = search_slack_history(question)
    answer = get_ai_answer(question, messages)

    slack_bot_client.chat_postMessage(
        channel=channel,
        thread_ts=thread_ts,
        text=answer
    )


# ---------------------------------------------------------------------------
# Flask routes
# ---------------------------------------------------------------------------
@app.route("/slack/events", methods=["POST"])
def slack_events():
    data = request.json or {}

    # Slack sends this once to verify your endpoint URL
    if data.get("type") == "url_verification":
        return jsonify({"challenge": data["challenge"]})

    # Reject anything that doesn't have a valid Slack signature
    if not verify_slack_signature(request):
        return jsonify({"error": "invalid signature"}), 403

    event = data.get("event", {})

    # Only act on @mentions; ignore messages from bots (including ourselves)
    if event.get("type") == "app_mention" and not event.get("bot_id"):
        t = threading.Thread(target=handle_mention, args=(data,), daemon=True)
        t.start()

    return jsonify({"ok": True})


@app.route("/health", methods=["GET"])
def health():
    return jsonify({"status": "ok"})


if __name__ == "__main__":
    port = int(os.environ.get("PORT", 3000))
    app.run(host="0.0.0.0", port=port)
