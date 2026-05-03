#!/usr/bin/env python3
"""
A2A Client CLI — OpenAgents Gensyn Track
=========================================
Interactive CLI for communicating with A2A agents on the AXL mesh.
Handles x402 payments autonomously.

Setup:
  export EVM_PRIVATE_KEY="0x..."          # Wallet with USDC on Base Sepolia
  export AXL_API="http://127.0.0.1:9002"  # Your AXL node (optional, this is the default)

Run:
  python3 a2a_cli.py
"""

import os
import sys
import uuid
import json
from typing import Optional

try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    pass

import requests as std_requests
from eth_account import Account
from x402 import x402ClientSync
from x402.http import x402HTTPClientSync
from x402.mechanisms.evm import EthAccountSigner
from x402.mechanisms.evm.exact.register import register_exact_evm_client


# ── ANSI colors ────────────────────────────────────────────────────────────

class C:
    RESET   = "\033[0m"
    BOLD    = "\033[1m"
    DIM     = "\033[2m"
    CYAN    = "\033[36m"
    GREEN   = "\033[32m"
    YELLOW  = "\033[33m"
    RED     = "\033[31m"
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


# ── x402 setup ────────────────────────────────────────────────────────────

def setup_x402(private_key: str):
    account = Account.from_key(private_key)
    signer  = EthAccountSigner(account)
    client  = x402ClientSync()
    register_exact_evm_client(client, signer)
    http    = x402HTTPClientSync(client)
    return account, client, http


# ── AXL / topology helpers ────────────────────────────────────────────────

def fetch_topology(axl_api: str) -> dict:
    resp = std_requests.get(f"{axl_api}/topology", timeout=8)
    resp.raise_for_status()
    return resp.json()


def extract_peer_keys(topology: dict) -> list[str]:
    our_key = topology.get("our_public_key", "")
    peers   = topology.get("peers", {})
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
    Try to fetch the agent card via the AXL A2A bridge.
    Tries /.well-known/agent.json first, then the root GET.
    """
    base_url   = f"{axl_api}/a2a/{peer_key}"
    candidates = [
        f"{base_url}/.well-known/agent.json",
        base_url,
    ]
    for url in candidates:
        try:
            resp = std_requests.get(url, timeout=8)
            if resp.status_code == 200 and resp.text.strip():
                try:
                    data = resp.json()
                    if isinstance(data, dict) and "error" not in data:
                        return data
                except Exception:
                    pass
        except Exception:
            pass
    return None


def probe_a2a(axl_api: str, peer_key: str) -> tuple[bool, str]:
    """
    Send a minimal JSON-RPC ping to check if this peer has a reachable A2A server.
    Returns (reachable: bool, reason: str).

    Key insight: a 502 from the AXL bridge means it couldn't reach the A2A
    backend (e.g. Agent B which is MCP-only, or a misconfigured port).
    A 402 or any JSON response means the A2A server is alive.
    """
    url = f"{axl_api}/a2a/{peer_key}"
    try:
        resp = std_requests.post(
            url,
            json={"jsonrpc": "2.0", "method": "agent/info", "id": 0, "params": {}},
            headers={"Content-Type": "application/json"},
            timeout=8,
        )
    except Exception as e:
        return False, str(e)

    raw = resp.text.strip() if resp.text else ""

    # 502 = AXL forwarded to the wrong/missing port ("connection refused")
    if resp.status_code == 502:
        reason = raw[:120] if raw else "502 — AXL could not reach A2A backend"
        return False, reason

    # 402 = server alive, wants payment — good
    if resp.status_code == 402:
        return True, "alive (payment required)"

    # No body on 200 — treat as alive
    if not raw:
        return True, "alive"

    # Try JSON
    try:
        data = resp.json()
        if isinstance(data, dict):
            if "_x402_challenge" in data:
                return True, "alive (payment required)"
            if "result" in data or "error" in data:
                return True, "alive"
        return True, "alive"
    except Exception:
        return True, "alive"


# ── Safe JSON parse ────────────────────────────────────────────────────────

def safe_json(resp) -> Optional[dict]:
    """Parse JSON safely; print a clear message and return None on failure."""
    raw = resp.text.strip() if resp.text else ""
    if not raw:
        print(c(C.RED,  f"  ✗ Empty response body (HTTP {resp.status_code})"))
        print(c(C.DIM,   "    The AXL node could not reach the A2A backend."))
        return None
    try:
        return resp.json()
    except Exception:
        print(c(C.RED,  f"  ✗ Non-JSON response (HTTP {resp.status_code}):"))
        print(c(C.DIM,  f"    {raw[:200]}"))
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
    Send a natural language message to an A2A agent via AXL.
    Handles x402 payment challenges (HTTP-header and JSON-tunneled) autonomously.
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

    # ── 502 = AXL couldn't reach A2A backend ──────────────────────────────
    if resp.status_code == 502:
        raw = resp.text.strip() if resp.text else "no body"
        print(c(C.RED,    "  ✗ AXL 502 — cannot reach the A2A backend for this peer."))
        print(c(C.DIM,   f"    {raw[:200]}"))
        print(c(C.YELLOW, "  ℹ  This peer may be MCP-only, or its A2A server is misconfigured."))
        return None

    # ── Real HTTP 402 (payment info in response headers) ──────────────────
    if resp.status_code == 402:
        print(c(C.YELLOW, "  ⚡ HTTP 402 — paying automatically via x402..."))
        raw_body = safe_json(resp) or {}

        def get_hdr(name: str) -> Optional[str]:
            return resp.headers.get(name)

        try:
            payment_required = x402_http_obj.get_payment_required_response(get_hdr, raw_body)
            payment_payload  = x402_client_obj.create_payment_payload(payment_required)
            payment_sig      = x402_http_obj.encode_payment_signature_header(payment_payload)

            resp = std_requests.post(
                a2a_url,
                json={**payload, "_x402_payment": payment_sig},
                headers={"Content-Type": "application/json"},
                timeout=60,
            )
            print(c(C.GREEN, "  ✓ Payment settled — $0.001 USDC via x402"))
        except Exception as e:
            print(c(C.RED, f"  ✗ Payment failed (HTTP 402 path): {e}"))
            return None

    # ── Parse JSON body ───────────────────────────────────────────────────
    result = safe_json(resp)
    if result is None:
        return None

    # ── JSON-tunneled x402 challenge ──────────────────────────────────────
    if isinstance(result, dict) and "_x402_challenge" in result:
        challenge         = result["_x402_challenge"]
        challenge_headers = challenge.get("headers", {})
        challenge_body    = challenge.get("body", {})

        print(c(C.YELLOW, "  ⚡ x402 challenge — paying automatically..."))

        def get_challenge_header(name: str) -> Optional[str]:
            for k, v in challenge_headers.items():
                if k.lower() == name.lower():
                    return v
            return None

        try:
            payment_required = x402_http_obj.get_payment_required_response(
                get_challenge_header, challenge_body
            )
            payment_payload = x402_client_obj.create_payment_payload(payment_required)
            payment_sig     = x402_http_obj.encode_payment_signature_header(payment_payload)

            resp2  = std_requests.post(
                a2a_url,
                json={**payload, "_x402_payment": payment_sig},
                headers={"Content-Type": "application/json"},
                timeout=60,
            )
            result = safe_json(resp2)
            if result is None:
                return None

            receipt = result.pop("_x402_receipt", None) if isinstance(result, dict) else None
            tx_info = ""
            if receipt:
                try:
                    r  = json.loads(receipt) if isinstance(receipt, str) else receipt
                    tx = r.get("transaction") or r.get("txHash", "")
                    if tx:
                        tx_info = f" (tx: {str(tx)[:12]}...)"
                except Exception:
                    pass

            print(c(C.GREEN, f"  ✓ Payment settled — $0.001 USDC via x402{tx_info}"))

        except Exception as e:
            print(c(C.RED, f"  ✗ Payment failed: {e}"))
            return None

    # ── Extract reply ─────────────────────────────────────────────────────
    if isinstance(result, dict) and "result" in result:
        parts = result["result"].get("parts", [])
        texts = [p.get("text", "") for p in parts if p.get("kind") == "text"]
        return "\n".join(texts) if texts else "(empty reply)"

    elif isinstance(result, dict) and "error" in result:
        print(c(C.RED, f"  ✗ Agent error: {result['error']}"))
        return None

    else:
        print(c(C.RED, f"  ✗ Unexpected response: {str(result)[:200]}"))
        return None


# ── UI helpers ─────────────────────────────────────────────────────────────

def prompt(text: str) -> str:
    try:
        return input(text).strip()
    except (EOFError, KeyboardInterrupt):
        print()
        raise


def display_servers(servers: list[dict]):
    """Pretty-print discovered A2A servers with reachability status."""
    print()
    separator("Available A2A Servers")
    print()

    reachable   = [s for s in servers if s.get("reachable")]
    unreachable = [s for s in servers if not s.get("reachable")]

    if not reachable:
        print(c(C.YELLOW, "  No reachable A2A servers found."))
        if unreachable:
            print()
            print(c(C.DIM, "  Peers found but unreachable:"))
            for s in unreachable:
                print(c(C.DIM, f"    ✗ {s['key'][:24]}... — {s.get('probe_reason','?')[:80]}"))
        print()
        print(c(C.DIM, "  Tip: make sure peer A2A servers are running and registered with AXL."))
        print()
        return

    for idx, srv in enumerate(reachable, 1):
        key   = srv["key"]
        card  = srv.get("card")
        name  = card.get("name", "Unknown Agent") if card else "Unknown Agent"
        desc  = card.get("description", "")[:80] if card else ""
        price = ""
        if card:
            pay   = card.get("capabilities", {}).get("payment", {})
            price = pay.get("price_incoming", "")

        print(f"  {c(C.CYAN + C.BOLD, str(idx) + '.')}  {c(C.WHITE + C.BOLD, name)}")
        print(f"      {c(C.DIM, 'Key:  ')}{c(C.DIM, key[:24] + '...')}")
        if desc:
            print(f"      {c(C.DIM, 'Info: ')}{desc}")
        if price:
            print(f"      {c(C.YELLOW, '💰 ')}{c(C.DIM, price)}")
        if card and "skills" in card:
            for skill in card["skills"]:
                print(f"      {c(C.GREEN, '✦ ')}{skill.get('name','')}: {skill.get('description','')[:70]}")
        print(f"      {c(C.GREEN, '● A2A reachable')}")
        print()

    if unreachable:
        print(c(C.DIM, "  Not selectable (no A2A backend):"))
        for s in unreachable:
            print(c(C.DIM, f"    ✗ {s['key'][:24]}... — {s.get('probe_reason','unreachable')[:80]}"))
        print()

    print(c(C.DIM, f"  {len(reachable)} reachable · {len(unreachable)} unavailable"))
    print()


def display_reply(reply: str):
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
    """
    1. Fetch topology to get all peer public keys.
    2. Probe each peer with a lightweight A2A ping to check reachability.
    3. Fetch agent cards only for reachable peers.
    """
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
        short = key[:16]

        print(c(C.DIM, f"  Probing {short}..."), end="", flush=True)
        reachable, reason = probe_a2a(axl_api, key)

        if reachable:
            print(c(C.GREEN, " ✓ A2A alive"), end="")
        else:
            print(c(C.RED, f" ✗ {reason[:60]}"), end="")
        print()

        card = None
        if reachable:
            print(c(C.DIM, f"  Fetching card for {short}..."), end="", flush=True)
            card = fetch_agent_card(axl_api, key)
            if card:
                print(c(C.GREEN, f" ✓ ({card.get('name', '?')})"))
            else:
                print(c(C.DIM, " (no card)"))

        servers.append({
            "key":          key,
            "card":         card,
            "reachable":    reachable,
            "probe_reason": reason,
        })

    return servers


def chat_loop(axl_api: str, server: dict, x402_client_obj, x402_http_obj):
    """
    Interactive prompt loop for a selected A2A server.
    Type 'back', 'quit', or 'exit' to return to server selection.
    """
    key  = server["key"]
    card = server.get("card")
    name = card.get("name", "Agent") if card else "Agent"

    print()
    print(c(C.CYAN + C.BOLD, f"  Connected to: {name}"))
    print(c(C.DIM, f"  Key: {key}"))
    print(c(C.DIM,  "  Each message costs $0.001 USDC — paid automatically."))
    print(c(C.DIM,  "  Type 'back' to return to server selection."))
    print()

    total_paid    = 0.0
    messages_sent = 0

    while True:
        try:
            user_input = prompt(c(C.CYAN, "  You → "))
        except (EOFError, KeyboardInterrupt):
            print(c(C.YELLOW, "\n  Returning to server selection..."))
            break

        if not user_input:
            continue

        if user_input.lower() in ("back", "quit", "exit", "q", "b"):
            print(c(C.YELLOW, "  Returning to server selection..."))
            break

        print()
        print(c(C.DIM, f"  Sending to {name}..."))

        reply = send_a2a_message(
            axl_api, key, user_input, x402_client_obj, x402_http_obj
        )

        if reply is not None:
            messages_sent += 1
            total_paid    += 0.001
            display_reply(reply)
            print(c(C.DIM, f"  [Session: {messages_sent} msg(s) · ${total_paid:.3f} USDC paid]"))
            print()
        else:
            print(c(C.RED, "  ✗ No reply received."))
            print()


def server_selection_loop(axl_api: str, x402_client_obj, x402_http_obj):
    """
    Home screen: discover → pick server → chat → back to home.
    """
    while True:
        print()
        print(c(C.CYAN + C.BOLD, "  ── Home ──────────────────────────────────────────────"))
        print()
        print(c(C.DIM, "  [r] Refresh & re-discover   [q] Quit"))
        print()

        servers   = discover_servers(axl_api)
        reachable = [s for s in servers if s.get("reachable")]

        display_servers(servers)

        if not reachable:
            try:
                action = prompt(c(C.WHITE, "  > "))
            except (EOFError, KeyboardInterrupt):
                break
            if action.lower() in ("q", "quit", "exit"):
                break
            continue

        prompt_str = (
            c(C.WHITE, "  Select a server ")
            + c(C.DIM, f"[1-{len(reachable)}]")
            + c(C.DIM, " · [r] refresh · [q] quit")
            + c(C.WHITE, " → ")
        )

        try:
            action = prompt(prompt_str)
        except (EOFError, KeyboardInterrupt):
            break

        if action.lower() in ("q", "quit", "exit"):
            break

        if action.lower() in ("r", "refresh", ""):
            continue

        choices = [str(i) for i in range(1, len(reachable) + 1)]
        if action in choices:
            chat_loop(axl_api, reachable[int(action) - 1], x402_client_obj, x402_http_obj)
        else:
            print(c(C.YELLOW, f"  Invalid choice. Enter 1-{len(reachable)}, r, or q."))


# ── Entry point ────────────────────────────────────────────────────────────

def main():
    banner()

    private_key = (
        os.environ.get("EVM_PRIVATE_KEY")
        or os.environ.get("AGENT_A_EVM_PRIVATE_KEY")
    )
    axl_api = os.environ.get("AXL_API", "http://127.0.0.1:9002")

    if not private_key:
        print(c(C.RED, "  ✗ EVM_PRIVATE_KEY is not set."))
        print(c(C.DIM, "    export EVM_PRIVATE_KEY='0x...'"))
        sys.exit(1)

    try:
        account, x402_client_obj, x402_http_obj = setup_x402(private_key)
    except Exception as e:
        print(c(C.RED, f"  ✗ Failed to initialise x402 client: {e}"))
        sys.exit(1)

    print(c(C.GREEN, f"  ✓ Wallet:   {account.address}"))
    print(c(C.GREEN, f"  ✓ AXL Node: {axl_api}"))
    print(c(C.DIM,    "  x402 payments will be handled automatically."))

    try:
        server_selection_loop(axl_api, x402_client_obj, x402_http_obj)
    except KeyboardInterrupt:
        pass

    print()
    print(c(C.CYAN, f"  Goodbye! Messages sent: {_call_counter[0]}"))
    print(c(C.DIM,  f"  Total paid: ${_call_counter[0] * 0.001:.3f} USDC"))
    print()


if __name__ == "__main__":
    main()