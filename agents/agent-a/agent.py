#!/usr/bin/env python3
"""
Agent A — Autonomous buyer agent for AgentMesh (Gemini-powered agentic loop).
Pays Agent B via x402 (KeeperHub ecosystem) through the AXL mesh.

x402 payment is tunneled through the JSON-RPC body because the AXL mesh
transports only JSON envelopes over TCP (no HTTP headers/status codes).
The MCP Router on Node B extracts payment signatures from the body and
injects them as HTTP headers before forwarding to Agent B's Flask server.

Setup:
  export AGENT_A_EVM_PRIVATE_KEY="0x..."   # wallet with USDC on Base Sepolia
  export GEMINI_API_KEY="..."              # Gemini API key for LLM reasoning

Run:
  python3 agents/agent-a/agent.py
  python3 agents/agent-a/agent.py "Analyze this text for sentiment and keywords: ..."
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

# ── 2. Set up x402 payment client (KeeperHub agentic wallet ecosystem) ───
account = Account.from_key(PRIVATE_KEY)
signer = EthAccountSigner(account)
x402_client = x402ClientSync()
register_exact_evm_client(x402_client, signer)

# Wrap in HTTP client for header parsing/encoding
x402_http = x402HTTPClientSync(x402_client)

# ── 3. Set up Gemini client ──────────────────────────────────────────────
GEMINI_MODEL = "gemini-3-flash-preview"
gemini_client = genai.Client(api_key=GEMINI_API_KEY)

print("=" * 60)
print("  AgentMesh — Agent A (Autonomous Buyer, Gemini-Powered)")
print("=" * 60)
print(f"  Agent A wallet:  {account.address}")
print(f"  Payment method:  x402 on Base Sepolia (KeeperHub ecosystem)")
print(f"  LLM engine:      {GEMINI_MODEL}")
print(f"  Price per call:  $0.001 USDC")
print()

# ── 4. Discover Node B's public key from Node A's topology ───────────────
print("Discovering Node B via AXL topology...")
try:
    topology = std_requests.get("http://127.0.0.1:9002/topology", timeout=5).json()
except Exception as e:
    print(f"ERROR: Cannot reach AXL Node A at port 9002: {e}")
    print("  Make sure Node A is running: cd axl && ./node -config node-config.json")
    sys.exit(1)

our_key = topology.get("our_public_key", "")
peers = topology.get("peers", {})

node_b_key = None
if isinstance(peers, dict):
    for k, v in peers.items():
        candidate = v.get("public_key", k) if isinstance(v, dict) else k
        if candidate and candidate != our_key:
            node_b_key = candidate
            break
elif isinstance(peers, list):
    for p in peers:
        candidate = p.get("public_key", "") if isinstance(p, dict) else str(p)
        if candidate and candidate != our_key:
            node_b_key = candidate
            break

if not node_b_key:
    print("ERROR: Node B not found in topology")
    print("  Make sure Node B is running: cd axl && ./node -config node-config-2.json")
    sys.exit(1)

MESH_URL = f"http://127.0.0.1:9002/mcp/{node_b_key}/agentmesh"
print(f"  Node B key:  {node_b_key[:16]}...")
print(f"  Mesh URL:    {MESH_URL}")
print()

# ── 5. x402 tunneled payment helper ──────────────────────────────────────

_call_counter = 0

def paid_mcp_call(method, params, label):
    """Make an MCP call through the AXL mesh with x402 payment tunneling.

    Returns the parsed JSON-RPC result dict, or None on failure.

    Flow:
      1. Send JSON-RPC request through mesh
      2. If response contains _x402_challenge → payment required
      3. Create payment payload, embed in body as _x402_payment
      4. Retry through mesh — MCP Router extracts it as HTTP header
      5. Agent B's x402 middleware verifies and processes the request
    """
    global _call_counter
    _call_counter += 1
    call_id = _call_counter

    print(f"── Call {call_id}: {label} {'─' * max(1, 43 - len(label))}")
    payload = {"jsonrpc": "2.0", "method": method, "id": call_id, "params": params}

    try:
        resp = std_requests.post(
            MESH_URL,
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

    # ── Check for x402 payment challenge tunneled through the mesh ──
    if isinstance(result, dict) and "_x402_challenge" in result:
        challenge = result["_x402_challenge"]
        print(f"  x402 challenge:   Payment required (status {challenge.get('status', '?')})")

        # Parse payment requirements from the tunneled 402 response
        challenge_headers = challenge.get("headers", {})
        challenge_body = challenge.get("body", {})

        def get_header(name):
            """Case-insensitive header lookup from tunneled headers."""
            for k, v in challenge_headers.items():
                if k.lower() == name.lower():
                    return v
            return None

        try:
            # Use x402 client to parse payment requirements and create payload
            payment_required = x402_http.get_payment_required_response(
                get_header, challenge_body
            )
            payment_payload = x402_client.create_payment_payload(payment_required)
            payment_headers = x402_http.encode_payment_signature_header(payment_payload)

            if not payment_headers:
                print(f"  ERROR: No payment signature created")
                print()
                return None

            # Tunnel the full header dict so the MCP Router knows the
            # correct header name (PAYMENT-SIGNATURE for V2, X-PAYMENT for V1)
            payment_tunnel = payment_headers

            print(f"  Payment signed:   ✓ (tunneling through mesh)")

            # Retry with payment headers embedded in the JSON body
            retry_payload = {
                "jsonrpc": "2.0",
                "method": method,
                "id": call_id,
                "params": params,
                "_x402_payment": payment_tunnel,
            }

            resp2 = std_requests.post(
                MESH_URL,
                json=retry_payload,
                headers={"Content-Type": "application/json"},
                timeout=30,
            )

            if resp2.status_code != 200:
                print(f"  ERROR: Retry failed with status {resp2.status_code}")
                print(f"  Body: {resp2.text[:200]}")
                print()
                return None

            result = resp2.json()
            print(f"  Payment settled:  ✓ USDC paid via x402")

            # Check for payment receipt tunneled back
            if isinstance(result, dict) and "_x402_receipt" in result:
                receipt_raw = result.pop("_x402_receipt")
                try:
                    receipt_data = json.loads(receipt_raw) if isinstance(receipt_raw, str) else receipt_raw
                    tx = (
                        receipt_data.get("transaction")
                        or receipt_data.get("txHash", "")
                    )
                    if tx:
                        print(f"  Tx hash:          {str(tx)[:20]}...")
                except Exception:
                    print(f"  Receipt:          {str(receipt_raw)[:60]}...")

        except Exception as e:
            print(f"  ERROR: Payment handling failed: {e}")
            import traceback
            traceback.print_exc()
            print()
            return None

    # ── Check for another x402 challenge (payment rejected on retry) ──
    if isinstance(result, dict) and "_x402_challenge" in result:
        print(f"  ERROR: Payment was not accepted (still getting 402)")
        print()
        return None

    # ── Return result ──
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
        else:
            print(f"  Response:         {json.dumps(result, indent=None)[:120]}")
        print()
        return content
    else:
        print(f"  Response:         {resp.text[:120]}")
        print()
        return None


# ── 6. Discover tools and build Gemini function declarations ─────────────

print("=" * 60)
print("  Phase 1: Discovering Agent B's tools")
print("=" * 60)
print()

tools_result = paid_mcp_call("tools/list", {}, "Discover available tools")
if not tools_result or "tools" not in tools_result:
    print("ERROR: Could not discover tools from Agent B")
    sys.exit(1)

mcp_tools = tools_result["tools"]

# Convert MCP tool schemas → Gemini FunctionDeclaration objects
gemini_function_decls = []
for tool in mcp_tools:
    schema = tool.get("inputSchema", {})
    # Build a clean JSON schema for Gemini parameters
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

print(f"  Registered {len(gemini_function_decls)} tools with Gemini:")
for d in gemini_function_decls:
    print(f"    • {d.name}")
print()


# ── 7. Agentic loop ─────────────────────────────────────────────────────

DEFAULT_GOAL = (
    "Analyze the following text for me — give me a summary, sentiment analysis, "
    "and extract the key keywords. Here is the text: "
    "'Autonomous agents are transforming software. They can now reason, plan, "
    "and pay for services without human intervention. KeeperHub provides the "
    "execution layer that guarantees transactions land onchain despite gas spikes "
    "and network congestion. x402 is the payment protocol that makes "
    "agent-to-agent commerce possible. The future of AI is autonomous, "
    "trustless, and decentralized.'"
)

# Accept goal from CLI or use default
user_goal = " ".join(sys.argv[1:]) if len(sys.argv) > 1 else DEFAULT_GOAL

SYSTEM_INSTRUCTION = (
    "You are Agent A, an autonomous AI agent in the AgentMesh network. "
    "You have access to paid tools served by Agent B over the mesh. "
    "Use the available tools to accomplish the user's goal. "
    "You can call multiple tools as needed. "
    "When you have gathered enough information, synthesize a final answer. "
    "Be concise but thorough in your final response."
)

MAX_ITERATIONS = 10

print("=" * 60)
print("  Phase 2: Agentic Loop (Gemini-Powered)")
print("=" * 60)
print(f"  Goal: {user_goal[:80]}{'...' if len(user_goal) > 80 else ''}")
print(f"  Max iterations: {MAX_ITERATIONS}")
print()

# Build initial conversation
conversation = [
    types.Content(role="user", parts=[types.Part.from_text(text=user_goal)])
]

for iteration in range(1, MAX_ITERATIONS + 1):
    print(f"─── Iteration {iteration}/{MAX_ITERATIONS} ─────────────────────────────────")

    # Call Gemini with tools (manual function calling — no automatic FC)
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

    # Check if Gemini wants to call functions
    function_calls = response.function_calls
    if function_calls:
        print(f"  Gemini requested {len(function_calls)} tool call(s)")

        # Add Gemini's response (with function_call parts) to conversation
        conversation.append(response.candidates[0].content)

        # Execute each function call as a paid MCP call
        function_response_parts = []
        for fc in function_calls:
            tool_name = fc.name
            tool_args = dict(fc.args) if fc.args else {}
            print(f"  → Calling tool: {tool_name}({json.dumps(tool_args)[:80]})")

            # Make the paid MCP call through the mesh
            mcp_result = paid_mcp_call(
                "tools/call",
                {"name": tool_name, "arguments": tool_args},
                f"Gemini→{tool_name}",
            )

            # Extract the text result from MCP response
            if mcp_result:
                tool_content = mcp_result.get("content", [{}])
                result_text = tool_content[0].get("text", "{}") if tool_content else "{}"
                try:
                    result_data = json.loads(result_text)
                except Exception:
                    result_data = {"raw": result_text}
            else:
                result_data = {"error": "Tool call failed"}

            # Build function response part for Gemini
            function_response_parts.append(
                types.Part.from_function_response(
                    name=tool_name,
                    response={"result": result_data},
                )
            )

        # Add function responses to conversation
        conversation.append(
            types.Content(role="tool", parts=function_response_parts)
        )

    else:
        # Gemini returned a text answer — we're done
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
    # Try to get whatever Gemini last said
    if response and response.text:
        print(response.text)

print("=" * 60)
print(f"  ✓ Agent A completed — {_call_counter} paid MCP calls made")
print("  All payments settled on Base Sepolia via KeeperHub ecosystem")
print("  All communication routed through AXL P2P mesh")
print("=" * 60)
