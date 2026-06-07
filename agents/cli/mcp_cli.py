#!/usr/bin/env python3
"""
AgentMesh — MCP CLI
────────────────────────────────────────────────────────────
Interactive command-line client for MCP tool calls.

What it does:
  1. Discovers all running MCP servers on the mesh
  2. Lists all available tools across all servers
  3. Takes your prompt
  4. Lets you pick a tool (or auto-selects if obvious)
  5. Asks for any required tool arguments
  6. Pays autonomously via x402 and runs the tool
  7. Prints the result

Setup (.env or export):
  AGENT_A_EVM_PRIVATE_KEY  — wallet that pays for requests
  GEMINI_API_KEY           — used to auto-map prompt → tool arguments

Run:
  python3 cli_mcp.py
  python3 cli_mcp.py --no-ai    # skip Gemini argument mapping, enter args manually
"""

import os
import sys
import json
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

# ── ANSI colours ──────────────────────────────────────────────────────────────
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
MAGENTA="\033[95m"
RESET = "\033[0m"
LINE  = "─" * _W

def hdr(text):  print(f"\n{BOLD}{CYAN}{text}{RESET}")
def ok(text):   print(f"  {GREEN}✓{RESET} {text}")
def warn(text): print(f"  {YELLOW}⚠{RESET}  {text}")
def err(text):  print(f"  {RED}✗{RESET} {text}")
def dim(text):  print(f"  {DIM}{text}{RESET}")

# ── Known MCP nodes from your node configs ────────────────────────────────────
# (axl_api, router_port, service_name, label)
KNOWN_MCP_NODES = [
    ("http://127.0.0.1:9002",  9013, "agent-a",   "Agent A  (analyze, rewrite)"),
    ("http://127.0.0.1:9012",  9003, "agentmesh", "Agent B  (summarize, sentiment, keywords, translate)"),
    ("http://127.0.0.1:9022",  9023, "agent-c",   "Agent C  (qa, code_review)"),
]

# ── Args ──────────────────────────────────────────────────────────────────────
parser = argparse.ArgumentParser(description="AgentMesh MCP CLI")
parser.add_argument("--no-ai", action="store_true",
                    help="Disable Gemini argument mapping — enter tool args manually")
parser.add_argument("--key", default=None,
                    help="EVM private key (overrides AGENT_A_EVM_PRIVATE_KEY)")
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

# ── Optional Gemini for argument mapping ─────────────────────────────────────
USE_AI = not args.no_ai
gemini_client = None
if USE_AI:
    GEMINI_API_KEY = os.environ.get("GEMINI_API_KEY")
    if GEMINI_API_KEY:
        try:
            from google import genai
            gemini_client = genai.Client(api_key=GEMINI_API_KEY)
        except ImportError:
            warn("google-genai not installed — falling back to manual argument entry.")
            USE_AI = False
    else:
        warn("GEMINI_API_KEY not set — falling back to manual argument entry.")
        USE_AI = False

# ─────────────────────────────────────────────────────────────────────────────
# Paid MCP call helper
# ─────────────────────────────────────────────────────────────────────────────

_call_counter = [0]

def _get_challenge_header(headers: dict, name: str):
    for k, v in headers.items():
        if k.lower() == name.lower():
            return v
    return None

def paid_mcp_call(url: str, method: str, params: dict) -> dict | None:
    """Make a paid MCP JSON-RPC call through the AXL mesh, handling x402."""
    _call_counter[0] += 1
    payload = {
        "jsonrpc": "2.0",
        "method":  method,
        "id":      _call_counter[0],
        "params":  params,
    }

    try:
        resp = requests.post(
            url, json=payload,
            headers={"Content-Type": "application/json"},
            timeout=30,
        )
    except Exception as e:
        err(f"Request failed: {e}")
        return None

    if resp.status_code != 200:
        err(f"HTTP {resp.status_code}: {resp.text[:120]}")
        return None

    result = resp.json()

    # ── x402 payment challenge ────────────────────────────────────────────────
    if isinstance(result, dict) and "_x402_challenge" in result:
        challenge = result["_x402_challenge"]
        ch_headers= challenge.get("headers", {})
        ch_body   = challenge.get("body", {})

        print(f"  {YELLOW}💰 Payment required — signing...{RESET}")

        try:
            payment_required = x402h.get_payment_required_response(
                lambda name: _get_challenge_header(ch_headers, name),
                ch_body,
            )
            payment_payload = x402c.create_payment_payload(payment_required)
            payment_headers = x402h.encode_payment_signature_header(payment_payload)

            retry = {**payload, "_x402_payment": payment_headers}
            resp2 = requests.post(
                url, json=retry,
                headers={"Content-Type": "application/json"},
                timeout=30,
            )
            result = resp2.json()
            if isinstance(result, dict):
                result.pop("_x402_receipt", None)

            ok("Payment settled — $0.001 USDC sent via x402")

        except Exception as e:
            err(f"Payment failed: {e}")
            return None

    if isinstance(result, dict) and "_x402_challenge" in result:
        err("Payment rejected by server.")
        return None

    return result.get("result") if isinstance(result, dict) else None

# ─────────────────────────────────────────────────────────────────────────────
# Step 1 — Discover MCP servers and their tools
# ─────────────────────────────────────────────────────────────────────────────

hdr("AgentMesh — MCP CLI")
print(f"  Wallet: {account.address}")
print(LINE)

hdr("Discovering MCP servers...")

# Each entry: {label, service, axl_api, mesh_url, tools: [...]}
all_servers: list[dict] = []

for axl_api, router_port, service_name, label in KNOWN_MCP_NODES:
    dim(f"Probing {label}...")

    # Build the mesh URL: requests go through the AXL node
    # First get the peer key for this node from topology
    try:
        topo = requests.get(f"{axl_api}/topology", timeout=4).json()
        our_key = topo.get("our_public_key", "")
    except Exception:
        our_key = ""

    # The MCP URL through the AXL mesh uses the node's own key
    if our_key:
        mesh_url = f"{axl_api}/mcp/{our_key}/{service_name}"
    else:
        # Fallback: try the router directly
        mesh_url = f"http://127.0.0.1:{router_port}/mcp"

    # Also try via peers — ask from another node's perspective
    # For the demo, also probe with a direct router URL as fallback
    direct_router_url = f"http://127.0.0.1:{router_port}/mcp"

    tools_found = []
    working_url = None

    for probe_url in [mesh_url, direct_router_url]:
        result = paid_mcp_call(probe_url, "tools/list", {})
        if result and "tools" in result:
            tools_found = result["tools"]
            working_url = probe_url
            break

    if tools_found and working_url:
        all_servers.append({
            "label":    label,
            "service":  service_name,
            "axl_api":  axl_api,
            "url":      working_url,
            "tools":    tools_found,
        })
        ok(f"{label}: {len(tools_found)} tool(s) — {[t['name'] for t in tools_found]}")
    else:
        warn(f"{label}: not reachable (is it running?)")

if not all_servers:
    err("No MCP servers found. Make sure agents are running:")
    print("      bash scripts/start-all.sh")
    sys.exit(1)

# ─────────────────────────────────────────────────────────────────────────────
# Step 2 — Get user prompt
# ─────────────────────────────────────────────────────────────────────────────

hdr("What would you like to do?")
print(f"  {DIM}Enter your request in plain English. Examples:{RESET}")
print(f"  {DIM}  • Summarize this article: <text>{RESET}")
print(f"  {DIM}  • Analyze the sentiment of: <text>{RESET}")
print(f"  {DIM}  • Review this Python code: <code>{RESET}")
print(f"  {DIM}  • Rewrite this in a formal style: <text>{RESET}")
print(f"  {DIM}Press Enter twice to submit.{RESET}\n")

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

user_prompt = "\n".join(lines).strip()
if not user_prompt:
    err("Empty prompt.")
    sys.exit(1)

# ─────────────────────────────────────────────────────────────────────────────
# Step 3 — Show all tools, let user pick
# ─────────────────────────────────────────────────────────────────────────────

hdr("Available Tools")
print()

all_tools: list[dict] = []  # flat list: {server, tool}
for server in all_servers:
    print(f"  {BOLD}{MAGENTA}{server['label']}{RESET}")
    for tool in server["tools"]:
        all_tools.append({"server": server, "tool": tool})
        idx = len(all_tools)
        print(f"    {BOLD}[{idx}]{RESET} {CYAN}{tool['name']}{RESET}")
        print(f"         {tool.get('description', '')[:80]}")
    print()

# Auto-suggest using Gemini if available
suggested_idx = None
if USE_AI and gemini_client:
    try:
        tool_list_text = "\n".join(
            f"{i+1}. {entry['tool']['name']} ({entry['server']['label']}): "
            f"{entry['tool'].get('description','')}"
            for i, entry in enumerate(all_tools)
        )
        suggest_prompt = (
            f"User request: {user_prompt}\n\n"
            f"Available tools:\n{tool_list_text}\n\n"
            f"Which tool number best matches the user's request? "
            f"Reply with ONLY the number, nothing else."
        )
        from google.genai import types as gtypes
        suggest_resp = gemini_client.models.generate_content(
            model="gemini-3-flash-preview",
            contents=suggest_prompt,
        )
        suggested_idx = int(suggest_resp.text.strip()) - 1
        if 0 <= suggested_idx < len(all_tools):
            suggested_name = all_tools[suggested_idx]["tool"]["name"]
            print(f"  {GREEN}AI suggests:{RESET} [{suggested_idx+1}] {suggested_name}")
        else:
            suggested_idx = None
    except Exception:
        suggested_idx = None

default_str = f" (default: {suggested_idx+1})" if suggested_idx is not None else ""
while True:
    try:
        choice = input(f"{BOLD}Select tool [1-{len(all_tools)}]{default_str}: {RESET}").strip()
        if choice == "" and suggested_idx is not None:
            tool_idx = suggested_idx
            break
        idx = int(choice) - 1
        if 0 <= idx < len(all_tools):
            tool_idx = idx
            break
        print(f"    Enter a number between 1 and {len(all_tools)}")
    except (ValueError, KeyboardInterrupt):
        print("\nBye!")
        sys.exit(0)

selected_server = all_tools[tool_idx]["server"]
selected_tool   = all_tools[tool_idx]["tool"]
ok(f"Selected: {selected_tool['name']} on {selected_server['label']}")

# ─────────────────────────────────────────────────────────────────────────────
# Step 4 — Build tool arguments
# ─────────────────────────────────────────────────────────────────────────────

hdr("Preparing tool arguments...")

tool_schema  = selected_tool.get("inputSchema", {})
required     = tool_schema.get("required", [])
properties   = tool_schema.get("properties", {})
tool_args: dict = {}

if USE_AI and gemini_client:
    # Ask Gemini to extract the right arguments from the user's prompt
    try:
        schema_text = json.dumps(properties, indent=2)
        arg_prompt  = (
            f"The user wants to call the tool '{selected_tool['name']}'.\n"
            f"Tool description: {selected_tool.get('description','')}\n\n"
            f"Tool input schema (properties):\n{schema_text}\n\n"
            f"User's request: {user_prompt}\n\n"
            f"Extract the arguments for this tool from the user's request.\n"
            f"Return ONLY a valid JSON object with the argument values. "
            f"No markdown, no explanation, just the JSON object."
        )
        from google.genai import types as gtypes
        arg_resp = gemini_client.models.generate_content(
            model="gemini-3-flash-preview",
            contents=arg_prompt,
        )
        raw = arg_resp.text.strip().replace("```json","").replace("```","").strip()
        tool_args = json.loads(raw)
        ok("Arguments extracted from your prompt:")
        for k, v in tool_args.items():
            v_str = str(v)[:60] + ("..." if len(str(v)) > 60 else "")
            dim(f"  {k}: {v_str}")
    except Exception as e:
        warn(f"AI argument extraction failed ({e}) — switching to manual entry.")
        USE_AI = False

if not USE_AI or not tool_args:
    # Manual argument entry
    print(f"  {DIM}Enter values for each argument (press Enter to skip optional ones):{RESET}\n")
    for prop_name, prop_def in properties.items():
        is_required = prop_name in required
        prop_type   = prop_def.get("type", "string")
        prop_desc   = prop_def.get("description", "")
        req_marker  = f"{RED}*required{RESET}" if is_required else f"{DIM}optional{RESET}"

        print(f"  {BOLD}{prop_name}{RESET} ({prop_type}) [{req_marker}]")
        print(f"  {DIM}{prop_desc}{RESET}")

        try:
            val = input("  > ").strip()
        except KeyboardInterrupt:
            print("\nBye!")
            sys.exit(0)

        if val:
            # Cast to int if schema says integer
            if prop_type == "integer":
                try:
                    val = int(val)
                except ValueError:
                    pass
            tool_args[prop_name] = val
        elif is_required:
            err(f"'{prop_name}' is required.")
            sys.exit(1)
        print()

# ─────────────────────────────────────────────────────────────────────────────
# Step 5 — Execute the paid tool call
# ─────────────────────────────────────────────────────────────────────────────

hdr(f"Calling {selected_tool['name']} on {selected_server['label']}...")
print(f"  {DIM}Payment will be handled automatically.{RESET}\n")

result = paid_mcp_call(
    selected_server["url"],
    "tools/call",
    {"name": selected_tool["name"], "arguments": tool_args},
)

# ─────────────────────────────────────────────────────────────────────────────
# Step 6 — Print result
# ─────────────────────────────────────────────────────────────────────────────

hdr("Result")
print(LINE)

if result is None:
    err("Tool call failed — no result returned.")
    sys.exit(1)

content = result.get("content", [])
if not content:
    warn("Tool returned an empty result.")
    dim(json.dumps(result, indent=2))
else:
    for item in content:
        if item.get("type") == "text":
            raw_text = item.get("text", "")
            # Try to pretty-print if it's JSON
            try:
                parsed = json.loads(raw_text)
                # Find the main text field to print nicely
                main_fields = ["analysis", "rewritten", "summary",
                               "answer", "review", "sentiment"]
                printed = False
                for field in main_fields:
                    if field in parsed:
                        print(f"\n  {parsed[field]}\n")
                        printed = True
                        # Print metadata dimly
                        meta = {k: v for k, v in parsed.items() if k != field}
                        if meta:
                            print(f"  {DIM}── metadata ──{RESET}")
                            for k, v in meta.items():
                                dim(f"  {k}: {v}")
                        break
                if not printed:
                    # No known main field — print all nicely
                    for k, v in parsed.items():
                        label_str = f"{BOLD}{k}{RESET}"
                        val_str   = str(v)
                        if len(val_str) > 120:
                            print(f"\n  {label_str}:\n  {val_str}\n")
                        else:
                            print(f"  {label_str}: {val_str}")
            except (json.JSONDecodeError, TypeError):
                # Not JSON — print raw
                print(f"\n  {raw_text}\n")

print(LINE)
print()
ok("Done.")
print(f"  Tool:    {selected_tool['name']}")
print(f"  Server:  {selected_server['label']}")
print(f"  Wallet:  {account.address}")
print(f"  Cost:    $0.001 USDC (paid via x402)")
print()