#!/usr/bin/env bash
# ============================================================================
# start-agent-b.sh — Start Agent B's full stack:
#                    MCP service + MCP Router + A2A Server
#
# After this runs, Agent B can be reached two ways:
#   MCP style:  POST /mcp/{node_b_key}/agentmesh  → tools/call
#   A2A style:  POST /a2a/{node_b_key}             → message/send
#
# Usage: bash scripts/start-agent-b.sh
# ============================================================================
set -euo pipefail

PROJECT_ROOT="$(cd "$(dirname "$0")/.." && pwd)"

if [ -f "$PROJECT_ROOT/.env" ]; then
    echo "Loading environment from $PROJECT_ROOT/.env"
    set -a
    source "$PROJECT_ROOT/.env"
    set +a
fi

if [ -f "$PROJECT_ROOT/.venv/bin/activate" ]; then
    source "$PROJECT_ROOT/.venv/bin/activate"
fi

if [ -z "${AGENT_B_WALLET_ADDRESS:-}" ]; then
    echo "ERROR: AGENT_B_WALLET_ADDRESS is not set"
    exit 1
fi

AXL_DIR="$PROJECT_ROOT/axl"
AGENT_B_DIR="$PROJECT_ROOT/agents/agent-b"

AGENT_B_PORT=7100
ROUTER_PORT=9003
A2A_PORT=9004
SERVICE_NAME="agentmesh"

echo "============================================"
echo "  AgentMesh — Starting Agent B (MCP + A2A)"
echo "============================================"
echo ""
echo "  x402 pay-to wallet: $AGENT_B_WALLET_ADDRESS"

# ---------------------------------------------------------------------------
# 1. Start Agent B Flask MCP server
# ---------------------------------------------------------------------------
echo ""
echo "[1/4] Starting Agent B MCP service on port $AGENT_B_PORT..."

cd "$AGENT_B_DIR"
python3 server.py &
AGENT_B_PID=$!
echo "       PID: $AGENT_B_PID"

for i in $(seq 1 15); do
    if curl -s http://127.0.0.1:$AGENT_B_PORT/health > /dev/null 2>&1; then
        echo "       ✓ Agent B MCP server is running"
        break
    fi
    if [ $i -eq 15 ]; then
        echo "       ✗ Failed to start"
        kill $AGENT_B_PID 2>/dev/null || true
        exit 1
    fi
    sleep 1
done

# ---------------------------------------------------------------------------
# 2. Start MCP Router
# ---------------------------------------------------------------------------
echo ""
echo "[2/4] Starting MCP Router on port $ROUTER_PORT..."

cd "$AXL_DIR/integrations"
python3 -m mcp_routing.mcp_router --port $ROUTER_PORT &
ROUTER_PID=$!
echo "       PID: $ROUTER_PID"

for i in $(seq 1 15); do
    if curl -s http://127.0.0.1:$ROUTER_PORT/health > /dev/null 2>&1; then
        echo "       ✓ MCP Router is running"
        break
    fi
    if [ $i -eq 15 ]; then
        echo "       ✗ Failed to start"
        kill $AGENT_B_PID 2>/dev/null || true
        kill $ROUTER_PID 2>/dev/null || true
        exit 1
    fi
    sleep 1
done

# ---------------------------------------------------------------------------
# 3. Register the MCP service
# ---------------------------------------------------------------------------
echo ""
echo "[3/4] Registering '$SERVICE_NAME' with MCP Router..."

REGISTER_RESPONSE=$(curl -s -X POST http://127.0.0.1:$ROUTER_PORT/register \
    -H "Content-Type: application/json" \
    -d "{\"service\": \"$SERVICE_NAME\", \"endpoint\": \"http://127.0.0.1:$AGENT_B_PORT/mcp\"}")

echo "       Response: $REGISTER_RESPONSE"
echo ""
echo "       Registered services:"
curl -s http://127.0.0.1:$ROUTER_PORT/services | python3 -m json.tool

# ---------------------------------------------------------------------------
# 4. Start Agent B's A2A server
# ---------------------------------------------------------------------------
echo ""
echo "[4/4] Starting Agent B A2A server on port $A2A_PORT..."

cd "$AGENT_B_DIR"
python3 a2a_server.py &
A2A_PID=$!
echo "       PID: $A2A_PID"

for i in $(seq 1 15); do
    if curl -s http://127.0.0.1:$A2A_PORT/health > /dev/null 2>&1; then
        echo "       ✓ A2A server is running"
        break
    fi
    if [ $i -eq 15 ]; then
        echo "       ✗ A2A server failed to start (MCP still works)"
        A2A_PID=""
        break
    fi
    sleep 1
done

# ---------------------------------------------------------------------------
# Done
# ---------------------------------------------------------------------------
echo ""
echo "============================================"
echo "  ✓ Agent B fully ready!"
echo "============================================"
echo ""
echo "  Agent B MCP:    http://127.0.0.1:$AGENT_B_PORT/mcp"
echo "  MCP Router:     http://127.0.0.1:$ROUTER_PORT"
echo "  A2A Server:     http://127.0.0.1:$A2A_PORT"
echo "  Agent card:     http://127.0.0.1:$A2A_PORT/.well-known/agent.json"
echo ""
echo "  Agent B can now be reached TWO ways:"
echo "    MCP:  POST /mcp/{node_b_key}/agentmesh  (tool-call style)"
echo "    A2A:  POST /a2a/{node_b_key}             (conversational style)"
echo ""
echo "  PIDs: MCP=$AGENT_B_PID  Router=$ROUTER_PID  A2A=${A2A_PID:-skipped}"
echo ""
echo "  Press Ctrl+C to stop all services."
echo ""

cleanup() {
    echo ""
    echo "Shutting down..."
    curl -s -X DELETE "http://127.0.0.1:$ROUTER_PORT/register/$SERVICE_NAME" > /dev/null 2>&1 || true
    kill $AGENT_B_PID 2>/dev/null || true
    kill $ROUTER_PID 2>/dev/null || true
    [ -n "${A2A_PID:-}" ] && kill $A2A_PID 2>/dev/null || true
    echo "Done."
}
trap cleanup EXIT INT TERM

wait