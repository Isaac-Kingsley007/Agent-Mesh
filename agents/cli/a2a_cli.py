#!/usr/bin/env python3
"""
A2A Client CLI — OpenAgents Gensyn Track
=========================================
An interactive command-line interface for communicating with A2A agents
on the AXL mesh network. Handles x402 payments autonomously.

Setup:
  export EVM_PRIVATE_KEY="0x..."     # Wallet with USDC on Base Sepolia
  export AXL_API="http://127.0.0.1:9002"  # Your AXL node (optional, default shown)

Run:
  python3 a2a_cli.py
"""

import os
import sys
import uuid
import json
from typing import Optional

# ── Optional: load .env if present ────────────────────────────────────────
try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    pass  # dotenv not required

import requests as std_requests
from eth_account import Account
from x402 import x402ClientSync
from x402.http import x402HTTPClientSync
from x402.mechanisms.evm import EthAccountSigner
from x402.mechanisms.evm.exact.register import register_exact_evm_client


# ── ANSI color helpers ─────────────────────────────────────────────────────

class C:
    RESET   = "\033[0m"
    BOLD    = "\033[1m"
    DIM     = "\033[2m"
    CYAN    = "\033[36m"
    GREEN   = "\033[32m"
    YELLOW  = "\033[33m"
    RED     = "\033[31m"
    MAGENTA = "\033[35m"
    BLUE    = "\033[34m"
    WHITE   = "\033[97m"

def c(color: str, text: str) -> str:
    return f"{color}{text}{C.RESET}"

def banner():
    print()
    print(c(C.CYAN + C.BOLD, "╔══════════════════════════════════════════════════════╗"))
    print(c(C.CYAN + C.BOLD, "║") + c(C.WHITE + C.BOLD, "       A2A Client CLI — OpenAgents / Gensyn          ") + c(C.CYAN + C.BOLD, "║"))
    print(c(C.CYAN + C.BOLD, "╚══════════════════════════════════════════════════════╝"))
    print()

def separator(title: str = ""):
    if title:
        padded = f"── {title} "
        print(c(C.DIM, padded + "─" * max(1, 54 - len(padded))))
    else:
        print(c(C.DIM, "─" * 56))


# ── Environment & x402 setup ──────────────────────────────────────────────

def setup_x402(private_key: str):
    account = Account.from_key(private_key)
    signer  = EthAccountSigner(account)
    client  = x402ClientSync()
    register_exact_evm_client(client, signer)
    http    = x402HTTPClientSync(client)
    return account, client, http


# ── Topology / peer discovery ─────────────────────────────────────────────

def fetch_topology(axl_api: str) -> dict:
    """
    GET /topology from the local AXL node.
    Returns the raw topology dict, or raises on error.
    """
    resp = std_requests.get(f"{axl_api}/topology", timeout=8)
    resp.raise_for_status()
    return resp.json()


def extract_peer_keys(topology: dict) -> list[str]:
    """Return all peer public keys that are not our own node."""
    our_key  = topology.get("our_public_key", "")
    peers    = topology.get("peers", {})
    keys: list[str] = []

    if isinstance(peers, dict):
        for k, v in peers.items():
            candidate = v.get("public_key", k) if isinstance(v, dict) else k
            if candidate and candidate != our_key:
                keys.append(candidate)
    elif isinstance(peers, list):
        for p in peers:
            candidate = p.get("public_key", "") if isinstance(p, dict) else str(p)
            if candidate and candidate != our_key:
                keys.append(candidate)

    return keys


def fetch_agent_card(axl_api: str, peer_key: str) -> Optional[dict]:
    """
    Try to fetch the agent card from a peer via the A2A URL.
    Returns the card dict or None if unavailable.
    """
    a2a_url = f"{axl_api}/a2a/{peer_key}"
    try:
        resp = std_requests.get(a2a_url, timeout=8)
        if resp.status_code == 200:
            return resp.json()
    except Exception:
        pass
    return None


# ── Core A2A message sender ────────────────────────────────────────────────

_call_counter = [0]

def send_a2a_message(
    axl_api: str,
    peer_key: str,
    message_text: str,
    x402_client_obj,
    x402_http_obj,
) -> Optional[str]:
    """
    Send a natural language message to an A2A agent.
    Handles x402 payment challenge autonomously.
    Returns the agent's reply text, or None on failure.
    """
    _call_counter[0] += 1
    call_id    = _call_counter[0]
    message_id = str(uuid.uuid4())[:8]
    a2a_url    = f"{axl_api}/a2a/{peer_key}"

    payload = {
        "jsonrpc": "2.0",
        "method":  "message/send",
        "id":       call_id,
        "params": {
            "message": {
                "role":      "user",
                "parts":     [{"kind": "text", "text": message_text}],
                "messageId": message_id,
            }
        },
    }

    # ── First attempt ──────────────────────────────────────────────────────
    try:
        resp = std_requests.post(
            a2a_url,
            json=payload,
            headers={"Content-Type": "application/json"},
            timeout=60,
        )
    except Exception as e:
        print(c(C.RED, f"  ✗ Request failed: {e}"))
        return None

    result = resp.json()

    # ── Handle x402 payment challenge ─────────────────────────────────────
    if isinstance(result, dict) and "_x402_challenge" in result:
        challenge        = result["_x402_challenge"]
        challenge_headers = challenge.get("headers", {})
        challenge_body    = challenge.get("body", {})

        print(c(C.YELLOW, "  ⚡ Payment required — paying automatically via x402..."))

        def get_header(name: str) -> Optional[str]:
            for k, v in challenge_headers.items():
                if k.lower() == name.lower():
                    return v
            return None

        try:
            payment_required = x402_http_obj.get_payment_required_response(
                get_header, challenge_body
            )
            payment_payload  = x402_client_obj.create_payment_payload(payment_required)
            payment_headers  = x402_http_obj.encode_payment_signature_header(payment_payload)

            retry_payload = {**payload, "_x402_payment": payment_headers}

            resp2  = std_requests.post(
                a2a_url,
                json=retry_payload,
                headers={"Content-Type": "application/json"},
                timeout=60,
            )
            result = resp2.json()

            # Strip receipt from display (but keep for logging)
            receipt = result.pop("_x402_receipt", None) if isinstance(result, dict) else None
            tx_info = ""
            if receipt:
                try:
                    r = json.loads(receipt) if isinstance(receipt, str) else receipt
                    tx = r.get("transaction") or r.get("txHash", "")
                    if tx:
                        tx_info = f" (tx: {str(tx)[:12]}...)"
                except Exception:
                    pass

            print(c(C.GREEN, f"  ✓ Payment settled — $0.001 USDC via x402{tx_info}"))

        except Exception as e:
            print(c(C.RED, f"  ✗ Payment failed: {e}"))
            return None

    # ── Extract reply text ─────────────────────────────────────────────────
    if isinstance(result, dict) and "result" in result:
        parts = result["result"].get("parts", [])
        texts = [p.get("text", "") for p in parts if p.get("kind") == "text"]
        return "\n".join(texts) if texts else "(empty reply)"

    elif isinstance(result, dict) and "error" in result:
        err = result["error"]
        print(c(C.RED, f"  ✗ Agent error: {err}"))
        return None

    else:
        print(c(C.RED, f"  ✗ Unexpected response: {str(result)[:200]}"))
        return None


# ── UI helpers ─────────────────────────────────────────────────────────────

def prompt(text: str) -> str:
    """Read user input, stripping whitespace. Raises EOFError/KeyboardInterrupt."""
    try:
        return input(text).strip()
    except (EOFError, KeyboardInterrupt):
        print()
        raise


def display_servers(servers: list[dict]):
    """Pretty-print the list of discovered A2A servers."""
    print()
    separator("Available A2A Servers")
    print()

    if not servers:
        print(c(C.YELLOW, "  No A2A servers found in topology."))
        print(c(C.DIM,    "  Make sure peers are connected to your AXL node."))
        print()
        return

    for idx, srv in enumerate(servers, 1):
        key   = srv["key"]
        card  = srv.get("card")
        name  = card.get("name", "Unknown Agent") if card else "Unknown Agent"
        desc  = card.get("description", "")[:80] if card else ""
        price = ""
        if card:
            caps  = card.get("capabilities", {})
            pay   = caps.get("payment", {})
            price = pay.get("price_incoming", "")

        print(f"  {c(C.CYAN + C.BOLD, str(idx) + '.')}  {c(C.WHITE + C.BOLD, name)}")
        print(f"      {c(C.DIM, 'Key:  ')} {c(C.DIM, key[:20] + '...')}")
        if desc:
            print(f"      {c(C.DIM, 'Info: ')} {desc}")
        if price:
            print(f"      {c(C.YELLOW, '💰 ')} {c(C.DIM, price)}")

        if card and "skills" in card:
            for skill in card["skills"]:
                print(f"      {c(C.GREEN, '✦ ')} {skill.get('name','')}: {skill.get('description','')[:70]}")
        print()

    print(c(C.DIM, f"  Found {len(servers)} server(s)"))
    print()


def display_reply(reply: str):
    """Format and display the agent's reply."""
    print()
    separator("Agent Reply")
    print()
    for line in reply.split("\n"):
        print(f"  {line}")
    print()
    separator()
    print()


# ── Main interaction flows ─────────────────────────────────────────────────

def discover_servers(axl_api: str) -> list[dict]:
    """Fetch topology, extract peers, and try to get each peer's agent card."""
    print(c(C.DIM, "  Querying AXL topology..."), end="", flush=True)

    try:
        topology = fetch_topology(axl_api)
    except Exception as e:
        print(c(C.RED, f"\n  ✗ Cannot reach AXL node at {axl_api}: {e}"))
        return []

    peer_keys = extract_peer_keys(topology)
    print(c(C.GREEN, f" found {len(peer_keys)} peer(s)"))

    if not peer_keys:
        return []

    servers = []
    for key in peer_keys:
        print(c(C.DIM, f"  Fetching card for {key[:16]}..."), end="", flush=True)
        card = fetch_agent_card(axl_api, key)
        if card:
            print(c(C.GREEN, " ✓"))
        else:
            print(c(C.DIM, " (no card — will still try A2A)"))
        servers.append({"key": key, "card": card})

    return servers


def chat_loop(
    axl_api: str,
    server: dict,
    x402_client_obj,
    x402_http_obj,
):
    """
    Interactive prompt loop for one selected A2A server.
    Type 'quit', 'exit', or 'back' to return to server selection.
    """
    key  = server["key"]
    card = server.get("card")
    name = card.get("name", "Agent") if card else "Agent"

    print()
    print(c(C.CYAN + C.BOLD, f"  Connected to: {name}"))
    print(c(C.DIM, f"  Key: {key[:32]}..."))
    print(c(C.DIM,  "  Type your message and press Enter. Each message costs $0.001 USDC."))
    print(c(C.DIM,  "  Type 'back' or 'quit' to return to server selection."))
    print()

    total_paid = 0
    messages_sent = 0

    while True:
        try:
            user_input = prompt(c(C.CYAN, "  You → "))
        except (EOFError, KeyboardInterrupt):
            print(c(C.YELLOW, "\n  Returning to server selection..."))
            break

        if not user_input:
            continue

        cmd = user_input.lower()
        if cmd in ("back", "quit", "exit", "q", "b"):
            print(c(C.YELLOW, "  Returning to server selection..."))
            break

        print()
        print(c(C.DIM, f"  Sending to {name[:40]}..."))

        reply = send_a2a_message(
            axl_api,
            key,
            user_input,
            x402_client_obj,
            x402_http_obj,
        )

        if reply is not None:
            messages_sent += 1
            total_paid    += 0.001
            display_reply(reply)
            print(c(C.DIM, f"  [Session: {messages_sent} message(s) sent, ${total_paid:.3f} USDC paid]"))
            print()
        else:
            print(c(C.RED, "  ✗ No reply received."))
            print()


def server_selection_loop(
    axl_api: str,
    x402_client_obj,
    x402_http_obj,
):
    """
    Home screen: discover servers, let user pick one, then enter chat loop.
    After quitting chat, returns here for a new selection.
    """
    while True:
        print()
        print(c(C.CYAN + C.BOLD, "  ── Home ──────────────────────────────────────────────"))
        print()
        print(c(C.DIM, "  [r] Refresh & re-discover servers"))
        print(c(C.DIM, "  [q] Quit"))
        print()

        servers = discover_servers(axl_api)
        display_servers(servers)

        if not servers:
            try:
                action = prompt(c(C.WHITE, "  > "))
            except (EOFError, KeyboardInterrupt):
                break
            if action.lower() in ("q", "quit", "exit"):
                break
            # 'r' or anything else → re-discover
            continue

        # Build choice prompt
        choices = [str(i) for i in range(1, len(servers) + 1)]
        prompt_str = (
            c(C.WHITE, "  Select a server ")
            + c(C.DIM, f"[1-{len(servers)}]")
            + c(C.DIM, ", [r] refresh, [q] quit")
            + c(C.WHITE, " → ")
        )

        try:
            action = prompt(prompt_str)
        except (EOFError, KeyboardInterrupt):
            break

        if action.lower() in ("q", "quit", "exit"):
            break

        if action.lower() in ("r", "refresh", ""):
            continue  # re-discover

        if action in choices:
            selected = servers[int(action) - 1]
            chat_loop(axl_api, selected, x402_client_obj, x402_http_obj)
        else:
            print(c(C.YELLOW, f"  Invalid choice '{action}'. Enter a number 1-{len(servers)}, r, or q."))


# ── Entry point ────────────────────────────────────────────────────────────

def main():
    banner()

    # ── Read config ────────────────────────────────────────────────────────
    private_key = os.environ.get("EVM_PRIVATE_KEY") or os.environ.get("AGENT_A_EVM_PRIVATE_KEY")
    axl_api     = os.environ.get("AXL_API", "http://127.0.0.1:9002")

    if not private_key:
        print(c(C.RED, "  ✗ EVM_PRIVATE_KEY is not set."))
        print(c(C.DIM, "    export EVM_PRIVATE_KEY='0x...'"))
        sys.exit(1)

    # ── Set up x402 ────────────────────────────────────────────────────────
    try:
        account, x402_client_obj, x402_http_obj = setup_x402(private_key)
    except Exception as e:
        print(c(C.RED, f"  ✗ Failed to initialise x402 payment client: {e}"))
        sys.exit(1)

    print(c(C.GREEN, f"  ✓ Wallet:   {account.address}"))
    print(c(C.GREEN, f"  ✓ AXL Node: {axl_api}"))
    print(c(C.DIM,    "  x402 payments will be handled automatically."))

    # ── Main loop ──────────────────────────────────────────────────────────
    try:
        server_selection_loop(axl_api, x402_client_obj, x402_http_obj)
    except KeyboardInterrupt:
        pass

    print()
    print(c(C.CYAN, "  Goodbye! Total messages sent: " + str(_call_counter[0])))
    print(c(C.DIM, f"  Total paid: ${_call_counter[0] * 0.001:.3f} USDC via x402"))
    print()


if __name__ == "__main__":
    main()