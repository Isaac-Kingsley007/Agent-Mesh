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


# Tool dispatch table
TOOL_HANDLERS = {
    "summarize": tool_summarize,
    "sentiment": tool_sentiment,
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
