"""
AgentMesh — Agent A MCP Service (Gemini-Powered Tools)

Flask HTTP server exposing Agent A's Gemini-powered tools over the AXL mesh.
Protected by x402 payment middleware — callers pay $0.001 USDC per call.

Tools:
  - gemini_analyze: Deep analysis of text using Gemini LLM
  - gemini_rewrite: Rewrite text in a specified style/tone

Routing chain:
  Remote node → POST /mcp/{node_a_key}/agent-a
    → Node A forwards to MCP Router (localhost:9013)
    → Router dispatches to this service (localhost:7200/mcp)
    → Response flows back to caller
"""

import json
import os
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

from google import genai

# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
)
logger = logging.getLogger("agent-a-server")

# ---------------------------------------------------------------------------
# Flask app
# ---------------------------------------------------------------------------
app = Flask(__name__)

# ---------------------------------------------------------------------------
# x402 payment middleware — protects POST /mcp with $0.001 USDC per call
# ---------------------------------------------------------------------------
EVM_NETWORK: Network = "eip155:84532"  # Base Sepolia testnet
FACILITATOR_URL = "https://x402.org/facilitator"

_pay_to = os.environ.get("AGENT_A_EVM_ADDRESS")
if not _pay_to:
    raise RuntimeError("AGENT_A_EVM_ADDRESS env var is required (Agent A's wallet for receiving payments)")

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
        description="Agent A Gemini-powered tools — pay-per-call via x402",
    )
}

payment_middleware(app, routes=_routes, server=_resource_server)

# ---------------------------------------------------------------------------
# Gemini client
# ---------------------------------------------------------------------------
GEMINI_API_KEY = os.environ.get("GEMINI_API_KEY")
if not GEMINI_API_KEY:
    raise RuntimeError("GEMINI_API_KEY env var is required")

GEMINI_MODEL = "gemini-3-flash-preview"
gemini_client = genai.Client(api_key=GEMINI_API_KEY)
logger.info(f"Gemini model: {GEMINI_MODEL}")

# ---------------------------------------------------------------------------
# Tool definitions (MCP schema)
# ---------------------------------------------------------------------------
TOOLS = [
    {
        "name": "gemini_analyze",
        "description": "Use Gemini LLM to deeply analyze text — extract themes, insights, implications, and key takeaways.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "text": {
                    "type": "string",
                    "description": "The text to analyze",
                },
                "focus": {
                    "type": "string",
                    "description": "Optional focus area for analysis (e.g., 'business implications', 'technical depth', 'risks')",
                },
            },
            "required": ["text"],
        },
    },
    {
        "name": "gemini_rewrite",
        "description": "Use Gemini LLM to rewrite text in a specified style or tone. Styles: formal, casual, technical, persuasive, concise, creative.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "text": {
                    "type": "string",
                    "description": "The text to rewrite",
                },
                "style": {
                    "type": "string",
                    "description": "Target style: formal, casual, technical, persuasive, concise, or creative (default: formal)",
                },
            },
            "required": ["text"],
        },
    },
]

# ---------------------------------------------------------------------------
# Tool implementations
# ---------------------------------------------------------------------------

def tool_gemini_analyze(arguments: dict) -> dict:
    """Use Gemini to analyze text."""
    text = arguments.get("text", "")
    focus = arguments.get("focus", "general")

    if not text:
        return {"error": "No text provided"}

    prompt = (
        f"Analyze the following text. Focus area: {focus}.\n"
        f"Provide: 1) Key themes, 2) Main insights, 3) Implications, 4) Summary.\n"
        f"Be concise but thorough.\n\n"
        f"Text:\n{text}"
    )

    try:
        response = gemini_client.models.generate_content(
            model=GEMINI_MODEL,
            contents=prompt,
        )
        return {
            "analysis": response.text,
            "focus": focus,
            "input_length": len(text),
            "model": GEMINI_MODEL,
        }
    except Exception as e:
        logger.error(f"Gemini analyze failed: {e}")
        return {"error": f"Gemini call failed: {str(e)}"}


def tool_gemini_rewrite(arguments: dict) -> dict:
    """Use Gemini to rewrite text in a specified style."""
    text = arguments.get("text", "")
    style = arguments.get("style", "formal")

    if not text:
        return {"error": "No text provided"}

    prompt = (
        f"Rewrite the following text in a {style} style. "
        f"Keep the core meaning intact but transform the tone and phrasing.\n\n"
        f"Original text:\n{text}"
    )

    try:
        response = gemini_client.models.generate_content(
            model=GEMINI_MODEL,
            contents=prompt,
        )
        return {
            "rewritten": response.text,
            "style": style,
            "original_length": len(text),
            "rewritten_length": len(response.text),
            "model": GEMINI_MODEL,
        }
    except Exception as e:
        logger.error(f"Gemini rewrite failed: {e}")
        return {"error": f"Gemini call failed: {str(e)}"}


# Tool dispatch table
TOOL_HANDLERS = {
    "gemini_analyze": tool_gemini_analyze,
    "gemini_rewrite": tool_gemini_rewrite,
}

# ---------------------------------------------------------------------------
# MCP JSON-RPC endpoint
# ---------------------------------------------------------------------------

@app.route("/mcp", methods=["POST"])
def handle_mcp():
    """Handle MCP JSON-RPC requests."""
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

    if method == "initialize":
        return jsonify({
            "jsonrpc": "2.0",
            "id": req_id,
            "result": {
                "protocolVersion": "2024-11-05",
                "capabilities": {"tools": {"listChanged": False}},
                "serverInfo": {"name": "agentmesh-agent-a", "version": "1.0.0"},
            },
        })

    if method == "tools/list":
        return jsonify({
            "jsonrpc": "2.0",
            "id": req_id,
            "result": {"tools": TOOLS},
        })

    if method == "tools/call":
        tool_name = params.get("name", "")
        arguments = params.get("arguments", {})

        if tool_name not in TOOL_HANDLERS:
            return jsonify({
                "jsonrpc": "2.0",
                "id": req_id,
                "error": {"code": -32601, "message": f"Unknown tool: {tool_name}"},
            })

        try:
            result = TOOL_HANDLERS[tool_name](arguments)
            logger.info(f"Tool {tool_name} executed successfully")
            return jsonify({
                "jsonrpc": "2.0",
                "id": req_id,
                "result": {
                    "content": [{"type": "text", "text": json.dumps(result)}],
                },
            })
        except Exception as e:
            logger.error(f"Tool {tool_name} failed: {e}")
            return jsonify({
                "jsonrpc": "2.0",
                "id": req_id,
                "error": {"code": -32603, "message": f"Internal error: {str(e)}"},
            })

    if method.startswith("notifications/"):
        return "", 204

    return jsonify({
        "jsonrpc": "2.0",
        "id": req_id,
        "error": {"code": -32601, "message": f"Method not found: {method}"},
    })


@app.route("/health", methods=["GET"])
def health():
    return jsonify({
        "status": "ok",
        "service": "agentmesh-agent-a",
        "tools": [t["name"] for t in TOOLS],
    })


if __name__ == "__main__":
    logger.info("Starting Agent A MCP service on http://127.0.0.1:7200")
    logger.info(f"Available tools: {[t['name'] for t in TOOLS]}")
    app.run(host="127.0.0.1", port=7200, debug=False)
