"""
AgentMesh — Agent B A2A Server

Wraps Agent B's MCP tools as an A2A-compatible agent.
Other agents (like Agent A) can send natural language messages to Agent B
and get natural language responses back — no need to know tool names.

Agent B reads the incoming message, decides which tool(s) to call internally,
runs them, and replies with a synthesized natural language answer.

Port: 9004 (A2A server, separate from MCP router on 9003)

This server handles:
  GET  /.well-known/agent.json  → Agent B's "business card" (what it can do)
  POST /                        → Receive a message, process it, respond

How it fits in the AXL routing chain:
  Remote node sends to /a2a/{node_b_key}
    → Node B's AXL node sees {"a2a": true} envelope
    → Forwards to this A2A server (localhost:9004)
    → We process the message and return a response
"""

import json
import re
import os
import logging
from pathlib import Path
from dotenv import load_dotenv

load_dotenv(Path(__file__).resolve().parents[2] / ".env")

from flask import Flask, request, jsonify

# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
)
logger = logging.getLogger("agent-b-a2a")

app = Flask(__name__)

# ---------------------------------------------------------------------------
# Agent B's identity card — this is what other agents discover
# They call GET /a2a/{node_b_key} and get this back
# ---------------------------------------------------------------------------
AGENT_CARD = {
    "name": "Agent B — Text Intelligence",
    "description": (
        "I'm Agent B, a text intelligence agent on the AgentMesh network. "
        "Send me any text and tell me what you need: summarization, sentiment analysis, "
        "keyword extraction, or translation. I'll handle the rest and reply in plain English. "
        "Each request costs $0.001 USDC via x402."
    ),
    "version": "2.0.0",
    "skills": [
        {
            "id": "text_intelligence",
            "name": "Text Intelligence",
            "description": "Analyze, summarize, extract keywords, detect sentiment, or translate text.",
            "examples": [
                "Summarize this article for me: ...",
                "What's the sentiment of this review?",
                "Extract the key topics from this text.",
                "Translate this to Spanish.",
                "Help me understand what this text is about.",
            ],
        }
    ],
    "capabilities": {
        "streaming": False,
        "payment": {
            "protocol": "x402",
            "network": "eip155:84532",
            "price": "$0.001 USDC per request",
        },
    },
    "url": "http://127.0.0.1:9004",
}

# ---------------------------------------------------------------------------
# Internal tool implementations (same logic as server.py — no HTTP needed)
# Agent B calls these directly when processing A2A messages
# ---------------------------------------------------------------------------

STOP_WORDS = {
    "a", "an", "the", "and", "or", "but", "in", "on", "at", "to", "for",
    "of", "with", "by", "from", "is", "it", "its", "was", "are", "were",
    "be", "been", "have", "has", "had", "do", "does", "did", "will",
    "would", "could", "should", "not", "no", "i", "you", "he", "she",
    "we", "they", "this", "that", "what", "which", "who", "how",
}

POSITIVE_WORDS = {
    "good", "great", "excellent", "amazing", "wonderful", "fantastic",
    "love", "happy", "best", "awesome", "brilliant", "outstanding",
    "perfect", "delightful", "pleased", "glad", "thrilled", "impressive",
    "remarkable", "success", "enjoy", "like", "nice", "helpful",
}

NEGATIVE_WORDS = {
    "bad", "terrible", "awful", "horrible", "hate", "sad", "angry",
    "worst", "poor", "disgusting", "disappointed", "annoyed", "frustrated",
    "fail", "wrong", "broken", "useless", "boring", "miserable",
    "problem", "issue", "error", "negative", "disappointing",
}

_TRANSLATE_EN_ES = {
    "hello": "hola", "goodbye": "adiós", "thank": "gracias",
    "good": "bueno", "bad": "malo", "day": "día", "water": "agua",
    "agent": "agente", "payment": "pago", "network": "red",
    "autonomous": "autónomo", "intelligence": "inteligencia",
    "analysis": "análisis", "text": "texto", "help": "ayuda",
    "new": "nuevo", "system": "sistema", "service": "servicio",
}


def _summarize(text: str, max_sentences: int = 2) -> str:
    sentences = re.split(r'(?<=[.!?])\s+', text.strip())
    sentences = [s for s in sentences if s]
    return " ".join(sentences[:max_sentences])


def _sentiment(text: str) -> tuple[str, float, list, list]:
    words = set(re.findall(r'\b\w+\b', text.lower()))
    pos = words & POSITIVE_WORDS
    neg = words & NEGATIVE_WORDS
    total = len(pos) + len(neg)
    if total == 0:
        return "neutral", 0.5, [], []
    elif len(pos) > len(neg):
        return "positive", round(len(pos) / total, 2), sorted(pos), sorted(neg)
    elif len(neg) > len(pos):
        return "negative", round(len(neg) / total, 2), sorted(pos), sorted(neg)
    else:
        return "neutral", 0.5, sorted(pos), sorted(neg)


def _keywords(text: str, max_kw: int = 8) -> list[str]:
    from collections import Counter
    tokens = re.findall(r'\b[a-zA-Z]+\b', text.lower())
    content = [t for t in tokens if t not in STOP_WORDS and len(t) > 2]
    counts = Counter(content)
    return [word for word, _ in counts.most_common(max_kw)]


def _translate_to_es(text: str) -> str:
    tokens = re.findall(r'\b\w+\b|\W+', text)
    result = []
    for token in tokens:
        w = token.lower()
        if re.match(r'^\w+$', w) and w in _TRANSLATE_EN_ES:
            result.append(_TRANSLATE_EN_ES[w])
        else:
            result.append(token)
    return "".join(result)


# ---------------------------------------------------------------------------
# The "brain" of Agent B's A2A handler
#
# Agent B reads the incoming message, figures out what's being asked,
# calls the right internal tools, and writes a natural language reply.
#
# In a production system this would be an LLM. Here we use intent detection
# so the project has no extra API dependency on Agent B's side.
# ---------------------------------------------------------------------------

def _detect_intent(message: str) -> list[str]:
    """Figure out what the sender wants. Returns a list of intents."""
    msg = message.lower()
    intents = []

    if any(w in msg for w in ["summar", "condense", "brief", "shorten", "tldr"]):
        intents.append("summarize")
    if any(w in msg for w in ["sentiment", "tone", "feel", "emotion", "positive", "negative"]):
        intents.append("sentiment")
    if any(w in msg for w in ["keyword", "topic", "key point", "extract", "theme", "about"]):
        intents.append("keywords")
    if any(w in msg for w in ["translat", "spanish", "french", "español"]):
        intents.append("translate")
    if any(w in msg for w in ["analyz", "analyse", "understand", "help me with", "tell me about"]):
        intents.append("analyze")  # triggers summarize + sentiment + keywords

    # Default: if no specific intent, do a full analysis
    if not intents:
        intents = ["analyze"]

    # "analyze" expands to the three core tools
    if "analyze" in intents:
        intents = list(dict.fromkeys(
            ["summarize", "sentiment", "keywords"] + [i for i in intents if i != "analyze"]
        ))

    return intents


def _extract_text_from_message(message: str) -> str:
    """
    Try to pull the actual text content from the message.
    Agents often say "Summarize this: <text>" so we strip the instruction.
    """
    # Common patterns: "summarize this: TEXT", "analyze the following: TEXT"
    patterns = [
        r'(?:following|this|text|content|passage)[\s:]+["\']?(.*)',
        r'(?:analyze|summarize|review|translate|help me with)[:\s]+["\']?(.*)',
        r'["\'](.+)["\']',  # quoted text
    ]
    for pattern in patterns:
        match = re.search(pattern, message, re.IGNORECASE | re.DOTALL)
        if match:
            extracted = match.group(1).strip().strip('"\'')
            if len(extracted) > 20:  # only use if we got something substantial
                return extracted
    return message  # fallback: treat the whole message as the text


def process_a2a_message(message_text: str, sender_id: str) -> str:
    """
    Agent B's core A2A logic.
    Reads a natural language message, runs the right tools, writes a reply.
    """
    logger.info(f"A2A message from {sender_id[:12]}...: {message_text[:80]}")

    intents = _detect_intent(message_text)
    text_to_process = _extract_text_from_message(message_text)

    logger.info(f"Detected intents: {intents}")
    logger.info(f"Text to process: {text_to_process[:60]}...")

    parts = []

    if "summarize" in intents:
        summary = _summarize(text_to_process)
        parts.append(f"**Summary:** {summary}")

    if "sentiment" in intents:
        label, confidence, pos_words, neg_words = _sentiment(text_to_process)
        sentiment_str = f"**Sentiment:** {label.capitalize()} (confidence: {confidence:.0%})"
        if pos_words:
            sentiment_str += f"\n  Positive signals: {', '.join(pos_words[:5])}"
        if neg_words:
            sentiment_str += f"\n  Negative signals: {', '.join(neg_words[:5])}"
        parts.append(sentiment_str)

    if "keywords" in intents:
        kws = _keywords(text_to_process)
        parts.append(f"**Key topics:** {', '.join(kws)}")

    if "translate" in intents:
        translated = _translate_to_es(text_to_process)
        parts.append(f"**Spanish translation:** {translated}")

    if not parts:
        parts.append("I received your message but couldn't determine what analysis to run. "
                     "Try asking me to summarize, analyze sentiment, extract keywords, or translate.")

    # Compose the reply as a natural language response from Agent B
    reply = (
        f"Hello! I'm Agent B. Here's what I found for your request:\n\n"
        + "\n\n".join(parts)
        + "\n\n— Agent B (Text Intelligence, AgentMesh)"
    )
    return reply


# ---------------------------------------------------------------------------
# A2A Protocol Endpoints
# ---------------------------------------------------------------------------

@app.route("/.well-known/agent.json", methods=["GET"])
def agent_card():
    """
    Agent B's business card.
    Any agent that calls GET /a2a/{node_b_key} gets this back first.
    It tells them: who am I, what can I do, how much do I charge.
    """
    return jsonify(AGENT_CARD)


@app.route("/", methods=["POST"])
def handle_a2a():
    """
    Main A2A message handler.
    Receives a message/send JSON-RPC call from another agent.

    The A2A protocol format:
    {
        "jsonrpc": "2.0",
        "method": "message/send",
        "id": 1,
        "params": {
            "message": {
                "role": "user",
                "parts": [{"kind": "text", "text": "Help me analyze this..."}],
                "messageId": "msg-001"
            }
        }
    }
    """
    req = request.get_json(silent=True)
    if not req:
        return jsonify({
            "jsonrpc": "2.0", "id": None,
            "error": {"code": -32700, "message": "Parse error"},
        }), 400

    method = req.get("method", "")
    req_id = req.get("id")
    params = req.get("params", {})

    # Handle agent card discovery via JSON-RPC too
    if method == "agent/info":
        return jsonify({
            "jsonrpc": "2.0", "id": req_id,
            "result": AGENT_CARD,
        })

    if method != "message/send":
        return jsonify({
            "jsonrpc": "2.0", "id": req_id,
            "error": {"code": -32601, "message": f"Method not found: {method}"},
        })

    # Extract the message text from A2A format
    message = params.get("message", {})
    parts = message.get("parts", [])
    message_id = message.get("messageId", "unknown")

    # Find the text part
    text_parts = [p.get("text", "") for p in parts if p.get("kind") == "text"]
    if not text_parts:
        return jsonify({
            "jsonrpc": "2.0", "id": req_id,
            "error": {"code": -32602, "message": "No text part found in message"},
        })

    full_message = " ".join(text_parts)

    # Get sender identity from header (set by AXL mesh)
    sender_id = request.headers.get("X-From-Peer-Id", "unknown")

    # Process the message — Agent B does its work here
    reply_text = process_a2a_message(full_message, sender_id)

    logger.info(f"A2A reply ready for {sender_id[:12]}..., length={len(reply_text)}")

    # Return in A2A format
    return jsonify({
        "jsonrpc": "2.0",
        "id": req_id,
        "result": {
            "messageId": f"reply-{message_id}",
            "role": "agent",
            "parts": [
                {
                    "kind": "text",
                    "text": reply_text,
                }
            ],
            "metadata": {
                "agent": "agent-b",
                "tools_used": _detect_intent(full_message),
            },
        },
    })


@app.route("/health", methods=["GET"])
def health():
    return jsonify({
        "status": "ok",
        "service": "agentmesh-agent-b-a2a",
        "version": "2.0.0",
    })


if __name__ == "__main__":
    logger.info("Starting Agent B A2A server on http://127.0.0.1:9004")
    logger.info("Agent card available at: http://127.0.0.1:9004/.well-known/agent.json")
    app.run(host="127.0.0.1", port=9004, debug=False)