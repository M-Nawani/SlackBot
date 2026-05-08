"""
ingest.py — Pull Slack history and store as embeddings in Pinecone.

Run once to do the initial load, then run again periodically (e.g. nightly)
to keep the index fresh. Safe to re-run — existing vectors are overwritten
by the same ID, not duplicated.

Usage:
    uv run python ingest.py              # ingest last 90 days (default)
    uv run python ingest.py --days 30    # ingest last 30 days
    uv run python ingest.py --days 365   # ingest last year

Required user token scopes (add these in Slack App → OAuth & Permissions → User Token Scopes):
    channels:read    — list public channels
    channels:history — read message history
    search:read      — already added
"""

import os
import time
import argparse
from datetime import datetime, timedelta
from dotenv import load_dotenv
from slack_sdk import WebClient
from slack_sdk.errors import SlackApiError
from openai import OpenAI
from pinecone import Pinecone, ServerlessSpec

load_dotenv()

# ---------------------------------------------------------------------------
# Clients
# ---------------------------------------------------------------------------
slack_client = WebClient(token=os.environ.get("SLACK_USER_TOKEN"))
openai_client = OpenAI(api_key=os.environ.get("OPENAI_API_KEY"))

PINECONE_API_KEY   = os.environ.get("PINECONE_API_KEY")
PINECONE_INDEX     = os.environ.get("PINECONE_INDEX_NAME", "slack-knowledge")
EMBED_MODEL        = "text-embedding-3-small"
EMBED_DIMENSION    = 1536
BATCH_SIZE         = 100   # messages per embedding + upsert batch


# ---------------------------------------------------------------------------
# Step 1 — Ensure the Pinecone index exists
# ---------------------------------------------------------------------------
def get_or_create_index():
    pc = Pinecone(api_key=PINECONE_API_KEY)
    existing = [i.name for i in pc.list_indexes()]

    if PINECONE_INDEX not in existing:
        print(f"Creating Pinecone index '{PINECONE_INDEX}'...")
        pc.create_index(
            name=PINECONE_INDEX,
            dimension=EMBED_DIMENSION,
            metric="cosine",
            spec=ServerlessSpec(cloud="aws", region="us-east-1")
        )
        # Wait for index to be ready
        while not pc.describe_index(PINECONE_INDEX).status["ready"]:
            print("Waiting for index to be ready...")
            time.sleep(2)
        print("Index ready.")
    else:
        print(f"Using existing Pinecone index '{PINECONE_INDEX}'.")

    return pc.Index(PINECONE_INDEX)


# ---------------------------------------------------------------------------
# Step 2 — Fetch all public channels
# ---------------------------------------------------------------------------
def get_public_channels() -> list[dict]:
    channels = []
    cursor = None

    while True:
        try:
            resp = slack_client.conversations_list(
                types="public_channel",
                limit=200,
                cursor=cursor
            )
            channels.extend(resp["channels"])
            cursor = resp.get("response_metadata", {}).get("next_cursor")
            if not cursor:
                break
            time.sleep(0.5)   # stay under rate limits
        except SlackApiError as e:
            print(f"[get_channels] Error: {e}")
            break

    print(f"Found {len(channels)} public channels.")
    return channels


# ---------------------------------------------------------------------------
# Step 3 — Fetch messages from a channel since a given timestamp
# ---------------------------------------------------------------------------
def get_channel_messages(channel_id: str, oldest_ts: str) -> list[dict]:
    messages = []
    cursor = None

    while True:
        try:
            resp = slack_client.conversations_history(
                channel=channel_id,
                oldest=oldest_ts,
                limit=200,
                cursor=cursor
            )
            messages.extend(resp.get("messages", []))
            cursor = resp.get("response_metadata", {}).get("next_cursor")
            if not cursor:
                break
            time.sleep(0.5)
        except SlackApiError as e:
            if "not_in_channel" in str(e) or "missing_scope" in str(e):
                return []   # skip channels we can't access
            print(f"[get_messages] Error in {channel_id}: {e}")
            break

    return messages


# ---------------------------------------------------------------------------
# Step 4 — Fetch replies for threaded messages
# ---------------------------------------------------------------------------
def get_thread_replies(channel_id: str, thread_ts: str) -> list[dict]:
    try:
        resp = slack_client.conversations_replies(
            channel=channel_id,
            ts=thread_ts
        )
        # Skip the first message — it's the parent, already in history
        return resp.get("messages", [])[1:]
    except SlackApiError:
        return []


# ---------------------------------------------------------------------------
# Step 5 — Embed a batch of texts
# ---------------------------------------------------------------------------
def embed_batch(texts: list[str]) -> list[list[float]]:
    resp = openai_client.embeddings.create(
        model=EMBED_MODEL,
        input=texts
    )
    return [item.embedding for item in resp.data]


# ---------------------------------------------------------------------------
# Step 6 — Upsert a batch of vectors to Pinecone
# ---------------------------------------------------------------------------
def upsert_batch(index, vectors: list[dict]):
    index.upsert(vectors=vectors)


# ---------------------------------------------------------------------------
# Main ingestion flow
# ---------------------------------------------------------------------------
def ingest(days: int = 90):
    oldest_ts = str((datetime.utcnow() - timedelta(days=days)).timestamp())
    print(f"Ingesting messages from the last {days} days...")

    index = get_or_create_index()
    channels = get_public_channels()

    total_upserted = 0
    buffer = []   # accumulate (id, text, metadata) tuples before batching

    for ch in channels:
        channel_id   = ch["id"]
        channel_name = ch.get("name", channel_id)

        messages = get_channel_messages(channel_id, oldest_ts)
        if not messages:
            continue

        print(f"  #{channel_name}: {len(messages)} messages")

        for msg in messages:
            text = msg.get("text", "").strip()
            if not text or msg.get("subtype"):   # skip system messages
                continue

            ts   = msg.get("ts", "")
            user = msg.get("user", "unknown")

            buffer.append({
                "id":       f"{channel_id}_{ts}",
                "text":     text,
                "channel":  channel_name,
                "user":     user,
                "ts":       ts,
                "permalink": ""   # ingest doesn't fetch permalinks — bot does at query time
            })

            # Fetch thread replies too
            if msg.get("reply_count", 0) > 0:
                replies = get_thread_replies(channel_id, ts)
                for reply in replies:
                    reply_text = reply.get("text", "").strip()
                    if not reply_text:
                        continue
                    reply_ts = reply.get("ts", "")
                    buffer.append({
                        "id":      f"{channel_id}_{reply_ts}",
                        "text":    reply_text,
                        "channel": channel_name,
                        "user":    reply.get("user", "unknown"),
                        "ts":      reply_ts,
                        "permalink": ""
                    })

            # Flush buffer in batches
            if len(buffer) >= BATCH_SIZE:
                total_upserted += flush_buffer(index, buffer)
                buffer = []

        time.sleep(1)   # be respectful of Slack rate limits between channels

    # Flush any remaining messages
    if buffer:
        total_upserted += flush_buffer(index, buffer)

    print(f"\nDone. {total_upserted} messages upserted to Pinecone index '{PINECONE_INDEX}'.")


def flush_buffer(index, buffer: list[dict]) -> int:
    texts = [m["text"] for m in buffer]
    embeddings = embed_batch(texts)

    vectors = [
        {
            "id": m["id"],
            "values": emb,
            "metadata": {
                "text":    m["text"],
                "channel": m["channel"],
                "user":    m["user"],
                "ts":      m["ts"]
            }
        }
        for m, emb in zip(buffer, embeddings)
    ]

    upsert_batch(index, vectors)
    print(f"    Upserted batch of {len(vectors)}")
    return len(vectors)


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------
if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--days", type=int, default=90,
                        help="How many days of history to ingest (default: 90)")
    args = parser.parse_args()
    ingest(days=args.days)
