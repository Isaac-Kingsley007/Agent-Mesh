#!/usr/bin/env python3
"""
AgentMesh — A2A CLI
────────────────────────────────────────────────────────────
Interactive command-line client for the A2A protocol.

What it does:
  1. Discovers all running A2A agents on the mesh
  2. Shows you a menu to pick one
  3. Takes your message
  4. Pays autonomously via x402 and sends the message
  5. Prints the agent's reply

Setup (.env or export):
  AGENT_A_EVM_PRIVATE_KEY  — wallet that pays for requests

Run:
  python3 cli_a2a.py
  python3 cli_a2a.py --node http://127.0.0.1:9002   # use a specific AXL node
"""

import os
import sys
import json
import uuid
import argparse
from pathlib import Path
from dotenv import load_dotenv

load_dotenv(Path(__file__).resolve().parents[2] / ".env")

import requests
from eth_account import Account
from x402 import x402ClientSync
from x402.http import x402HTTPClientSync
from x402.mechanisms.evm import EthAccountSigner
from x402.mechanisms.evm.exact.register import register_exact_evm_client

# ── ANSI colours (degrade gracefully on Windows) ─────────────────────────────
try:
    import shutil
    _W = shutil.get_terminal_size().columns
except Exception:
    _W = 80

BOLD  = "\033[1m"
DIM   = "\033[2m"
CYAN  = "\033[96m"
GREEN = "\033[92m"
YELLOW= "\033[93m"
RED   = "\033[91m"
RESET = "\033[0m"
LINE  = "─" * _W

def hdr(text): print(f"\n{BOLD}{CYAN}{text}{RESET}")
def ok(text):  print(f"  {GREEN}✓{RESET} {text}")
def warn(text):print(f"  {YELLOW}⚠{RESET}  {text}")
def err(text): print(f"  {RED}✗{RESET} {text}")
def dim(text): print(f"  {DIM}{text}{RESET}")

# ── Known A2A endpoints derived from node configs ─────────────────────────────
# Each entry: (axl_api, a2a_port, label)
KNOWN_A2A_NODES = [
    ("http://127.0.0.1:9002",  9014, "Node A  (api:9002 → a2a:9014)"),
    ("http://127.0.0.1:9022",  9004, "Node C  (api:9022 → a2a:9004)"),
]

# ── Args ──────────────────────────────────────────────────────────────────────
parser = argparse.ArgumentParser(description="AgentMesh A2A CLI")
parser.add_argument("--node", default=None,
                    help="AXL node API URL to use (default: auto-discover from all nodes)")
parser.add_argument("--key", default=None,
                    help="EVM private key (overrides AGENT_A_EVM_PRIVATE_KEY env var)")
args = parser.parse_args()

# ── Wallet setup ──────────────────────────────────────────────────────────────
PRIVATE_KEY = args.key or os.environ.get("AGENT_A_EVM_PRIVATE_KEY")
if not PRIVATE_KEY:
    err("No private key found.")
    print("    Set AGENT_A_EVM_PRIVATE_KEY in .env or pass --key 0x...")
    sys.exit(1)

account = Account.from_key(PRIVATE_KEY)
signer  = EthAccountSigner(account)
x402c   = x402ClientSync()
register_exact_evm_client(x402c, signer)
x402h   = x402HTTPClientSync(x402c)

# ─────────────────────────────────────────────────────────────────────────────
# Step 1 — Discover available A2A agents
# ─────────────────────────────────────────────────────────────────────────────

def discover_a2a_agents(axl_api: str, a2a_port: int) -> list[dict]:
    """
    Given an AXL node, find all peer public keys and try fetching their
    A2A agent card.  Returns list of agent dicts.
    """
    agents = []

    # Get our own public key and all peer keys
    try:
        topology = requests.get(f"{axl_api}/topology", timeout=5).json()
    except Exception as e:
        warn(f"Cannot reach {axl_api}: {e}")
        return agents

    our_key = topology.get("our_public_key", "")
    peers   = topology.get("peers", {})

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

    # Also check ourselves — our own A2A server is reachable directly
    all_keys = [our_key] + peer_keys if our_key else peer_keys

    for peer_key in all_keys:
        # Try A2A via the AXL mesh routing
        mesh_a2a_url = f"{axl_api}/a2a/{peer_key}"
        # Also try the local port directly (for our own node)
        direct_url   = f"http://127.0.0.1:{a2a_port}"

        for base_url, source in [(mesh_a2a_url, "mesh"), (direct_url, "direct")]:
            try:
                resp = requests.get(base_url, timeout=4)
                if resp.status_code == 200:
                    card = resp.json()
                    if "name" in card or "skills" in card:
                        agents.append({
                            "peer_key":  peer_key,
                            "axl_api":   axl_api,
                            "base_url":  base_url,
                            "post_url":  base_url,
                            "card":      card,
                            "source":    source,
                        })
                        break   # found it, no need to try direct too
            except Exception:
                pass

    return agents


hdr("AgentMesh — A2A CLI")
print(f"  Wallet: {account.address}")
print(LINE)

hdr("Discovering A2A agents on the mesh...")

all_agents: list[dict] = []
seen_urls: set[str] = set()

nodes_to_scan = [(args.node, None, "custom")] if args.node else [
    (axl_api, a2a_port, label) for axl_api, a2a_port, label in KNOWN_A2A_NODES
]

for axl_api, a2a_port, node_label in nodes_to_scan:
    if a2a_port is None:
        # Try to infer from a running topology
        a2a_port = 9014  # fallback
    dim(f"Scanning {node_label or axl_api}...")
    found = discover_a2a_agents(axl_api, a2a_port)
    for agent in found:
        if agent["post_url"] not in seen_urls:
            seen_urls.add(agent["post_url"])
            all_agents.append(agent)

# Also probe local A2A ports directly (catches agents on this machine)
for axl_api, a2a_port, label in KNOWN_A2A_NODES:
    direct = f"http://127.0.0.1:{a2a_port}"
    if direct not in seen_urls:
        try:
            resp = requests.get(direct, timeout=3)
            if resp.status_code == 200:
                card = resp.json()
                if "name" in card or "skills" in card:
                    all_agents.append({
                        "peer_key": "local",
                        "axl_api":  axl_api,
                        "base_url": direct,
                        "post_url": direct,
                        "card":     card,
                        "source":   "direct",
                    })
                    seen_urls.add(direct)
        except Exception:
            pass

if not all_agents:
    err("No A2A agents found.")
    print("    Make sure at least one agent is running:")
    print("      bash scripts/start-all.sh")
    sys.exit(1)

ok(f"Found {len(all_agents)} A2A agent(s)")

# ─────────────────────────────────────────────────────────────────────────────
# Step 2 — Agent selection menu
# ─────────────────────────────────────────────────────────────────────────────

hdr("Available A2A Agents")
print()

for i, agent in enumerate(all_agents, 1):
    card  = agent["card"]
    name  = card.get("name", "Unknown Agent")
    desc  = card.get("description", "")[:72]
    price = card.get("capabilities", {}).get("payment", {}).get("price_incoming", "$0.001 USDC")
    skills= [s.get("name", "") for s in card.get("skills", [])]

    print(f"  {BOLD}[{i}]{RESET} {CYAN}{name}{RESET}")
    print(f"       {desc}")
    print(f"       Skills: {', '.join(skills) if skills else 'general'}")
    print(f"       Price:  {price}")
    print(f"       URL:    {agent['post_url']}")
    print()

while True:
    try:
        choice = input(f"{BOLD}Select agent [1-{len(all_agents)}]: {RESET}").strip()
        idx = int(choice) - 1
        if 0 <= idx < len(all_agents):
            selected = all_agents[idx]
            break
        print(f"    Enter a number between 1 and {len(all_agents)}")
    except (ValueError, KeyboardInterrupt):
        print("\nBye!")
        sys.exit(0)

card     = selected["card"]
post_url = selected["post_url"]
ok(f"Selected: {card.get('name', 'Agent')}")

# ─────────────────────────────────────────────────────────────────────────────
# Step 3 — Message input
# ─────────────────────────────────────────────────────────────────────────────

hdr("Compose your message")
print(f"  {DIM}Type your message below. Press Enter twice (blank line) to send.{RESET}")
print(f"  {DIM}Or just press Enter once for a single-line message.{RESET}\n")

lines = []
try:
    while True:
        line = input("  > ")
        if line == "" and lines:
            break
        lines.append(line)
except KeyboardInterrupt:
    print("\nBye!")
    sys.exit(0)

user_message = "\n".join(lines).strip()
if not user_message:
    err("Empty message — nothing to send.")
    sys.exit(1)

# ─────────────────────────────────────────────────────────────────────────────
# Step 4 — Send paid A2A message
# ─────────────────────────────────────────────────────────────────────────────

hdr(f"Sending message to {card.get('name', 'agent')}...")
print(f"  {DIM}Payment will be made automatically if the agent requires it.{RESET}\n")

message_id  = str(uuid.uuid4())[:8]
a2a_payload = {
    "jsonrpc": "2.0",
    "method":  "message/send",
    "id":      1,
    "params": {
        "message": {
            "role":      "user",
            "parts":     [{"kind": "text", "text": user_message}],
            "messageId": message_id,
        }
    },
}

def _get_challenge_header(challenge_headers: dict, name: str):
    for k, v in challenge_headers.items():
        if k.lower() == name.lower():
            return v
    return None

try:
    resp = requests.post(
        post_url, json=a2a_payload,
        headers={"Content-Type": "application/json"},
        timeout=30,
    )
except Exception as e:
    err(f"Request failed: {e}")
    sys.exit(1)

result = resp.json()

# ── Handle x402 payment challenge ────────────────────────────────────────────
if isinstance(result, dict) and "_x402_challenge" in result:
    challenge        = result["_x402_challenge"]
    challenge_headers= challenge.get("headers", {})
    challenge_body   = challenge.get("body", {})

    print(f"  {YELLOW}💰 Payment required — signing transaction...{RESET}")

    try:
        payment_required = x402h.get_payment_required_response(
            lambda name: _get_challenge_header(challenge_headers, name),
            challenge_body,
        )
        payment_payload = x402c.create_payment_payload(payment_required)
        payment_headers = x402h.encode_payment_signature_header(payment_payload)

        retry = {**a2a_payload, "_x402_payment": payment_headers}
        resp2 = requests.post(
            post_url, json=retry,
            headers={"Content-Type": "application/json"},
            timeout=30,
        )
        result = resp2.json()
        if isinstance(result, dict):
            result.pop("_x402_receipt", None)

        ok("Payment settled — $0.001 USDC sent via x402")

    except Exception as e:
        err(f"Payment failed: {e}")
        sys.exit(1)

# Still getting a 402 after paying
if isinstance(result, dict) and "_x402_challenge" in result:
    err("Payment was rejected by the agent.")
    sys.exit(1)

# ─────────────────────────────────────────────────────────────────────────────
# Step 5 — Display reply
# ─────────────────────────────────────────────────────────────────────────────

hdr("Agent Reply")
print(LINE)

if isinstance(result, dict) and "result" in result:
    reply_data  = result["result"]
    parts       = reply_data.get("parts", [])
    reply_texts = [p.get("text", "") for p in parts if p.get("kind") == "text"]
    reply       = "\n".join(reply_texts)

    if reply:
        for line in reply.split("\n"):
            print(f"  {line}")
    else:
        warn("Agent returned an empty reply.")
        dim(json.dumps(reply_data, indent=2))

elif isinstance(result, dict) and "error" in result:
    err(f"Agent returned an error: {result['error']}")
    sys.exit(1)
else:
    warn("Unexpected response format:")
    dim(json.dumps(result, indent=2)[:400])

print(LINE)
print()
ok("Done.")
print(f"  From wallet:  {account.address}")
print(f"  To agent:     {card.get('name', post_url)}")
print(f"  Message ID:   {message_id}")
print()