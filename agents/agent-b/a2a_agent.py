"""
AgentMesh — Agent D: Full A2A Agent Server

This is a COMPLETE A2A agent with three layers working together:

  ┌─────────────────────────────────────────────────────────────┐
  │  LAYER 1 — SELLER (incoming)                                │
  │  Exposes A2A endpoints, guarded by x402 payment middleware  │
  │  Callers MUST pay $0.001 USDC before getting a response     │
  ├─────────────────────────────────────────────────────────────┤
  │  LAYER 2 — BRAIN                                            │
  │  Gemini reads the paid request, decides which upstream      │
  │  services to call (MCP tools or A2A agents) to answer it    │
  ├─────────────────────────────────────────────────────────────┤
  │  LAYER 3 — BUYER (outgoing)                                 │
  │  Built-in MCP client + A2A client, both handle x402 payment │
  │  autonomously — they discover, pay, and call peer services  │
  └─────────────────────────────────────────────────────────────┘

Full request flow:
  Caller → POST / (A2A message/send)
    → x402 middleware challenges for payment
    → Caller pays $0.001 USDC
    → Gemini brain reads the request
    → Brain decides: call MCP tool on Agent B? call A2A on Agent A?
    → MCP client / A2A client makes the upstream call (paying autonomously)
    → Agent D synthesizes all results
    → Returns natural language answer to original caller

Setup (in .env or exported):
  AGENT_D_WALLET_ADDRESS     = "0x..."   # Agent D's wallet — RECEIVES payments
  AGENT_D_EVM_PRIVATE_KEY    = "0x..."   # Agent D's private key — MAKES payments
  GEMINI_API_KEY             = "..."     # Gemini for the reasoning brain
  AGENT_D_AXL_API            = "http://127.0.0.1:9032"  # Agent D's AXL node

Run:
  python3 agents/agent-d/a2a_agent.py

Register with AXL:
  bash scripts/start-agent-d.sh
"""

import json
import re
import os
import sys
import uuid
import logging
from collections import Counter
from pathlib import Path
from dotenv import load_dotenv

load_dotenv(Path(__file__).resolve().parents[2] / ".env")

import requests as std_requests
from flask import Flask, request, jsonify

# x402 — for RECEIVING payments (middleware on incoming requests)
from x402.http import (
    FacilitatorConfig,
    HTTPFacilitatorClientSync,
    PaymentOption,
    x402HTTPClientSync,
)
from x402.http.middleware.flask import payment_middleware
from x402.http.types import RouteConfig
from x402.mechanisms.evm.exact import ExactEvmServerScheme
from x402.schemas import Network
from x402.server import x402ResourceServerSync

# x402 — for MAKING payments (client for outgoing requests)
from x402 import x402ClientSync
from x402.mechanisms.evm import EthAccountSigner
from x402.mechanisms.evm.exact.register import register_exact_evm_client
from eth_account import Account

# Gemini — the reasoning brain
from google import genai
from google.genai import types as genai_types

# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
)
logger = logging.getLogger("agent-b")

# ---------------------------------------------------------------------------
# Validate required env vars
# ---------------------------------------------------------------------------
_REQUIRED = {
    "AGENT_B_WALLET_ADDRESS": "Agent B's EVM address (receives payments)",
    "AGENT_B_EVM_PRIVATE_KEY": "Agent B's private key (makes outgoing payments)",
    "GEMINI_API_KEY": "Gemini API key for the reasoning brain",
}
for var, description in _REQUIRED.items():
    if not os.environ.get(var):
        logger.error(f"Missing required env var: {var} — {description}")
        sys.exit(1)

WALLET_ADDRESS   = os.environ["AGENT_B_WALLET_ADDRESS"]
PRIVATE_KEY      = os.environ["AGENT_B_EVM_PRIVATE_KEY"]
GEMINI_API_KEY   = os.environ["GEMINI_API_KEY"]
AXL_API          = os.environ.get("AGENT_B_AXL_API", "http://127.0.0.1:9002")
A2A_PORT         = int(os.environ.get("AGENT_B_A2A_PORT", "9004"))
EVM_NETWORK: Network = "eip155:84532"   # Base Sepolia testnet
FACILITATOR_URL  = "https://x402.org/facilitator"
GEMINI_MODEL     = "gemini-2.0-flash"

logger.info(f"Agent B wallet (receive): {WALLET_ADDRESS}")
logger.info(f"AXL node: {AXL_API}")

# ---------------------------------------------------------------------------
# ── LAYER 3: BUYER — x402 payment client for OUTGOING calls ──────────────
# ---------------------------------------------------------------------------
# This is what Agent B uses to pay OTHER agents (MCP or A2A) when it
# needs to call upstream services to answer an incoming request.

_account = Account.from_key(PRIVATE_KEY)
_signer = EthAccountSigner(_account)
_x402_client = x402ClientSync()
register_exact_evm_client(_x402_client, _signer)
_x402_http = x402HTTPClientSync(_x402_client)

logger.info(f"Agent B buyer wallet (pay): {_account.address}")

# ---------------------------------------------------------------------------
# ── LAYER 1: SELLER — Flask app + x402 middleware for INCOMING requests ──
# ---------------------------------------------------------------------------
# Every POST / is protected. Callers must pay before getting a response.

app = Flask(__name__)

_facilitator      = HTTPFacilitatorClientSync(FacilitatorConfig(url=FACILITATOR_URL))
_resource_server  = x402ResourceServerSync(_facilitator)
_resource_server.register(EVM_NETWORK, ExactEvmServerScheme())

_routes = {
    "POST /": RouteConfig(
        accepts=[PaymentOption(
            scheme="exact",
            pay_to=WALLET_ADDRESS,
            price="$0.001",
            network=EVM_NETWORK,
        )],
        mime_type="application/json",
        description="Agent D A2A endpoint — pay-per-request via x402",
    )
}

payment_middleware(app, routes=_routes, server=_resource_server)
# Note: GET /.well-known/agent.json and GET /health are NOT in _routes
# so they remain free (discovery should be free, only execution costs)

# ---------------------------------------------------------------------------
# Agent D's identity card
# ---------------------------------------------------------------------------
AGENT_CARD = {
    "name": "Agent D — Orchestrator",
    "description": (
        "I'm Agent D, an orchestrator agent on the AgentMesh network. "
        "Send me any natural language request. I'll figure out which peer agents "
        "and tools to hire, pay for them autonomously, and return a synthesized answer. "
        "Each request costs $0.001 USDC via x402."
    ),
    "version": "1.0.0",
    "skills": [
        {
            "id": "orchestrate",
            "name": "Task Orchestration",
            "description": (
                "I accept any text task and autonomously route it to the right "
                "peer agents — calling MCP tools or A2A agents as needed, paying "
                "for each service myself, and returning a unified answer."
            ),
            "examples": [
                "Analyze the sentiment and summarize this article: ...",
                "Review this code snippet and rewrite it more concisely",
                "Extract keywords and translate this text to Spanish",
                "Ask Agent A to analyze this and get sentiment from Agent B",
            ],
        }
    ],
    "capabilities": {
        "streaming": False,
        "orchestration": True,
        "upstream_protocols": ["mcp", "a2a"],
        "payment": {
            "protocol": "x402",
            "network": EVM_NETWORK,
            "price_incoming": "$0.001 USDC per request (you pay Agent D)",
            "price_outgoing": "variable (Agent D pays peer agents autonomously)",
        },
    },
    "url": f"http://127.0.0.1:{A2A_PORT}",
}

# ---------------------------------------------------------------------------
# ── LAYER 2: BRAIN — Gemini client ────────────────────────────────────────
# ---------------------------------------------------------------------------

_gemini = genai.Client(api_key=GEMINI_API_KEY)

# ---------------------------------------------------------------------------
# Peer discovery — find all peers and their services at startup
# ---------------------------------------------------------------------------
# Maps: service_name → {"peer_key": str, "url": str, "type": "mcp"|"a2a"}
_peer_registry: dict[str, dict] = {}


def _discover_peers():
    """
    Query the AXL topology and probe all peers for MCP services and A2A agents.
    Populates _peer_registry. Called once at startup and can be re-called.
    """
    global _peer_registry
    _peer_registry = {}

    try:
        topology = std_requests.get(f"{AXL_API}/topology", timeout=5).json()
    except Exception as e:
        logger.warning(f"Cannot reach AXL node at {AXL_API}: {e}")
        return

    our_key = topology.get("our_public_key", "")
    peers = topology.get("peers", {})

    peer_keys = []
    if isinstance(peers, dict):
        for k, v in peers.items():
            candidate = v.get("public_key", k) if isinstance(v, dict) else k
            if candidate and candidate != our_key:
                peer_keys.append(candidate)
    elif isinstance(peers, list):
        for p in peers:
            candidate = p.get("public_key", "") if isinstance(p, dict) else str(p)
            if candidate and candidate != our_key:
                peer_keys.append(candidate)

    logger.info(f"Found {len(peer_keys)} peer(s) in AXL topology")

    # Probe known MCP service names on each peer
    MCP_SERVICE_NAMES = ["agentmesh", "agent-a", "agent-b", "agent-c", "agent-d"]

    for peer_key in peer_keys:
        short_key = peer_key[:12]

        # ── Probe MCP services ──
        for service_name in MCP_SERVICE_NAMES:
            url = f"{AXL_API}/mcp/{peer_key}/{service_name}"
            try:
                probe = std_requests.post(
                    url,
                    json={"jsonrpc": "2.0", "method": "tools/list", "id": 0, "params": {}},
                    headers={"Content-Type": "application/json"},
                    timeout=8,
                )
                body = probe.json()
                # Success if we get tools back (or a 402 challenge meaning it exists)
                tools = body.get("result", {}).get("tools", [])
                is_402_challenge = "_x402_challenge" in body
                if tools or is_402_challenge:
                    registry_key = f"mcp:{service_name}@{short_key}"
                    _peer_registry[registry_key] = {
                        "peer_key": peer_key,
                        "service": service_name,
                        "url": url,
                        "type": "mcp",
                        "tools": [t["name"] for t in tools],
                    }
                    logger.info(f"Registered MCP: {registry_key} → tools={[t['name'] for t in tools]}")
            except Exception:
                pass  # service not available on this peer

        # ── Probe A2A agents ──
        a2a_url = f"{AXL_API}/a2a/{peer_key}"
        try:
            card_resp = std_requests.get(a2a_url, timeout=8)
            if card_resp.status_code == 200:
                card = card_resp.json()
                agent_name = card.get("name", f"agent@{short_key}")
                registry_key = f"a2a:{agent_name}@{short_key}"
                _peer_registry[registry_key] = {
                    "peer_key": peer_key,
                    "url": a2a_url,
                    "type": "a2a",
                    "card": card,
                    "name": agent_name,
                }
                logger.info(f"Registered A2A: {registry_key} → {agent_name}")
        except Exception:
            pass

    logger.info(f"Peer registry: {list(_peer_registry.keys())}")


# ---------------------------------------------------------------------------
# ── LAYER 3 IMPLEMENTATION: Outgoing paid MCP client ─────────────────────
# ---------------------------------------------------------------------------

_mcp_call_counter = [0]


def _paid_mcp_call(url: str, method: str, params: dict) -> dict | None:
    """
    Make a paid MCP JSON-RPC call through the AXL mesh.
    Handles x402 payment tunneling autonomously.
    Returns the 'result' field from the JSON-RPC response, or None on error.
    """
    _mcp_call_counter[0] += 1
    call_id = _mcp_call_counter[0]

    payload = {
        "jsonrpc": "2.0",
        "method": method,
        "id": call_id,
        "params": params,
    }

    try:
        resp = std_requests.post(
            url, json=payload,
            headers={"Content-Type": "application/json"},
            timeout=30,
        )
    except Exception as e:
        logger.error(f"MCP call failed: {e}")
        return None

    if resp.status_code != 200:
        logger.error(f"MCP non-200: {resp.status_code} — {resp.text[:200]}")
        return None

    result = resp.json()

    # ── Handle x402 challenge ──────────────────────────────────────────────
    if isinstance(result, dict) and "_x402_challenge" in result:
        challenge = result["_x402_challenge"]
        challenge_headers = challenge.get("headers", {})
        challenge_body = challenge.get("body", {})

        def _get_header(name):
            for k, v in challenge_headers.items():
                if k.lower() == name.lower():
                    return v
            return None

        try:
            payment_required = _x402_http.get_payment_required_response(
                _get_header, challenge_body
            )
            payment_payload = _x402_client.create_payment_payload(payment_required)
            payment_headers = _x402_http.encode_payment_signature_header(payment_payload)

            logger.info(f"MCP x402: paying for call {call_id}")

            retry = {
                "jsonrpc": "2.0",
                "method": method,
                "id": call_id,
                "params": params,
                "_x402_payment": payment_headers,
            }
            resp2 = std_requests.post(
                url, json=retry,
                headers={"Content-Type": "application/json"},
                timeout=30,
            )
            if resp2.status_code != 200:
                logger.error(f"MCP payment retry failed: {resp2.status_code}")
                return None

            result = resp2.json()
            if isinstance(result, dict):
                result.pop("_x402_receipt", None)

            logger.info(f"MCP x402: payment settled for call {call_id}")

        except Exception as e:
            logger.error(f"MCP payment handling failed: {e}")
            return None

    # Still getting a challenge after paying — reject
    if isinstance(result, dict) and "_x402_challenge" in result:
        logger.error("MCP payment not accepted")
        return None

    return result.get("result") if isinstance(result, dict) else None


def _call_mcp_tool(registry_key: str, tool_name: str, arguments: dict) -> str:
    """
    Call a specific tool on a registered MCP service. Returns text result.
    """
    entry = _peer_registry.get(registry_key)
    if not entry:
        return f"[Error: service '{registry_key}' not in registry]"

    result = _paid_mcp_call(
        entry["url"], "tools/call",
        {"name": tool_name, "arguments": arguments},
    )

    if result is None:
        return f"[Error: MCP call to {registry_key}/{tool_name} failed]"

    content = result.get("content", [{}])
    raw_text = content[0].get("text", "{}") if content else "{}"

    try:
        parsed = json.loads(raw_text)
        return json.dumps(parsed, indent=2)
    except Exception:
        return raw_text


# ---------------------------------------------------------------------------
# ── LAYER 3 IMPLEMENTATION: Outgoing paid A2A client ─────────────────────
# ---------------------------------------------------------------------------

_a2a_call_counter = [0]


def _paid_a2a_send(url: str, message_text: str) -> str | None:
    """
    Send a natural language message to a peer A2A agent through the AXL mesh.
    Handles x402 payment tunneling autonomously.
    Returns the agent's reply text, or None on error.
    """
    _a2a_call_counter[0] += 1
    call_id = _a2a_call_counter[0]
    message_id = str(uuid.uuid4())[:8]

    a2a_payload = {
        "jsonrpc": "2.0",
        "method": "message/send",
        "id": call_id,
        "params": {
            "message": {
                "role": "user",
                "parts": [{"kind": "text", "text": message_text}],
                "messageId": message_id,
            }
        },
    }

    try:
        resp = std_requests.post(
            url, json=a2a_payload,
            headers={"Content-Type": "application/json"},
            timeout=30,
        )
    except Exception as e:
        logger.error(f"A2A send failed: {e}")
        return None

    if resp.status_code != 200:
        logger.error(f"A2A non-200: {resp.status_code}")
        return None

    result = resp.json()

    # ── Handle x402 challenge ──────────────────────────────────────────────
    if isinstance(result, dict) and "_x402_challenge" in result:
        challenge = result["_x402_challenge"]
        challenge_headers = challenge.get("headers", {})
        challenge_body = challenge.get("body", {})

        def _get_header(name):
            for k, v in challenge_headers.items():
                if k.lower() == name.lower():
                    return v
            return None

        try:
            payment_required = _x402_http.get_payment_required_response(
                _get_header, challenge_body
            )
            payment_payload = _x402_client.create_payment_payload(payment_required)
            payment_headers = _x402_http.encode_payment_signature_header(payment_payload)

            logger.info(f"A2A x402: paying for message {call_id}")

            retry = {**a2a_payload, "_x402_payment": payment_headers}
            resp2 = std_requests.post(
                url, json=retry,
                headers={"Content-Type": "application/json"},
                timeout=30,
            )
            if resp2.status_code != 200:
                logger.error(f"A2A payment retry failed: {resp2.status_code}")
                return None

            result = resp2.json()
            if isinstance(result, dict):
                result.pop("_x402_receipt", None)

            logger.info(f"A2A x402: payment settled for message {call_id}")

        except Exception as e:
            logger.error(f"A2A payment handling failed: {e}")
            return None

    if isinstance(result, dict) and "_x402_challenge" in result:
        logger.error("A2A payment not accepted")
        return None

    # Extract reply text
    if isinstance(result, dict) and "result" in result:
        parts = result["result"].get("parts", [])
        texts = [p.get("text", "") for p in parts if p.get("kind") == "text"]
        return "\n".join(texts)

    return None


# ---------------------------------------------------------------------------
# ── LAYER 2 IMPLEMENTATION: Gemini brain with tool use ───────────────────
# ---------------------------------------------------------------------------
# Gemini is given the registry of available services as tools.
# It decides what to call, we execute the calls (with payment), feed results
# back to Gemini, and let it produce a final synthesized answer.

def _build_gemini_tools() -> list:
    """
    Build Gemini function declarations from the current peer registry.
    Each registered MCP tool and A2A agent becomes a Gemini-callable function.
    """
    declarations = []

    for registry_key, entry in _peer_registry.items():
        if entry["type"] == "mcp":
            # Fetch tool schemas from MCP service
            mcp_result = _paid_mcp_call(entry["url"], "tools/list", {})
            if not mcp_result:
                continue

            for tool in mcp_result.get("tools", []):
                schema = tool.get("inputSchema", {})
                props = {}
                for prop_name, prop_def in schema.get("properties", {}).items():
                    props[prop_name] = {
                        "type": prop_def.get("type", "string").upper(),
                        "description": prop_def.get("description", ""),
                    }

                fn_name = f"mcp__{registry_key.replace(':', '__').replace('@', '_at_').replace('.', '_')}__{tool['name']}"
                fn_name = re.sub(r'[^a-zA-Z0-9_]', '_', fn_name)[:60]

                declarations.append(genai_types.FunctionDeclaration(
                    name=fn_name,
                    description=f"[MCP:{registry_key}] {tool.get('description', '')}",
                    parameters_json_schema={
                        "type": "object",
                        "properties": props,
                        "required": schema.get("required", []),
                    },
                ))

                # Store mapping so we can route Gemini's call back to MCP
                _GEMINI_TOOL_ROUTES[fn_name] = {
                    "type": "mcp",
                    "registry_key": registry_key,
                    "tool_name": tool["name"],
                }

        elif entry["type"] == "a2a":
            card = entry.get("card", {})
            skills = card.get("skills", [{}])
            skill_desc = skills[0].get("description", "") if skills else ""

            fn_name = f"a2a__{registry_key.replace(':', '__').replace('@', '_at_').replace('.', '_')}"
            fn_name = re.sub(r'[^a-zA-Z0-9_]', '_', fn_name)[:60]

            declarations.append(genai_types.FunctionDeclaration(
                name=fn_name,
                description=f"[A2A:{entry['name']}] {skill_desc or card.get('description', '')}",
                parameters_json_schema={
                    "type": "object",
                    "properties": {
                        "message": {
                            "type": "STRING",
                            "description": (
                                "The natural language message to send to this agent. "
                                "Be specific and include all relevant text/data."
                            ),
                        }
                    },
                    "required": ["message"],
                },
            ))

            _GEMINI_TOOL_ROUTES[fn_name] = {
                "type": "a2a",
                "registry_key": registry_key,
                "url": entry["url"],
            }

    return [genai_types.Tool(function_declarations=declarations)] if declarations else []


# Maps Gemini function name → routing info (built alongside declarations)
_GEMINI_TOOL_ROUTES: dict[str, dict] = {}


def _execute_gemini_function(fn_name: str, fn_args: dict) -> str:
    """
    Execute a function call that Gemini requested.
    Routes to MCP tool or A2A agent and pays autonomously.
    """
    route = _GEMINI_TOOL_ROUTES.get(fn_name)
    if not route:
        return f"[Error: no route found for function '{fn_name}']"

    logger.info(f"Executing Gemini function: {fn_name} → {route['type']}")

    if route["type"] == "mcp":
        return _call_mcp_tool(
            route["registry_key"],
            route["tool_name"],
            fn_args,
        )

    elif route["type"] == "a2a":
        message_text = fn_args.get("message", str(fn_args))
        reply = _paid_a2a_send(route["url"], message_text)
        return reply or f"[Error: A2A agent {route['registry_key']} returned no response]"

    return "[Error: unknown route type]"


def _run_gemini_agentic_loop(user_message: str, sender_id: str, max_iterations: int = 8) -> str:
    """
    The core brain loop.
    Gemini receives the user's request and the available tools/agents.
    It decides what to call, we execute (with payment), feed results back,
    and repeat until Gemini produces a final text answer.
    """
    logger.info(f"Brain loop started for: {user_message[:80]}")

    # Build tools from current registry
    _GEMINI_TOOL_ROUTES.clear()
    gemini_tools = _build_gemini_tools()

    system_instruction = (
        "You are Agent D, an autonomous orchestrator agent in the AgentMesh network. "
        "You receive tasks from paying clients and fulfill them by calling peer agents "
        "and MCP tools — paying for each service yourself. "
        "\n\n"
        "Your job:\n"
        "1. Understand the client's request\n"
        "2. Call the appropriate peer tools or agents to gather the information needed\n"
        "3. Synthesize all results into a single, clear, natural language answer\n"
        "4. Be concise and direct — the client paid for a quality answer\n"
        "\n"
        f"Client sender ID: {sender_id}"
    )

    conversation = [
        genai_types.Content(
            role="user",
            parts=[genai_types.Part.from_text(text=user_message)],
        )
    ]

    for iteration in range(1, max_iterations + 1):
        logger.info(f"Brain iteration {iteration}/{max_iterations}")

        try:
            response = _gemini.models.generate_content(
                model=GEMINI_MODEL,
                contents=conversation,
                config=genai_types.GenerateContentConfig(
                    system_instruction=system_instruction,
                    tools=gemini_tools if gemini_tools else None,
                    automatic_function_calling=genai_types.AutomaticFunctionCallingConfig(
                        disable=True
                    ),
                    temperature=0.2,
                ),
            )
        except Exception as e:
            logger.error(f"Gemini call failed: {e}")
            return f"I encountered an error while processing your request: {e}"

        fn_calls = response.function_calls

        if fn_calls:
            # Gemini wants to call one or more tools/agents
            logger.info(f"Gemini requested {len(fn_calls)} function call(s)")
            conversation.append(response.candidates[0].content)

            function_response_parts = []
            for fc in fn_calls:
                fn_name = fc.name
                fn_args = dict(fc.args) if fc.args else {}

                logger.info(f"  Calling: {fn_name}({list(fn_args.keys())})")
                result_text = _execute_gemini_function(fn_name, fn_args)
                logger.info(f"  Result: {result_text[:80]}...")

                function_response_parts.append(
                    genai_types.Part.from_function_response(
                        name=fn_name,
                        response={"result": result_text},
                    )
                )

            conversation.append(
                genai_types.Content(role="tool", parts=function_response_parts)
            )

        else:
            # Gemini produced a final text answer — we're done
            final_answer = response.text or "(No response generated)"
            logger.info(f"Brain loop complete after {iteration} iteration(s)")
            return final_answer

    # Hit max iterations — return whatever Gemini last said
    last_text = response.text if hasattr(response, "text") and response.text else (
        "I reached my processing limit. Here is what I gathered so far from peer agents."
    )
    return last_text


# ---------------------------------------------------------------------------
# ── LAYER 1 IMPLEMENTATION: A2A endpoints ────────────────────────────────
# ---------------------------------------------------------------------------

@app.route("/.well-known/agent.json", methods=["GET"])
def agent_card_endpoint():
    """
    Agent D's public business card. FREE — no payment required.
    Any agent can call this to discover what Agent D offers and how to pay.
    """
    return jsonify(AGENT_CARD)


@app.route("/", methods=["POST"])
def handle_a2a():
    """
    Main A2A message endpoint. PAID — x402 middleware enforces $0.001 USDC.

    The middleware runs BEFORE this function. By the time we get here,
    the payment has already been verified. We just process the request.

    Accepts A2A protocol format:
    {
        "jsonrpc": "2.0",
        "method": "message/send",
        "id": 1,
        "params": {
            "message": {
                "role": "user",
                "parts": [{"kind": "text", "text": "...your request..."}],
                "messageId": "msg-001"
            }
        }
    }
    """
    req = request.get_json(silent=True)
    if not req:
        return jsonify({
            "jsonrpc": "2.0", "id": None,
            "error": {"code": -32700, "message": "Parse error: invalid JSON"},
        }), 400

    method  = req.get("method", "")
    req_id  = req.get("id")
    params  = req.get("params", {})

    # ── agent/info via JSON-RPC ──
    if method == "agent/info":
        return jsonify({
            "jsonrpc": "2.0", "id": req_id,
            "result": AGENT_CARD,
        })

    # ── Only handle message/send ──
    if method != "message/send":
        return jsonify({
            "jsonrpc": "2.0", "id": req_id,
            "error": {"code": -32601, "message": f"Method not found: {method}"},
        })

    # ── Extract message text ──
    message     = params.get("message", {})
    parts       = message.get("parts", [])
    message_id  = message.get("messageId", str(uuid.uuid4())[:8])
    text_parts  = [p.get("text", "") for p in parts if p.get("kind") == "text"]

    if not text_parts:
        return jsonify({
            "jsonrpc": "2.0", "id": req_id,
            "error": {"code": -32602, "message": "No text part found in message"},
        })

    full_message = " ".join(text_parts)
    sender_id    = request.headers.get("X-From-Peer-Id", "unknown")

    logger.info(f"[PAID] A2A request from {sender_id[:16]}: {full_message[:80]}")

    # ── LAYER 2: Brain processes the paid request ──────────────────────────
    # Gemini decides what to call, Layer 3 clients make the paid calls.
    reply_text = _run_gemini_agentic_loop(full_message, sender_id)

    logger.info(f"Sending reply ({len(reply_text)} chars) to {sender_id[:16]}")

    # ── Return A2A formatted response ──
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
                "agent": "agent-d",
                "version": "1.0.0",
                "peers_used": list(_peer_registry.keys()),
            },
        },
    })


@app.route("/health", methods=["GET"])
def health():
    """Health check. FREE — no payment required."""
    return jsonify({
        "status": "ok",
        "service": "agentmesh-agent-d",
        "version": "1.0.0",
        "wallet_receive": WALLET_ADDRESS,
        "wallet_pay": _account.address,
        "axl_node": AXL_API,
        "peers_registered": len(_peer_registry),
        "peer_services": list(_peer_registry.keys()),
        "model": GEMINI_MODEL,
    })


@app.route("/registry/refresh", methods=["POST"])
def refresh_registry():
    """
    Re-discover peers and refresh the registry. FREE — admin endpoint.
    Call this if new agents join the mesh after Agent D started.
    """
    _discover_peers()
    return jsonify({
        "status": "ok",
        "peers_registered": len(_peer_registry),
        "peer_services": list(_peer_registry.keys()),
    })


# ---------------------------------------------------------------------------
# Startup
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    logger.info("=" * 60)
    logger.info("  AgentMesh — Agent D (Full A2A Orchestrator)")
    logger.info("=" * 60)
    logger.info(f"  Receives payments at: {WALLET_ADDRESS}")
    logger.info(f"  Makes payments from:  {_account.address}")
    logger.info(f"  AXL node:             {AXL_API}")
    logger.info(f"  A2A port:             {A2A_PORT}")
    logger.info(f"  Gemini model:         {GEMINI_MODEL}")
    logger.info("")

    # Discover peers before accepting requests
    logger.info("Discovering peers...")
    _discover_peers()

    if not _peer_registry:
        logger.warning(
            "No peers found yet — Agent D will run but Gemini will have no tools. "
            "POST /registry/refresh to re-discover once peers are up."
        )

    logger.info(f"Starting A2A server on http://127.0.0.1:{A2A_PORT}")
    logger.info("  GET  /.well-known/agent.json  → free (agent card)")
    logger.info("  POST /                         → PAID via x402 ($0.001 USDC)")
    logger.info("  GET  /health                   → free")
    logger.info("  POST /registry/refresh         → free (re-discover peers)")

    app.run(host="127.0.0.1", port=A2A_PORT, debug=False)