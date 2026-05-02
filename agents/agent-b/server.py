"""
AgentMesh — Agent B MCP Service

Flask HTTP server implementing the MCP (Model Context Protocol) JSON-RPC interface.
Exposes two tools over the AXL mesh:
  - summarize: Condense input text into a concise summary
  - sentiment: Analyze the emotional tone of input text

Routing chain:
  Remote node → POST /mcp/{node_b_key}/agentmesh
    → Node B forwards to MCP Router (localhost:9003)
    → Router dispatches to this service (localhost:7100/mcp)
    → Response flows back to caller
"""

import json
import os
import re
import logging
from pathlib import Path
from dotenv import load_dotenv

# Load .env from project root
load_dotenv(Path(__file__).resolve().parents[2] / ".env")
from flask import Flask, request, jsonify
from x402.http import FacilitatorConfig, HTTPFacilitatorClientSync, PaymentOption
from x402.http.middleware.flask import payment_middleware
from x402.http.types import RouteConfig
from x402.mechanisms.evm.exact import ExactEvmServerScheme
from x402.schemas import Network
from x402.server import x402ResourceServerSync

# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
)
logger = logging.getLogger("agent-b")

# ---------------------------------------------------------------------------
# Flask app
# ---------------------------------------------------------------------------
app = Flask(__name__)

# ---------------------------------------------------------------------------
# x402 payment middleware — protects POST /mcp with $0.001 USDC per call
# ---------------------------------------------------------------------------
EVM_NETWORK: Network = "eip155:84532"  # Base Sepolia testnet
FACILITATOR_URL = "https://x402.org/facilitator"  # testnet facilitator

_pay_to = os.environ.get("AGENT_B_WALLET_ADDRESS")
if not _pay_to:
    raise RuntimeError("AGENT_B_WALLET_ADDRESS env var is required")

logger.info(f"x402 pay-to address: {_pay_to}")

_facilitator = HTTPFacilitatorClientSync(FacilitatorConfig(url=FACILITATOR_URL))
_resource_server = x402ResourceServerSync(_facilitator)
_resource_server.register(EVM_NETWORK, ExactEvmServerScheme())

_routes = {
    "POST /mcp": RouteConfig(
        accepts=[PaymentOption(
            scheme="exact",
            pay_to=_pay_to,
            price="$0.001",
            network=EVM_NETWORK,
        )],
        mime_type="application/json",
        description="AgentMesh MCP tool access — pay-per-call via KeeperHub x402",
    )
}

payment_middleware(app, routes=_routes, server=_resource_server)
# GET /health stays unprotected — it is NOT in _routes

# ---------------------------------------------------------------------------
# Tool definitions (MCP schema)
# ---------------------------------------------------------------------------
TOOLS = [
    {
        "name": "summarize",
        "description": "Summarize input text into a concise version. Returns the key sentences.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "text": {
                    "type": "string",
                    "description": "The text to summarize",
                },
                "max_sentences": {
                    "type": "integer",
                    "description": "Maximum number of sentences in the summary (default: 2)",
                    "default": 2,
                },
            },
            "required": ["text"],
        },
    },
    {
        "name": "sentiment",
        "description": "Analyze the sentiment of input text. Returns positive, negative, or neutral with a confidence score.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "text": {
                    "type": "string",
                    "description": "The text to analyze",
                },
            },
            "required": ["text"],
        },
    },
    {
        "name": "keyword_extract",
        "description": "Extract the most important keywords and key-phrases from text. Returns ranked keywords with frequency scores.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "text": {
                    "type": "string",
                    "description": "The text to extract keywords from",
                },
                "top_n": {
                    "type": "integer",
                    "description": "Number of top keywords to return (default: 10)",
                    "default": 10,
                },
            },
            "required": ["text"],
        },
    },
    {
        "name": "text_translate",
        "description": "Translate text between languages using a built-in dictionary. Supports English, Spanish, French, and German.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "text": {
                    "type": "string",
                    "description": "The text to translate",
                },
                "source_lang": {
                    "type": "string",
                    "description": "Source language code: en, es, fr, de (default: en)",
                    "default": "en",
                },
                "target_lang": {
                    "type": "string",
                    "description": "Target language code: en, es, fr, de",
                },
            },
            "required": ["text", "target_lang"],
        },
    },
    {
        "name": "word_frequency",
        "description": "Analyze the word frequency distribution of text. Returns top words, unique count, and statistics.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "text": {
                    "type": "string",
                    "description": "The text to analyze",
                },
                "top_n": {
                    "type": "integer",
                    "description": "Number of top words to return (default: 15)",
                    "default": 15,
                },
            },
            "required": ["text"],
        },
    },
    {
        "name": "text_similarity",
        "description": "Compare two texts for similarity using Jaccard similarity. Returns similarity score and shared/unique terms.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "text_a": {
                    "type": "string",
                    "description": "First text to compare",
                },
                "text_b": {
                    "type": "string",
                    "description": "Second text to compare",
                },
            },
            "required": ["text_a", "text_b"],
        },
    },
    {
        "name": "entity_extract",
        "description": "Extract named entities from text including emails, URLs, dates, phone numbers, and currency amounts.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "text": {
                    "type": "string",
                    "description": "The text to extract entities from",
                },
            },
            "required": ["text"],
        },
    },
]

# ---------------------------------------------------------------------------
# Tool implementations
# ---------------------------------------------------------------------------

# Sentiment keyword lists
POSITIVE_WORDS = {
    "good", "great", "excellent", "amazing", "wonderful", "fantastic",
    "love", "happy", "joy", "best", "beautiful", "awesome", "brilliant",
    "outstanding", "superb", "perfect", "delightful", "pleased", "glad",
    "thrilled", "excited", "impressive", "remarkable", "success", "win",
    "enjoy", "like", "positive", "nice", "cool", "helpful", "thank",
}

NEGATIVE_WORDS = {
    "bad", "terrible", "awful", "horrible", "hate", "sad", "angry",
    "worst", "ugly", "poor", "disgusting", "disappointed", "annoyed",
    "frustrated", "fail", "failure", "wrong", "broken", "useless",
    "boring", "dreadful", "pathetic", "miserable", "tragic", "disaster",
    "problem", "issue", "error", "bug", "crash", "negative", "never",
}


def _split_sentences(text: str) -> list[str]:
    """Split text into sentences using simple regex."""
    sentences = re.split(r'(?<=[.!?])\s+', text.strip())
    return [s.strip() for s in sentences if s.strip()]


def tool_summarize(arguments: dict) -> dict:
    """Summarize text by extracting leading sentences."""
    text = arguments.get("text", "")
    max_sentences = arguments.get("max_sentences", 2)

    if not text:
        return {"error": "No text provided"}

    sentences = _split_sentences(text)
    if not sentences:
        return {"summary": text, "original_length": len(text), "sentence_count": 0}

    summary_sentences = sentences[:max_sentences]
    summary = " ".join(summary_sentences)

    return {
        "summary": summary,
        "original_sentences": len(sentences),
        "summary_sentences": len(summary_sentences),
        "compression_ratio": round(len(summary) / max(len(text), 1), 2),
    }


def tool_sentiment(arguments: dict) -> dict:
    """Analyze sentiment using keyword scoring."""
    text = arguments.get("text", "")

    if not text:
        return {"error": "No text provided"}

    words = set(re.findall(r'\b\w+\b', text.lower()))

    pos_matches = words & POSITIVE_WORDS
    neg_matches = words & NEGATIVE_WORDS

    pos_count = len(pos_matches)
    neg_count = len(neg_matches)
    total = pos_count + neg_count

    if total == 0:
        sentiment = "neutral"
        confidence = 0.5
    elif pos_count > neg_count:
        sentiment = "positive"
        confidence = round(pos_count / total, 2)
    elif neg_count > pos_count:
        sentiment = "negative"
        confidence = round(neg_count / total, 2)
    else:
        sentiment = "neutral"
        confidence = 0.5

    return {
        "sentiment": sentiment,
        "confidence": confidence,
        "positive_signals": list(pos_matches),
        "negative_signals": list(neg_matches),
        "word_count": len(words),
    }


# ── Stop words for keyword extraction ──
STOP_WORDS = {
    "the", "a", "an", "is", "are", "was", "were", "be", "been", "being",
    "have", "has", "had", "do", "does", "did", "will", "would", "could",
    "should", "may", "might", "shall", "can", "need", "dare", "ought",
    "used", "to", "of", "in", "for", "on", "with", "at", "by", "from",
    "as", "into", "through", "during", "before", "after", "above", "below",
    "between", "out", "off", "over", "under", "again", "further", "then",
    "once", "here", "there", "when", "where", "why", "how", "all", "each",
    "every", "both", "few", "more", "most", "other", "some", "such", "no",
    "nor", "not", "only", "own", "same", "so", "than", "too", "very",
    "just", "because", "but", "and", "or", "if", "while", "about", "up",
    "that", "this", "these", "those", "it", "its", "i", "me", "my", "we",
    "our", "you", "your", "he", "him", "his", "she", "her", "they", "them",
    "their", "what", "which", "who", "whom", "also", "s", "t", "d", "re",
}


def tool_keyword_extract(arguments: dict) -> dict:
    """Extract top keywords from text using term frequency with stop-word filtering."""
    text = arguments.get("text", "")
    top_n = arguments.get("top_n", 10)

    if not text:
        return {"error": "No text provided"}

    words = re.findall(r'\b[a-zA-Z]{2,}\b', text.lower())
    filtered = [w for w in words if w not in STOP_WORDS]

    freq = {}
    for w in filtered:
        freq[w] = freq.get(w, 0) + 1

    ranked = sorted(freq.items(), key=lambda x: x[1], reverse=True)[:top_n]

    return {
        "keywords": [{"word": w, "count": c, "score": round(c / max(len(filtered), 1), 4)} for w, c in ranked],
        "total_words": len(words),
        "unique_keywords": len(freq),
    }


# ── Translation dictionaries ──
TRANSLATION_DICTS = {
    ("en", "es"): {
        "hello": "hola", "world": "mundo", "good": "bueno", "morning": "mañana",
        "thank": "gracias", "you": "tú", "please": "por favor", "yes": "sí",
        "no": "no", "the": "el", "is": "es", "are": "son", "i": "yo",
        "love": "amor", "day": "día", "night": "noche", "water": "agua",
        "food": "comida", "house": "casa", "book": "libro", "time": "tiempo",
        "work": "trabajo", "life": "vida", "new": "nuevo", "big": "grande",
        "small": "pequeño", "happy": "feliz", "sad": "triste", "friend": "amigo",
        "agent": "agente", "payment": "pago", "network": "red", "autonomous": "autónomo",
    },
    ("en", "fr"): {
        "hello": "bonjour", "world": "monde", "good": "bon", "morning": "matin",
        "thank": "merci", "you": "vous", "please": "s'il vous plaît", "yes": "oui",
        "no": "non", "the": "le", "is": "est", "are": "sont", "i": "je",
        "love": "amour", "day": "jour", "night": "nuit", "water": "eau",
        "food": "nourriture", "house": "maison", "book": "livre", "time": "temps",
        "work": "travail", "life": "vie", "new": "nouveau", "big": "grand",
        "small": "petit", "happy": "heureux", "sad": "triste", "friend": "ami",
        "agent": "agent", "payment": "paiement", "network": "réseau", "autonomous": "autonome",
    },
    ("en", "de"): {
        "hello": "hallo", "world": "welt", "good": "gut", "morning": "morgen",
        "thank": "danke", "you": "du", "please": "bitte", "yes": "ja",
        "no": "nein", "the": "der", "is": "ist", "are": "sind", "i": "ich",
        "love": "liebe", "day": "tag", "night": "nacht", "water": "wasser",
        "food": "essen", "house": "haus", "book": "buch", "time": "zeit",
        "work": "arbeit", "life": "leben", "new": "neu", "big": "groß",
        "small": "klein", "happy": "glücklich", "sad": "traurig", "friend": "freund",
        "agent": "agent", "payment": "zahlung", "network": "netzwerk", "autonomous": "autonom",
    },
}


def tool_text_translate(arguments: dict) -> dict:
    """Translate text using built-in dictionaries."""
    text = arguments.get("text", "")
    source = arguments.get("source_lang", "en").lower()
    target = arguments.get("target_lang", "").lower()

    if not text:
        return {"error": "No text provided"}
    if not target:
        return {"error": "target_lang is required"}
    if source == target:
        return {"translated": text, "source_lang": source, "target_lang": target, "note": "Same language"}

    dictionary = TRANSLATION_DICTS.get((source, target))
    if not dictionary:
        # Try reverse
        rev = TRANSLATION_DICTS.get((target, source))
        if rev:
            dictionary = {v: k for k, v in rev.items()}
        else:
            return {"error": f"Translation pair {source}→{target} not supported. Supported: en↔es, en↔fr, en↔de"}

    words = re.findall(r'\b\w+\b|\S', text)
    translated_words = []
    translated_count = 0
    for w in words:
        lower = w.lower()
        if lower in dictionary:
            translated_words.append(dictionary[lower])
            translated_count += 1
        else:
            translated_words.append(w)

    return {
        "translated": " ".join(translated_words),
        "source_lang": source,
        "target_lang": target,
        "words_translated": translated_count,
        "total_words": len(words),
    }


def tool_word_frequency(arguments: dict) -> dict:
    """Analyze word frequency distribution of text."""
    text = arguments.get("text", "")
    top_n = arguments.get("top_n", 15)

    if not text:
        return {"error": "No text provided"}

    words = re.findall(r'\b[a-zA-Z]+\b', text.lower())
    total = len(words)

    freq = {}
    for w in words:
        freq[w] = freq.get(w, 0) + 1

    ranked = sorted(freq.items(), key=lambda x: x[1], reverse=True)
    top_words = ranked[:top_n]

    return {
        "top_words": [{"word": w, "count": c, "percentage": round(c / max(total, 1) * 100, 1)} for w, c in top_words],
        "total_words": total,
        "unique_words": len(freq),
        "avg_frequency": round(total / max(len(freq), 1), 2),
        "hapax_legomena": sum(1 for c in freq.values() if c == 1),
    }


def tool_text_similarity(arguments: dict) -> dict:
    """Compare two texts using Jaccard similarity."""
    text_a = arguments.get("text_a", "")
    text_b = arguments.get("text_b", "")

    if not text_a or not text_b:
        return {"error": "Both text_a and text_b are required"}

    words_a = set(re.findall(r'\b\w+\b', text_a.lower()))
    words_b = set(re.findall(r'\b\w+\b', text_b.lower()))

    intersection = words_a & words_b
    union = words_a | words_b
    jaccard = round(len(intersection) / max(len(union), 1), 4)

    return {
        "jaccard_similarity": jaccard,
        "similarity_percent": round(jaccard * 100, 1),
        "shared_terms": sorted(list(intersection)),
        "unique_to_a": sorted(list(words_a - words_b)),
        "unique_to_b": sorted(list(words_b - words_a)),
        "terms_in_a": len(words_a),
        "terms_in_b": len(words_b),
    }


def tool_entity_extract(arguments: dict) -> dict:
    """Extract entities (emails, URLs, dates, phone numbers, currency) via regex."""
    text = arguments.get("text", "")

    if not text:
        return {"error": "No text provided"}

    emails = re.findall(r'[a-zA-Z0-9._%+-]+@[a-zA-Z0-9.-]+\.[a-zA-Z]{2,}', text)
    urls = re.findall(r'https?://[^\s<>"{}|\\^`\[\]]+', text)
    dates = re.findall(
        r'\b\d{1,2}[/-]\d{1,2}[/-]\d{2,4}\b'
        r'|\b\d{4}[/-]\d{1,2}[/-]\d{1,2}\b'
        r'|\b(?:Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec)[a-z]* \d{1,2},? \d{4}\b',
        text, re.IGNORECASE,
    )
    phones = re.findall(r'[\+]?[(]?\d{1,4}[)]?[-\s.]?\d{1,4}[-\s.]?\d{1,9}', text)
    phones = [p for p in phones if len(re.findall(r'\d', p)) >= 7]
    currency = re.findall(r'[\$€£¥]\s?\d[\d,]*\.?\d*|\d[\d,]*\.?\d*\s?(?:USD|EUR|GBP|USDC|ETH|BTC)', text)

    entities = {
        "emails": emails,
        "urls": urls,
        "dates": dates,
        "phone_numbers": phones,
        "currency_amounts": currency,
    }

    total = sum(len(v) for v in entities.values())

    return {
        "entities": entities,
        "total_entities_found": total,
    }


# Tool dispatch table
TOOL_HANDLERS = {
    "summarize": tool_summarize,
    "sentiment": tool_sentiment,
    "keyword_extract": tool_keyword_extract,
    "text_translate": tool_text_translate,
    "word_frequency": tool_word_frequency,
    "text_similarity": tool_text_similarity,
    "entity_extract": tool_entity_extract,
}

# ---------------------------------------------------------------------------
# MCP JSON-RPC endpoint
# ---------------------------------------------------------------------------

@app.route("/mcp", methods=["POST"])
def handle_mcp():
    """Handle MCP JSON-RPC requests.

    Supported methods:
      - initialize       → server capabilities
      - tools/list       → list available tools
      - tools/call       → call a specific tool
      - notifications/*  → acknowledge silently
    """
    req = request.get_json(silent=True)
    if not req:
        return jsonify({
            "jsonrpc": "2.0",
            "id": None,
            "error": {"code": -32700, "message": "Parse error: invalid JSON"},
        }), 400

    method = req.get("method", "")
    req_id = req.get("id")
    params = req.get("params", {})

    logger.info(f"MCP request: method={method} id={req_id}")

    # --- initialize ---
    if method == "initialize":
        return jsonify({
            "jsonrpc": "2.0",
            "id": req_id,
            "result": {
                "protocolVersion": "2024-11-05",
                "capabilities": {"tools": {"listChanged": False}},
                "serverInfo": {
                    "name": "agentmesh-agent-b",
                    "version": "1.0.0",
                },
            },
        })

    # --- tools/list ---
    if method == "tools/list":
        return jsonify({
            "jsonrpc": "2.0",
            "id": req_id,
            "result": {"tools": TOOLS},
        })

    # --- tools/call ---
    if method == "tools/call":
        tool_name = params.get("name", "")
        arguments = params.get("arguments", {})

        if tool_name not in TOOL_HANDLERS:
            return jsonify({
                "jsonrpc": "2.0",
                "id": req_id,
                "error": {
                    "code": -32601,
                    "message": f"Unknown tool: {tool_name}",
                },
            })

        try:
            result = TOOL_HANDLERS[tool_name](arguments)
            logger.info(f"Tool {tool_name} executed successfully")
            return jsonify({
                "jsonrpc": "2.0",
                "id": req_id,
                "result": {
                    "content": [
                        {
                            "type": "text",
                            "text": json.dumps(result),
                        }
                    ],
                },
            })
        except Exception as e:
            logger.error(f"Tool {tool_name} failed: {e}")
            return jsonify({
                "jsonrpc": "2.0",
                "id": req_id,
                "error": {
                    "code": -32603,
                    "message": f"Internal error: {str(e)}",
                },
            })

    # --- notifications (fire-and-forget) ---
    if method.startswith("notifications/"):
        return "", 204

    # --- unknown method ---
    return jsonify({
        "jsonrpc": "2.0",
        "id": req_id,
        "error": {
            "code": -32601,
            "message": f"Method not found: {method}",
        },
    })


@app.route("/health", methods=["GET"])
def health():
    """Health check endpoint."""
    return jsonify({
        "status": "ok",
        "service": "agentmesh-agent-b",
        "tools": [t["name"] for t in TOOLS],
    })


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------
if __name__ == "__main__":
    logger.info("Starting Agent B MCP service on http://127.0.0.1:7100")
    logger.info(f"Available tools: {[t['name'] for t in TOOLS]}")
    app.run(host="127.0.0.1", port=7100, debug=False)
