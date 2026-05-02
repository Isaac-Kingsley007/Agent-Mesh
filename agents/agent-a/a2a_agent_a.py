#!/usr/bin/env python3
"""
Agent A — A2A Client

This shows the "Hey help me with this" style of agent-to-agent communication.

Instead of:
  Agent A → tools/list → picks tool → tools/call → gets raw data

Now:
  Agent A → "Hey Agent B, help me analyze this text..."
  Agent B → "Sure! Here's my analysis: ..."

Agent A sends a natural language message to Agent B via the A2A protocol.
Agent B figures out what to do, runs its tools internally, and replies.
Payment flows via x402 on every exchange.

Setup:
  export AGENT_A_EVM_PRIVATE_KEY="0x..."
  export ANTHROPIC_API_KEY="sk-ant-..."   # Claude reasons about what to ask Agent B

Run:
  python3 agent_a_a2a.py
  python3 agent_a_a2a.py "Analyze my customer review and tell me if it's positive..."
"""

import os
import sys
import json
import uuid
from pathlib import Path
from dotenv import load_dotenv

load_dotenv(Path(__file__).resolve().parents[2] / ".env")

import requests as std_requests
import anthropic
from eth_account import Account
from x402 import x402ClientSync
from x402.http import x402HTTPClientSync
from x402.mechanisms.evm import EthAccountSigner
from x402.mechanisms.evm.exact.register import register_exact_evm_client

# ── Env vars ──────────────────────────────────────────────────────────────
PRIVATE_KEY = os.environ.get("AGENT_A_EVM_PRIVATE_KEY")
if not PRIVATE_KEY:
    print("ERROR: AGENT_A_EVM_PRIVATE_KEY is not set")
    sys.exit(1)

ANTHROPIC_API_KEY = os.environ.get("ANTHROPIC_API_KEY")
if not ANTHROPIC_API_KEY:
    print("ERROR: ANTHROPIC_API_KEY is not set")
    sys.exit(1)

# ── x402 payment setup ────────────────────────────────────────────────────
account = Account.from_key(PRIVATE_KEY)
signer = EthAccountSigner(account)
x402_client = x402ClientSync()
register_exact_evm_client(x402_client, signer)
x402_http = x402HTTPClientSync(x402_client)

# ── Claude (Agent A's reasoning engine) ──────────────────────────────────
claude = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY)

AXL_API = "http://127.0.0.1:9002"  # Agent A's AXL node

print("=" * 60)
print("  AgentMesh — Agent A (A2A Client Mode)")
print("=" * 60)
print(f"  Wallet:   {account.address}")
print(f"  Mode:     A2A — natural language agent-to-agent")
print()

# ── Discover Agent B ──────────────────────────────────────────────────────
print("Discovering Agent B via AXL topology...")
try:
    topology = std_requests.get(f"{AXL_API}/topology", timeout=5).json()
except Exception as e:
    print(f"ERROR: Cannot reach AXL Node A: {e}")
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
    print("ERROR: No peers found")
    sys.exit(1)

A2A_URL = f"{AXL_API}/a2a/{node_b_key}"
print(f"  Agent B: {node_b_key[:16]}...")
print(f"  A2A URL: {A2A_URL}")
print()


# ── Step 1: Discover what Agent B can do (agent card) ────────────────────
print("── Step 1: Reading Agent B's business card ─────────────────────────")

try:
    card_resp = std_requests.get(A2A_URL, timeout=10)
    if card_resp.status_code == 200:
        agent_card = card_resp.json()
        print(f"  Agent B says: {agent_card.get('description', '?')[:100]}")
        skills = agent_card.get("skills", [])
        for skill in skills:
            print(f"  Skill: {skill['name']} — {skill['description'][:80]}")
        print()
    else:
        print(f"  Could not fetch agent card (status {card_resp.status_code}) — continuing anyway")
        agent_card = {}
except Exception as e:
    print(f"  Agent card fetch failed: {e} — continuing anyway")
    agent_card = {}


# ── The x402 + A2A payment helper ─────────────────────────────────────────

_call_counter = [0]

def send_a2a_message(message_text: str, label: str = "") -> str | None:
    """
    Send a natural language message to Agent B via A2A.
    Pay via x402 if challenged.
    Returns Agent B's natural language reply.

    This is the core "hey help me with this" → "here's the answer" flow.
    """
    _call_counter[0] += 1
    call_id = _call_counter[0]
    message_id = str(uuid.uuid4())[:8]

    display_label = label or message_text[:40]
    print(f"── A2A Message {call_id}: {display_label} ──")
    print(f"  Agent A says: \"{message_text[:80]}{'...' if len(message_text) > 80 else ''}\"")

    # Build the A2A message/send payload
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

    # First attempt (may get 402 challenge back)
    try:
        resp = std_requests.post(
            A2A_URL,
            json=a2a_payload,
            headers={"Content-Type": "application/json"},
            timeout=30,
        )
    except Exception as e:
        print(f"  ERROR: {e}")
        return None

    print(f"  HTTP status: {resp.status_code}")
    result = resp.json()

    # ── Handle x402 payment challenge ────────────────────────────────────
    if isinstance(result, dict) and "_x402_challenge" in result:
        challenge = result["_x402_challenge"]
        print(f"  Agent B says: 💰 Payment required ($0.001 USDC)")

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

            # Embed payment in the A2A payload (same tunneling as MCP)
            retry_payload = {**a2a_payload, "_x402_payment": payment_headers}

            resp2 = std_requests.post(
                A2A_URL,
                json=retry_payload,
                headers={"Content-Type": "application/json"},
                timeout=30,
            )

            result = resp2.json()
            if isinstance(result, dict):
                result.pop("_x402_receipt", None)

            print(f"  Payment:     ✓ $0.001 USDC settled via x402")

        except Exception as e:
            print(f"  ERROR: Payment failed: {e}")
            return None

    # ── Extract Agent B's reply ───────────────────────────────────────────
    if isinstance(result, dict) and "result" in result:
        reply_parts = result["result"].get("parts", [])
        reply_texts = [p.get("text", "") for p in reply_parts if p.get("kind") == "text"]
        reply = "\n".join(reply_texts)

        print()
        print(f"  Agent B replies:")
        print(f"  {'─' * 50}")
        for line in reply.split("\n"):
            print(f"  {line}")
        print(f"  {'─' * 50}")
        print()
        return reply

    elif isinstance(result, dict) and "error" in result:
        print(f"  ERROR from Agent B: {result['error']}")
        return None
    else:
        print(f"  Unexpected response: {str(result)[:200]}")
        return None


# ── Step 2: Agent A uses Claude to decide what to ask Agent B ─────────────

USER_TASK = " ".join(sys.argv[1:]) if len(sys.argv) > 1 else (
    "I have a customer review that I need Agent B to help me with: "
    "'The product arrived quickly and the packaging was perfect! "
    "However, the battery life is disappointing — it barely lasts 4 hours. "
    "Customer support was helpful when I reached out. Overall a mixed experience.'"
)

print("── Step 2: Claude (Agent A's brain) composing message for Agent B ───")
print(f"  Task: {USER_TASK[:80]}...")
print()

# Claude reads the task and composes what Agent A should *say* to Agent B
compose_response = claude.messages.create(
    model="claude-opus-4-5",
    max_tokens=256,
    system=(
        "You are Agent A's messaging composer. "
        "Your job is to write a clear, natural language request to send to Agent B, "
        "a text intelligence agent. "
        "Be conversational but specific. Include the actual text that needs processing. "
        "Write just the message — no preamble, no quotes around it."
    ),
    messages=[{
        "role": "user",
        "content": (
            f"I need to send Agent B a message asking for help with this task:\n{USER_TASK}\n\n"
            f"Agent B can: summarize text, analyze sentiment, extract keywords, translate to Spanish.\n"
            f"Write the message Agent A should send to Agent B."
        ),
    }],
)

composed_message = compose_response.content[0].text.strip()
print(f"  Claude composed: \"{composed_message[:100]}...\"")
print()


# ── Step 3: Send the A2A message — "Hey help me with this" ───────────────

print("── Step 3: Agent A → Agent B (A2A) ─────────────────────────────────")
reply = send_a2a_message(composed_message, "Help request to Agent B")


# ── Step 4: Claude synthesizes Agent B's reply into a final report ────────

if reply:
    print("── Step 4: Claude synthesizing Agent B's response ───────────────────")

    synthesis = claude.messages.create(
        model="claude-opus-4-5",
        max_tokens=400,
        messages=[{
            "role": "user",
            "content": (
                f"I (Agent A) asked Agent B to help with this task:\n{USER_TASK}\n\n"
                f"Agent B replied:\n{reply}\n\n"
                f"Write a brief final summary of what we learned. "
                f"Keep it actionable and under 150 words."
            ),
        }],
    )

    final = synthesis.content[0].text.strip()
    print()
    print("  ╔══ AGENT A FINAL SUMMARY ═══════════════════════════════╗")
    for line in final.split("\n"):
        print(f"  ║  {line}")
    print("  ╚════════════════════════════════════════════════════════╝")
    print()


print("=" * 60)
print(f"  ✓ A2A exchange complete")
print(f"  Messages sent: {_call_counter[0]}")
print(f"  Total paid: ${_call_counter[0] * 0.001:.3f} USDC via x402")
print(f"  Protocol: A2A over AXL mesh")
print("=" * 60)