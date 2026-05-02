"""
AgentMesh — Agent C MCP Service (Gemini-Powered Tools)

Flask HTTP server exposing Agent C's Gemini-powered tools over the AXL mesh.
Protected by x402 payment middleware — callers pay $0.001 USDC per call.

Tools:
  - gemini_qa: Ask Gemini a question and get a detailed answer
  - gemini_code_review: Have Gemini review a code snippet

Routing chain:
  Remote node → POST /mcp/{node_c_key}/agent-c
    → Node C forwards to MCP Router (localhost:9023)
    → Router dispatches to this service (localhost:7300/mcp)
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
logger = logging.getLogger("agent-c-server")

# ---------------------------------------------------------------------------
# Flask app
# ---------------------------------------------------------------------------
app = Flask(__name__)

# ---------------------------------------------------------------------------
# x402 payment middleware — protects POST /mcp with $0.001 USDC per call
# ---------------------------------------------------------------------------
EVM_NETWORK: Network = "eip155:84532"  # Base Sepolia testnet
FACILITATOR_URL = "https://x402.org/facilitator"

_pay_to = os.environ.get("AGENT_C_WALLET_ADDRESS")
if not _pay_to:
    raise RuntimeError("AGENT_C_WALLET_ADDRESS env var is required (Agent C's wallet for receiving payments)")

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
        description="Agent C Gemini-powered tools — pay-per-call via x402",
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
        "name": "gemini_qa",
        "description": "Ask Gemini a question and get a detailed, well-structured answer. Good for factual queries, explanations, and research.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "question": {
                    "type": "string",
                    "description": "The question to ask Gemini",
                },
                "context": {
                    "type": "string",
                    "description": "Optional context to help Gemini answer more accurately",
                },
            },
            "required": ["question"],
        },
    },
    {
        "name": "gemini_code_review",
        "description": "Have Gemini review a code snippet for bugs, improvements, security issues, and best practices.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "code": {
                    "type": "string",
                    "description": "The code snippet to review",
                },
                "language": {
                    "type": "string",
                    "description": "Programming language (e.g., python, javascript, go)",
                },
            },
            "required": ["code"],
        },
    },
]

# ---------------------------------------------------------------------------
# Tool implementations
# ---------------------------------------------------------------------------

def tool_gemini_qa(arguments: dict) -> dict:
    """Use Gemini to answer a question."""
    question = arguments.get("question", "")
    context = arguments.get("context", "")

    if not question:
        return {"error": "No question provided"}

    prompt = f"Answer the following question thoroughly but concisely.\n\n"
    if context:
        prompt += f"Context: {context}\n\n"
    prompt += f"Question: {question}"

    try:
        response = gemini_client.models.generate_content(
            model=GEMINI_MODEL,
            contents=prompt,
        )
        return {
            "answer": response.text,
            "question": question,
            "model": GEMINI_MODEL,
        }
    except Exception as e:
        logger.error(f"Gemini QA failed: {e}")
        return {"error": f"Gemini call failed: {str(e)}"}


def tool_gemini_code_review(arguments: dict) -> dict:
    """Use Gemini to review code."""
    code = arguments.get("code", "")
    language = arguments.get("language", "unknown")

    if not code:
        return {"error": "No code provided"}

    prompt = (
        f"Review the following {language} code. Provide:\n"
        f"1) Bugs or issues found\n"
        f"2) Security concerns\n"
        f"3) Performance improvements\n"
        f"4) Best practice suggestions\n"
        f"5) Overall assessment\n\n"
        f"```{language}\n{code}\n```"
    )

    try:
        response = gemini_client.models.generate_content(
            model=GEMINI_MODEL,
            contents=prompt,
        )
        return {
            "review": response.text,
            "language": language,
            "code_length": len(code),
            "model": GEMINI_MODEL,
        }
    except Exception as e:
        logger.error(f"Gemini code review failed: {e}")
        return {"error": f"Gemini call failed: {str(e)}"}


# Tool dispatch table
TOOL_HANDLERS = {
    "gemini_qa": tool_gemini_qa,
    "gemini_code_review": tool_gemini_code_review,
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
                "serverInfo": {"name": "agentmesh-agent-c", "version": "1.0.0"},
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
        "service": "agentmesh-agent-c",
        "tools": [t["name"] for t in TOOLS],
    })


if __name__ == "__main__":
    logger.info("Starting Agent C MCP service on http://127.0.0.1:7300")
    logger.info(f"Available tools: {[t['name'] for t in TOOLS]}")
    app.run(host="127.0.0.1", port=7300, debug=False)
