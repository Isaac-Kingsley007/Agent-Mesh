#!/usr/bin/env python3
"""
Agent A — Autonomous buyer agent for AgentMesh.
Pays Agent B via x402 (KeeperHub ecosystem) through the AXL mesh.

x402 payment is tunneled through the JSON-RPC body because the AXL mesh
transports only JSON envelopes over TCP (no HTTP headers/status codes).
The MCP Router on Node B extracts payment signatures from the body and
injects them as HTTP headers before forwarding to Agent B's Flask server.

Setup:
  export AGENT_A_EVM_PRIVATE_KEY="0x..."   # wallet with USDC on Base Sepolia
  export AGENT_B_WALLET_ADDRESS="0x..."    # KeeperHub creator wallet (informational)

Run:
  python3 agents/agent-a/agent.py
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

# ── 1. Validate env vars ──────────────────────────────────────────────────
PRIVATE_KEY = os.environ.get("AGENT_A_EVM_PRIVATE_KEY")
if not PRIVATE_KEY:
    print("ERROR: AGENT_A_EVM_PRIVATE_KEY is not set")
    print("  Generate a wallet: python3 -c \"from eth_account import Account; import secrets; k='0x'+secrets.token_hex(32); a=Account.from_key(k); print('Address:',a.address); print('Key:',k)\"")
    print("  Fund it with test USDC at https://faucet.circle.com (Base Sepolia)")
    sys.exit(1)

# ── 2. Set up x402 payment client (KeeperHub agentic wallet ecosystem) ───
account = Account.from_key(PRIVATE_KEY)
signer = EthAccountSigner(account)
x402_client = x402ClientSync()
register_exact_evm_client(x402_client, signer)

# Wrap in HTTP client for header parsing/encoding
x402_http = x402HTTPClientSync(x402_client)

print("=" * 55)
print("  AgentMesh — Agent A (Buyer)")
print("=" * 55)
print(f"  Agent A wallet:  {account.address}")
print(f"  Payment method:  x402 on Base Sepolia (KeeperHub ecosystem)")
print(f"  Price per call:  $0.001 USDC")
print()

# ── 3. Discover Node B's public key from Node A's topology ───────────────
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

# ── 4. x402 tunneled payment helper ──────────────────────────────────────

def paid_mcp_call(method, params, call_id, label):
    """Make an MCP call through the AXL mesh with x402 payment tunneling.

    Flow:
      1. Send JSON-RPC request through mesh
      2. If response contains _x402_challenge → payment required
      3. Create payment payload, embed in body as _x402_payment
      4. Retry through mesh — MCP Router extracts it as HTTP header
      5. Agent B's x402 middleware verifies and processes the request
    """
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
        return

    print(f"  HTTP status:      {resp.status_code}")

    if resp.status_code != 200:
        print(f"  ERROR: Non-200 from mesh: {resp.text[:200]}")
        print()
        return

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
                return

            # Tunnel the full header dict so the MCP Router knows the
            # correct header name (PAYMENT-SIGNATURE for V2, X-PAYMENT for V1)
            payment_tunnel = payment_headers  # e.g. {"X-PAYMENT": "base64..."}

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
                return

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
            return

    # ── Check for another x402 challenge (payment rejected on retry) ──
    if isinstance(result, dict) and "_x402_challenge" in result:
        print(f"  ERROR: Payment was not accepted (still getting 402)")
        print()
        return

    # ── Display results ──
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
    else:
        print(f"  Response:         {resp.text[:120]}")
    print()


# ── 5. Execute paid MCP calls through the AXL mesh ──────────────────────

# Call 1: Discover tools
paid_mcp_call("tools/list", {}, 1, "Discover available tools")

# Call 2: Summarize
paid_mcp_call(
    "tools/call",
    {"name": "summarize", "arguments": {
        "text": (
            "Autonomous agents are transforming software. "
            "They can now reason, plan, and pay for services without human intervention. "
            "KeeperHub provides the execution layer that guarantees transactions land "
            "onchain despite gas spikes and network congestion. "
            "x402 is the payment protocol that makes agent-to-agent commerce possible."
        )
    }},
    2,
    "Summarize text (Agent A hires Agent B)",
)

# Call 3: Sentiment analysis
paid_mcp_call(
    "tools/call",
    {"name": "sentiment", "arguments": {
        "text": "This agent-to-agent payment system is brilliant! Autonomous, reliable, and trustless."
    }},
    3,
    "Sentiment analysis (Agent A hires Agent B)",
)

print("=" * 55)
print("  ✓ Agent A completed all paid calls")
print("  Agent A hired Agent B autonomously via x402")
print("  Payments settled on Base Sepolia via KeeperHub ecosystem")
print("  All communication routed through AXL P2P mesh")
print("=" * 55)
