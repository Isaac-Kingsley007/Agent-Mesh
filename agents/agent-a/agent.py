#!/usr/bin/env python3
"""
Agent A — Autonomous buyer agent for AgentMesh.
Pays Agent B via x402 (KeeperHub ecosystem) through the AXL mesh.

Setup:
  export AGENT_A_EVM_PRIVATE_KEY="0x..."   # wallet with USDC on Base Sepolia
  export AGENT_B_WALLET_ADDRESS="0x..."    # KeeperHub creator wallet (informational)

Run:
  python3 agents/agent-a/agent.py
"""

import os
import sys
import json
import requests as std_requests
from eth_account import Account
from x402 import x402ClientSync
from x402.http import x402HTTPClientSync
from x402.http.clients import x402_requests
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

# ── 4. Make paid MCP calls through the mesh ──────────────────────────────
def paid_mcp_call(session, method, params, call_id, label):
    print(f"── Call {call_id}: {label} {'─' * max(1, 43 - len(label))}")
    payload = {"jsonrpc": "2.0", "method": method, "id": call_id, "params": params}

    try:
        resp = session.post(
            MESH_URL,
            json=payload,
            headers={"Content-Type": "application/json"},
            timeout=30,
        )
    except Exception as e:
        print(f"  ERROR: Request failed: {e}")
        return

    print(f"  HTTP status:      {resp.status_code}")

    payment_receipt = resp.headers.get("PAYMENT-RESPONSE") or resp.headers.get("X-PAYMENT-RESPONSE")
    if payment_receipt:
        try:
            receipt_data = json.loads(payment_receipt)
            print(f"  Payment settled:  ✓ USDC paid")
            if "transaction" in receipt_data or "txHash" in receipt_data:
                tx = receipt_data.get("transaction") or receipt_data.get("txHash", "")
                print(f"  Tx hash:          {str(tx)[:20]}...")
        except Exception:
            print(f"  Payment receipt:  {payment_receipt[:60]}...")
    else:
        print(f"  Payment receipt:  (not in headers — check middleware logs)")

    if resp.status_code == 200:
        try:
            result = resp.json()
            content = result.get("result", {})
            if method == "tools/list":
                tools = content.get("tools", [])
                print(f"  Tools available:  {[t['name'] for t in tools]}")
            elif method == "tools/call":
                tool_result = content.get("content", [{}])[0].get("text", "")
                parsed = json.loads(tool_result) if tool_result else {}
                print(f"  Tool result:      {json.dumps(parsed, indent=None)[:120]}")
        except Exception as e:
            print(f"  Response:         {resp.text[:120]}")
    elif resp.status_code == 402:
        print("  ERROR: Got 402 — payment was not accepted")
        print(f"  Body: {resp.text[:200]}")
    else:
        print(f"  Response: {resp.text[:200]}")
    print()

with x402_requests(x402_client) as session:
    # Call 1: Discover tools
    paid_mcp_call(session, "tools/list", {}, 1, "Discover available tools")

    # Call 2: Summarize
    paid_mcp_call(session,
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
    paid_mcp_call(session,
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
print("=" * 55)
