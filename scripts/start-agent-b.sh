#!/usr/bin/env bash
# ============================================================================
# start-agent-d.sh — Launch Agent D (Full A2A Orchestrator)
#
# What this script starts:
#   1. Agent D's A2A server (a2a_agent.py) — on port 9034
#      • POST /    → PAID via x402 (callers pay $0.001 USDC)
#      • GET  /.well-known/agent.json → free (agent card)
#      • GET  /health → free
#
# What AXL registration does:
#   Agent D registers itself with AXL Node D so peers on the mesh can
#   reach it. Peers call:  POST /a2a/{node_d_key}
#   AXL Node D forwards those to:  http://127.0.0.1:9034/
#
# Required env vars (in .env or exported):
#   AGENT_D_WALLET_ADDRESS    — EVM address that receives payments
#   AGENT_D_EVM_PRIVATE_KEY   — Private key that makes outgoing payments
#   GEMINI_API_KEY            — Gemini API key for the brain
#
# Optional env vars:
#   AGENT_D_AXL_API           — AXL node API (default: http://127.0.0.1:9032)
#   AGENT_D_A2A_PORT          — A2A server port (default: 9034)
#   AGENT_D_AXL_CONFIG        — AXL node config file (default: node-config-d.json)
#
# Usage:
#   bash scripts/start-agent-d.sh
# ============================================================================
set -euo pipefail

PROJECT_ROOT="$(cd "$(dirname "$0")/.." && pwd)"

# ---------------------------------------------------------------------------
# Load .env
# ---------------------------------------------------------------------------
if [ -f "$PROJECT_ROOT/.env" ]; then
    echo "Loading environment from $PROJECT_ROOT/.env"
    set -a
    source "$PROJECT_ROOT/.env"
    set +a
fi

# Activate venv
if [ -f "$PROJECT_ROOT/.venv/bin/activate" ]; then
    source "$PROJECT_ROOT/.venv/bin/activate"
fi

# ---------------------------------------------------------------------------
# Validate required env vars
# ---------------------------------------------------------------------------
MISSING=0
for VAR in AGENT_D_WALLET_ADDRESS AGENT_D_EVM_PRIVATE_KEY GEMINI_API_KEY; do
    if [ -z "${!VAR:-}" ]; then
        echo "ERROR: $VAR is not set"
        MISSING=1
    fi
done
if [ "$MISSING" -eq 1 ]; then
    echo ""
    echo "Set the missing vars in your .env file or export them:"
    echo "  AGENT_D_WALLET_ADDRESS  — EVM address that receives payments"
    echo "  AGENT_D_EVM_PRIVATE_KEY — Private key for making outgoing payments"
    echo "  GEMINI_API_KEY          — Gemini API key"
    exit 1
fi

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------
AXL_DIR="$PROJECT_ROOT/axl"
AGENT_D_DIR="$PROJECT_ROOT/agents/agent-d"

A2A_PORT="${AGENT_D_A2A_PORT:-9034}"
AXL_API="${AGENT_D_AXL_API:-http://127.0.0.1:9032}"
AXL_CONFIG="${AGENT_D_AXL_CONFIG:-node-config-d.json}"
SERVICE_NAME="agent-d"

echo ""
echo "============================================================"
echo "  AgentMesh — Starting Agent D (Full A2A Orchestrator)"
echo "============================================================"
echo ""
echo "  Receives payments:  $AGENT_D_WALLET_ADDRESS"
echo "  AXL node:           $AXL_API"
echo "  A2A port:           $A2A_PORT"
echo "  Service name:       $SERVICE_NAME"
echo ""

# ---------------------------------------------------------------------------
# Step 1: Start the AXL node for Agent D (if not already running)
# ---------------------------------------------------------------------------
echo "[1/3] Checking AXL node for Agent D ($AXL_API)..."

AXL_PID=""
if curl -s "$AXL_API/topology" > /dev/null 2>&1; then
    echo "       ✓ AXL node already running at $AXL_API"
else
    echo "       AXL node not running — starting it..."

    if [ ! -f "$AXL_DIR/$AXL_CONFIG" ]; then
        echo "       ✗ AXL config not found: $AXL_DIR/$AXL_CONFIG"
        echo "         Create it or set AGENT_D_AXL_CONFIG to point to an existing config."
        echo "         Example config:"
        echo "           {\"api_port\": 9032, \"p2p_port\": 9033, \"bootstrap_peers\": [...]}"
        exit 1
    fi

    cd "$AXL_DIR"
    ./node -config "$AXL_CONFIG" &
    AXL_PID=$!
    echo "       PID: $AXL_PID"

    echo "       Waiting for AXL node..."
    AXL_STARTED=0
    for i in $(seq 1 20); do
        if curl -s "$AXL_API/topology" > /dev/null 2>&1; then
            echo "       ✓ AXL node is running"
            AXL_STARTED=1
            break
        fi
        sleep 1
    done

    if [ "$AXL_STARTED" -eq 0 ]; then
        echo "       ✗ AXL node failed to start"
        kill $AXL_PID 2>/dev/null || true
        exit 1
    fi
fi

# ---------------------------------------------------------------------------
# Step 2: Start Agent D's A2A server
# ---------------------------------------------------------------------------
echo ""
echo "[2/3] Starting Agent D A2A server on port $A2A_PORT..."

cd "$AGENT_D_DIR"

# Export config for the Python process
export AGENT_D_AXL_API="$AXL_API"
export AGENT_D_A2A_PORT="$A2A_PORT"

python3 a2a_agent.py &
AGENT_D_PID=$!
echo "       PID: $AGENT_D_PID"

echo "       Waiting for A2A server to start..."
for i in $(seq 1 20); do
    if curl -s "http://127.0.0.1:$A2A_PORT/health" > /dev/null 2>&1; then
        echo "       ✓ Agent D A2A server is running"
        break
    fi
    if [ $i -eq 20 ]; then
        echo "       ✗ Agent D failed to start"
        [ -n "${AXL_PID:-}" ] && kill $AXL_PID 2>/dev/null || true
        kill $AGENT_D_PID 2>/dev/null || true
        exit 1
    fi
    sleep 1
done

# Show health
echo ""
echo "       Health check:"
curl -s "http://127.0.0.1:$A2A_PORT/health" | python3 -m json.tool 2>/dev/null || true
echo ""

# Show agent card
echo "       Agent card:"
curl -s "http://127.0.0.1:$A2A_PORT/.well-known/agent.json" | python3 -m json.tool 2>/dev/null || true
echo ""

# ---------------------------------------------------------------------------
# Step 3: Register Agent D's A2A server with the AXL node
#
# This tells the AXL node: "when you receive an A2A message for this node,
# forward it to localhost:A2A_PORT"
#
# After registration, peers on the mesh can reach Agent D via:
#   POST /a2a/{node_d_public_key}
# ---------------------------------------------------------------------------
echo "[3/3] Registering Agent D with AXL node..."

# Get our public key from topology
OUR_KEY=$(curl -s "$AXL_API/topology" | python3 -c "
import sys, json
data = json.load(sys.stdin)
print(data.get('our_public_key', ''))
" 2>/dev/null || echo "")

if [ -z "$OUR_KEY" ]; then
    echo "       ⚠ Could not read public key from AXL topology"
    echo "         Agent D is running but may not be reachable via mesh"
else
    echo "       Node public key: ${OUR_KEY:0:16}..."
fi

# Register the A2A service with the AXL node
# This maps incoming /a2a/{our_key} requests → our local A2A server
REG_RESPONSE=$(curl -s -X POST "$AXL_API/register/a2a" \
    -H "Content-Type: application/json" \
    -d "{
        \"service\": \"$SERVICE_NAME\",
        \"endpoint\": \"http://127.0.0.1:$A2A_PORT\",
        \"type\": \"a2a\"
    }" 2>/dev/null || echo "{\"error\": \"registration endpoint not available\"}")

echo "       Registration response: $REG_RESPONSE"

# Some AXL versions use a different registration endpoint — try the MCP one too
# so Agent D can also be discovered via MCP-style probing
REG_MCP=$(curl -s -X POST "$AXL_API/register" \
    -H "Content-Type: application/json" \
    -d "{
        \"service\": \"$SERVICE_NAME\",
        \"endpoint\": \"http://127.0.0.1:$A2A_PORT\"
    }" 2>/dev/null || echo "{\"skipped\": true}")

echo "       MCP-compat registration: $REG_MCP"

# ---------------------------------------------------------------------------
# Done
# ---------------------------------------------------------------------------
echo ""
echo "============================================================"
echo "  ✓ Agent D is fully operational!"
echo "============================================================"
echo ""
echo "  A2A server:     http://127.0.0.1:$A2A_PORT"
echo "  Agent card:     http://127.0.0.1:$A2A_PORT/.well-known/agent.json"
echo "  Health:         http://127.0.0.1:$A2A_PORT/health"
echo "  AXL node:       $AXL_API"
if [ -n "$OUR_KEY" ]; then
    echo "  Mesh A2A URL:   $AXL_API/a2a/$OUR_KEY"
fi
echo ""
echo "  HOW PEERS CALL AGENT D:"
echo "  ─────────────────────────────────────────────────────────"
echo "  1. GET  $AXL_API/a2a/$OUR_KEY    (fetch agent card, free)"
echo "  2. POST $AXL_API/a2a/$OUR_KEY    (send message, pay \$0.001 USDC)"
echo ""
echo "  WHAT AGENT D CAN CALL (peer discovery ran at startup):"
curl -s "http://127.0.0.1:$A2A_PORT/health" | python3 -c "
import sys, json
try:
    data = json.load(sys.stdin)
    peers = data.get('peer_services', [])
    if peers:
        for p in peers:
            print(f'    • {p}')
    else:
        print('    (none yet — POST /registry/refresh once peers are up)')
except:
    pass
" 2>/dev/null || true
echo ""
echo "  PIDs: A2A=$AGENT_D_PID  AXL=${AXL_PID:-(pre-existing)}"
echo ""
echo "  Press Ctrl+C to stop."
echo ""

# ---------------------------------------------------------------------------
# Cleanup on exit
# ---------------------------------------------------------------------------
cleanup() {
    echo ""
    echo "Shutting down Agent D..."

    # Deregister from AXL
    curl -s -X DELETE "$AXL_API/register/a2a/$SERVICE_NAME" > /dev/null 2>&1 || true
    curl -s -X DELETE "$AXL_API/register/$SERVICE_NAME" > /dev/null 2>&1 || true

    kill $AGENT_D_PID 2>/dev/null || true
    [ -n "${AXL_PID:-}" ] && kill $AXL_PID 2>/dev/null || true

    echo "Done."
}
trap cleanup EXIT INT TERM

wait