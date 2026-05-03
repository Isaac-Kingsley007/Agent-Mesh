#!/usr/bin/env python3
"""
Agent A — Autonomous agent for AgentMesh (Gemini-powered agentic loop).
Discovers and hires tools from ALL peers on the mesh, paying via x402.

x402 payment is tunneled through the JSON-RPC body because the AXL mesh
transports only JSON envelopes over TCP (no HTTP headers/status codes).

Setup:
  export AGENT_A_EVM_PRIVATE_KEY="0x..."   # wallet with USDC on Base Sepolia
  export GEMINI_API_KEY="..."              # Gemini API key for LLM reasoning

Run:
  python3 agents/agent-a/agent.py
  python3 agents/agent-a/agent.py "Analyze this text and review this code: ..."
"""

import os
import sys
import json
from pathlib import Path
from dotenv import load_dotenv

# Load .env from project root
load_dotenv(Path(__file__).resolve().parents[2] / ".env")

import requests as std_requests
from eth_account import Account
from x402 import x402ClientSync
from x402.http import x402HTTPClientSync
from x402.mechanisms.evm import EthAccountSigner
from x402.mechanisms.evm.exact.register import register_exact_evm_client

from google import genai
from google.genai import types

# ── 1. Validate env vars ──────────────────────────────────────────────────
PRIVATE_KEY = os.environ.get("AGENT_A_EVM_PRIVATE_KEY")
if not PRIVATE_KEY:
    print("ERROR: AGENT_A_EVM_PRIVATE_KEY is not set")
    sys.exit(1)

GEMINI_API_KEY = os.environ.get("GEMINI_API_KEY")
if not GEMINI_API_KEY:
    print("ERROR: GEMINI_API_KEY is not set")
    sys.exit(1)

# ── 2. Set up x402 payment client ────────────────────────────────────────
account = Account.from_key(PRIVATE_KEY)
signer = EthAccountSigner(account)
x402_client = x402ClientSync()
register_exact_evm_client(x402_client, signer)
x402_http = x402HTTPClientSync(x402_client)

# ── 3. Set up Gemini client ──────────────────────────────────────────────
GEMINI_MODEL = "gemini-3-flash-preview"
gemini_client = genai.Client(api_key=GEMINI_API_KEY)

AXL_API = "http://127.0.0.1:9002"

print("=" * 60)
print("  AgentMesh — Agent A (Autonomous, Gemini-Powered)")
print("=" * 60)
print(f"  Wallet:    {account.address}")
print(f"  LLM:       {GEMINI_MODEL}")
print(f"  AXL Node:  {AXL_API}")
print()

# ── 4. Discover all peers ────────────────────────────────────────────────
print("Discovering peers via AXL topology...")
try:
    topology = std_requests.get(f"{AXL_API}/topology", timeout=5).json()
except Exception as e:
    print(f"ERROR: Cannot reach AXL Node A at {AXL_API}: {e}")
    sys.exit(1)

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

if not peer_keys:
    print("ERROR: No peers found in topology")
    sys.exit(1)

print(f"  Found {len(peer_keys)} peer(s)")
for pk in peer_keys:
    print(f"    • {pk[:16]}...")
print()

# ── 5. Known service names per peer type ─────────────────────────────────
# We try these service names on each peer to discover available tools
SERVICE_NAMES = ["agentmesh", "agent-a", "agent-c"]

# ── 6. x402 tunneled payment helper ──────────────────────────────────────

_call_counter = 0
# Maps tool_name → (peer_key, service_name) for routing
_tool_routing = {}


def paid_mcp_call(mesh_url, method, params, label):
    """Make an MCP call through the AXL mesh with x402 payment tunneling."""
    global _call_counter
    _call_counter += 1
    call_id = _call_counter

    print(f"── Call {call_id}: {label} {'─' * max(1, 43 - len(label))}")
    payload = {"jsonrpc": "2.0", "method": method, "id": call_id, "params": params}

    try:
        resp = std_requests.post(
            mesh_url,
            json=payload,
            headers={"Content-Type": "application/json"},
            timeout=30,
        )
    except Exception as e:
        print(f"  ERROR: Request failed: {e}")
        return None

    print(f"  HTTP status:      {resp.status_code}")

    if resp.status_code != 200:
        print(f"  ERROR: Non-200 from mesh: {resp.text[:200]}")
        print()
        return None

    result = resp.json()

    # ── x402 payment challenge handling ──
    if isinstance(result, dict) and "_x402_challenge" in result:
        challenge = result["_x402_challenge"]
        print(f"  x402 challenge:   Payment required")

        challenge_headers = challenge.get("headers", {})
        challenge_body = challenge.get("body", {})

        def get_header(name):
            for k, v in challenge_headers.items():
                if k.lower() == name.lower():
                    return v
            return None

        try:
            payment_required = x402_http.get_payment_required_response(
                get_header, challenge_body
            )
            payment_payload = x402_client.create_payment_payload(payment_required)
            payment_headers = x402_http.encode_payment_signature_header(payment_payload)

            if not payment_headers:
                print(f"  ERROR: No payment signature created")
                return None

            print(f"  Payment signed:   ✓ (tunneling through mesh)")

            retry_payload = {
                "jsonrpc": "2.0",
                "method": method,
                "id": call_id,
                "params": params,
                "_x402_payment": payment_headers,
            }

            resp2 = std_requests.post(
                mesh_url,
                json=retry_payload,
                headers={"Content-Type": "application/json"},
                timeout=30,
            )

            if resp2.status_code != 200:
                print(f"  ERROR: Retry failed with status {resp2.status_code}")
                return None

            result = resp2.json()
            print(f"  Payment settled:  ✓ USDC paid via x402")

            if isinstance(result, dict) and "_x402_receipt" in result:
                receipt_raw = result.pop("_x402_receipt")
                try:
                    receipt_data = json.loads(receipt_raw) if isinstance(receipt_raw, str) else receipt_raw
                    tx = receipt_data.get("transaction") or receipt_data.get("txHash", "")
                    if tx:
                        print(f"  Tx hash:          {str(tx)[:20]}...")
                except Exception:
                    print(f"  Receipt:          {str(receipt_raw)[:60]}...")

        except Exception as e:
            print(f"  ERROR: Payment handling failed: {e}")
            import traceback
            traceback.print_exc()
            return None

    if isinstance(result, dict) and "_x402_challenge" in result:
        print(f"  ERROR: Payment was not accepted")
        return None

    # ── Extract and display result ──
    if isinstance(result, dict):
        content = result.get("result", {})
        if method == "tools/list":
            tools = content.get("tools", [])
            print(f"  Tools available:  {[t['name'] for t in tools]}")
        elif method == "tools/call":
            tool_content = content.get("content", [{}])
            if tool_content:
                tool_result = tool_content[0].get("text", "")
                try:
                    parsed = json.loads(tool_result) if tool_result else {}
                    print(f"  Tool result:      {json.dumps(parsed, indent=None)[:120]}")
                except Exception:
                    print(f"  Tool result:      {tool_result[:120]}")
        elif "error" in result:
            print(f"  JSON-RPC error:   {result['error']}")
        print()
        return content
    else:
        print(f"  Response:         {resp.text[:120]}")
        print()
        return None


# ── 7. Discover tools from all peers and services ────────────────────────

print("=" * 60)
print("  Phase 1: Discovering tools from all peers")
print("=" * 60)
print()

all_mcp_tools = []

for peer_key in peer_keys:
    for service_name in SERVICE_NAMES:
        mesh_url = f"{AXL_API}/mcp/{peer_key}/{service_name}"
        print(f"  Probing {peer_key[:12]}.../{service_name}")

        tools_result = paid_mcp_call(
            mesh_url, "tools/list", {},
            f"Discover {service_name}@{peer_key[:8]}",
        )

        if tools_result and "tools" in tools_result:
            for tool in tools_result["tools"]:
                tool_name = tool["name"]
                if tool_name not in _tool_routing:
                    _tool_routing[tool_name] = (peer_key, service_name)
                    all_mcp_tools.append(tool)
                    print(f"    ✓ Registered: {tool_name}")

if not all_mcp_tools:
    print("ERROR: No tools discovered from any peer")
    sys.exit(1)

# ── Build Gemini function declarations ──
gemini_function_decls = []
for tool in all_mcp_tools:
    schema = tool.get("inputSchema", {})
    params_schema = {
        "type": schema.get("type", "object"),
        "properties": {},
        "required": schema.get("required", []),
    }
    for prop_name, prop_def in schema.get("properties", {}).items():
        param_type = prop_def.get("type", "string").upper()
        params_schema["properties"][prop_name] = {
            "type": param_type,
            "description": prop_def.get("description", ""),
        }

    decl = types.FunctionDeclaration(
        name=tool["name"],
        description=tool.get("description", ""),
        parameters_json_schema=params_schema,
    )
    gemini_function_decls.append(decl)

gemini_tools = [types.Tool(function_declarations=gemini_function_decls)]

print()
print(f"  Total tools discovered: {len(gemini_function_decls)}")
for d in gemini_function_decls:
    peer_key, svc = _tool_routing[d.name]
    print(f"    • {d.name} → {svc}@{peer_key[:12]}...")
print()


# ── 8. Agentic loop ─────────────────────────────────────────────────────

DEFAULT_GOAL = (
    "I need you to do a comprehensive analysis. First, summarize this text and extract keywords: "
    "'Autonomous agents are transforming software. They can reason, plan, and pay for services "
    "without human intervention. KeeperHub provides the execution layer for onchain transactions. "
    "x402 is the payment protocol for agent-to-agent commerce.' "
    "Then analyze the sentiment. Finally, rewrite the text in a casual style."
)

user_goal = " ".join(sys.argv[1:]) if len(sys.argv) > 1 else DEFAULT_GOAL

SYSTEM_INSTRUCTION = (
    "You are Agent A, an autonomous AI agent in the AgentMesh network. "
    "You have access to paid tools from multiple agents on the mesh. "
    "Use the available tools to accomplish the user's goal. "
    "You can call multiple tools as needed — each call costs $0.001 USDC. "
    "When you have gathered enough information, synthesize a final answer. "
    "Be concise but thorough."
)

MAX_ITERATIONS = 10

print("=" * 60)
print("  Phase 2: Agentic Loop (Gemini-Powered)")
print("=" * 60)
print(f"  Goal: {user_goal[:80]}{'...' if len(user_goal) > 80 else ''}")
print()

conversation = [
    types.Content(role="user", parts=[types.Part.from_text(text=user_goal)])
]

for iteration in range(1, MAX_ITERATIONS + 1):
    print(f"─── Iteration {iteration}/{MAX_ITERATIONS} ─────────────────────────────────")

    try:
        response = gemini_client.models.generate_content(
            model=GEMINI_MODEL,
            contents=conversation,
            config=types.GenerateContentConfig(
                system_instruction=SYSTEM_INSTRUCTION,
                tools=gemini_tools,
                automatic_function_calling=types.AutomaticFunctionCallingConfig(
                    disable=True
                ),
                temperature=0.2,
            ),
        )
    except Exception as e:
        print(f"  ERROR: Gemini call failed: {e}")
        break

    function_calls = response.function_calls
    if function_calls:
        print(f"  Gemini requested {len(function_calls)} tool call(s)")
        conversation.append(response.candidates[0].content)

        function_response_parts = []
        for fc in function_calls:
            tool_name = fc.name
            tool_args = dict(fc.args) if fc.args else {}
            print(f"  → Calling tool: {tool_name}")

            # Route to correct peer/service
            if tool_name in _tool_routing:
                peer_key, service_name = _tool_routing[tool_name]
                mesh_url = f"{AXL_API}/mcp/{peer_key}/{service_name}"
            else:
                print(f"    WARNING: Unknown tool routing for {tool_name}")
                function_response_parts.append(
                    types.Part.from_function_response(
                        name=tool_name,
                        response={"result": {"error": f"No route for tool {tool_name}"}},
                    )
                )
                continue

            mcp_result = paid_mcp_call(
                mesh_url,
                "tools/call",
                {"name": tool_name, "arguments": tool_args},
                f"Gemini→{tool_name}",
            )

            if mcp_result:
                tool_content = mcp_result.get("content", [{}])
                result_text = tool_content[0].get("text", "{}") if tool_content else "{}"
                try:
                    result_data = json.loads(result_text)
                except Exception:
                    result_data = {"raw": result_text}
            else:
                result_data = {"error": "Tool call failed"}

            function_response_parts.append(
                types.Part.from_function_response(
                    name=tool_name,
                    response={"result": result_data},
                )
            )

        conversation.append(
            types.Content(role="tool", parts=function_response_parts)
        )

    else:
        final_text = response.text or "(no text in response)"
        print(f"  Gemini produced final answer")
        print()
        print("=" * 60)
        print("  ✓ Agent A — Final Answer")
        print("=" * 60)
        print()
        print(final_text)
        print()
        break
else:
    print(f"  WARNING: Reached max iterations ({MAX_ITERATIONS})")
    if response and response.text:
        print(response.text)

print("=" * 60)
print(f"  ✓ Agent A completed — {_call_counter} paid MCP calls made")
print("  Payments settled on Base Sepolia via KeeperHub x402")
print("  Communication routed through AXL P2P mesh")
print("=" * 60)
