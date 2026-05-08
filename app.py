import os
import re
import hmac
import hashlib
import time
import threading
import numpy as np
from flask import Flask, request, jsonify
from slack_sdk import WebClient
from slack_sdk.errors import SlackApiError
from openai import OpenAI
from pinecone import Pinecone
from dotenv import load_dotenv

load_dotenv()

app = Flask(__name__)

# ---------------------------------------------------------------------------
# Clients — set these in your .env file (see .env.example)
# ---------------------------------------------------------------------------
slack_bot_client = WebClient(token=os.environ.get("SLACK_BOT_TOKEN"))
slack_user_client = WebClient(token=os.environ.get("SLACK_USER_TOKEN"))
openai_client     = OpenAI(api_key=os.environ.get("OPENAI_API_KEY"))
SLACK_SIGNING_SECRET = os.environ.get("SLACK_SIGNING_SECRET", "")

# Pinecone — gracefully disabled if keys are missing
_pc_index = None
try:
    _pc = Pinecone(api_key=os.environ.get("PINECONE_API_KEY"))
    _pc_index = _pc.Index(os.environ.get("PINECONE_INDEX_NAME", "slack-knowledge"))
    print("[startup] Pinecone connected.")
except Exception as e:
    print(f"[startup] Pinecone unavailable, will use fallback search: {e}")


# ---------------------------------------------------------------------------
# Slack signature verification
# ---------------------------------------------------------------------------
def verify_slack_signature(req) -> bool:
    timestamp = req.headers.get("X-Slack-Request-Timestamp", "")
    try:
        if abs(time.time() - int(timestamp)) > 300:
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
# LAYER 1 — Pinecone semantic search (primary)
# ---------------------------------------------------------------------------
def search_pinecone(question: str, top_k: int = 5) -> list[dict]:
    if _pc_index is None:
        return []

    try:
        # Embed the question
        resp = openai_client.embeddings.create(
            model="text-embedding-3-small",
            input=[question]
        )
        query_vector = resp.data[0].embedding

        # Query Pinecone
        results = _pc_index.query(
            vector=query_vector,
            top_k=top_k,
            include_metadata=True
        )

        SCORE_THRESHOLD = 0.35  # discard matches below this — too weak to be relevant
        messages = []
        for match in results.get("matches", []):
            score = match.get("score", 0)
            if score < SCORE_THRESHOLD:
                continue   # skip low-confidence matches
            meta = match.get("metadata", {})
            messages.append({
                "text":      meta.get("text", ""),
                "channel":   meta.get("channel", "unknown"),
                "user":      meta.get("user", "unknown"),
                "permalink": meta.get("permalink", ""),
                "score":     round(score, 3)
            })

        print(f"[search_pinecone] {len(messages)} matches above threshold {SCORE_THRESHOLD}")
        return messages

    except Exception as e:
        print(f"[search_pinecone] Error: {e}")
        return []


# ---------------------------------------------------------------------------
# LAYER 2 — Keyword search via Slack API (fallback candidate fetcher)
# ---------------------------------------------------------------------------
def search_slack_history(query: str, max_results: int = 50) -> list[dict]:
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
                "text":      match.get("text", ""),
                "channel":   match.get("channel", {}).get("name", "unknown"),
                "user":      match.get("username", "unknown"),
                "permalink": match.get("permalink", "")
            })
        return results
    except SlackApiError as e:
        print(f"[search_slack_history] Slack API error: {e}")
        return []


# ---------------------------------------------------------------------------
# LAYER 3 — In-memory semantic reranking (fallback if Pinecone is down)
# ---------------------------------------------------------------------------
def embed_texts(texts: list[str]) -> np.ndarray:
    response = openai_client.embeddings.create(
        model="text-embedding-3-small",
        input=texts
    )
    return np.array([item.embedding for item in response.data])


def cosine_similarity(query_vec: np.ndarray, doc_vecs: np.ndarray) -> np.ndarray:
    query_norm = query_vec / (np.linalg.norm(query_vec) + 1e-10)
    doc_norms  = doc_vecs / (np.linalg.norm(doc_vecs, axis=1, keepdims=True) + 1e-10)
    return doc_norms @ query_norm


def semantic_rerank_candidates(question: str, candidates: list[dict], top_k: int = 5) -> list[dict]:
    try:
        texts = [m["text"] for m in candidates]
        all_embeddings  = embed_texts([question] + texts)
        query_embedding = all_embeddings[0]
        doc_embeddings  = all_embeddings[1:]
        scores          = cosine_similarity(query_embedding, doc_embeddings)
        top_indices     = np.argsort(scores)[::-1][:top_k]
        return [candidates[i] for i in top_indices]
    except Exception as e:
        print(f"[semantic_rerank] Error: {e}")
        return candidates[:top_k]


# ---------------------------------------------------------------------------
# Unified search — tries each layer in order, stops at first result
# ---------------------------------------------------------------------------
def search(question: str, top_k: int = 5) -> tuple[list[dict], str]:
    """
    Returns (messages, source) where source is one of:
      'pinecone'  — full vector search from indexed history
      'semantic'  — in-memory reranking of keyword candidates
      'keyword'   — raw keyword results only
      'none'      — nothing found anywhere
    """

    # Layer 1: Pinecone
    results = search_pinecone(question, top_k=top_k)
    if results:
        return results, "pinecone"

    # Layer 2 & 3: keyword candidates → semantic rerank
    candidates = search_slack_history(question, max_results=50)
    if not candidates:
        return [], "none"

    if len(candidates) <= top_k:
        return candidates, "keyword"

    reranked = semantic_rerank_candidates(question, candidates, top_k=top_k)
    return reranked, "semantic"


# ---------------------------------------------------------------------------
# Build the prompt and return an answer via OpenAI
# ---------------------------------------------------------------------------
def get_ai_answer(question: str, slack_messages: list[dict]) -> str:
    system_message = """You are a company knowledge assistant that ONLY answers from provided Slack messages.

STRICT RULES — you must follow these without exception:
1. ONLY use information explicitly present in the Slack messages provided below.
2. NEVER use your training knowledge to fill gaps or add context not in the messages.
3. NEVER invent names, processes, links, ticket numbers, or any details.
4. If the messages do not contain a clear answer, say exactly: "I couldn't find a reliable answer to this in our Slack history. Try asking in a relevant channel or checking with a team member."
5. Always cite the source channel (e.g. 'according to #customer-success') for every claim you make.
6. If you are even slightly unsure, say so — do not guess."""

    if slack_messages:
        context_lines = [
            f"[#{m['channel']} — {m['user']}] (confidence: {m.get('score', 'n/a')}): {m['text']}"
            for m in slack_messages
        ]
        context = "\n\n".join(context_lines)

        user_message = f"""Here are the most relevant Slack messages I found:

{context}

---
Question: {question}

Answer strictly using only the messages above. Cite the channel for every fact."""

    else:
        user_message = f"""I searched our Slack history but found no relevant messages for this question.

Question: {question}

Tell the user you couldn't find a reliable answer and suggest they ask in a relevant channel."""

    response = openai_client.chat.completions.create(
        model="gpt-4o",
        max_tokens=1024,
        messages=[
            {"role": "system", "content": system_message},
            {"role": "user",   "content": user_message}
        ]
    )
    return response.choices[0].message.content


# ---------------------------------------------------------------------------
# Core handler
# ---------------------------------------------------------------------------
def handle_mention(event_data: dict):
    event    = event_data.get("event", {})
    channel  = event.get("channel")
    thread_ts = event.get("thread_ts") or event.get("ts")

    raw_text = event.get("text", "")
    question = re.sub(r"<@[A-Z0-9]+>", "", raw_text).strip()

    if not question:
        slack_bot_client.chat_postMessage(
            channel=channel, thread_ts=thread_ts,
            text="Hi! Mention me with a question and I'll search our Slack history to help. 👋"
        )
        return

    slack_bot_client.chat_postMessage(
        channel=channel, thread_ts=thread_ts,
        text="🔍 Searching our Slack history..."
    )

    messages, source = search(question)
    answer = get_ai_answer(question, messages)

    print(f"[handle_mention] source={source}, results={len(messages)}")

    slack_bot_client.chat_postMessage(
        channel=channel, thread_ts=thread_ts,
        text=answer
    )


# ---------------------------------------------------------------------------
# Flask routes
# ---------------------------------------------------------------------------
@app.route("/slack/events", methods=["POST"])
def slack_events():
    data = request.json or {}

    if data.get("type") == "url_verification":
        return jsonify({"challenge": data["challenge"]})

    if not verify_slack_signature(request):
        return jsonify({"error": "invalid signature"}), 403

    event = data.get("event", {})

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
