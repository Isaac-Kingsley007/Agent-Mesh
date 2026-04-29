#!/usr/bin/env bash
# ============================================================================
# start-agent-b.sh — Start Agent B's MCP service + AXL MCP router
#
# Usage: bash scripts/start-agent-b.sh
# Run from the openagents project root.
# ============================================================================
set -euo pipefail

PROJECT_ROOT="$(cd "$(dirname "$0")/.." && pwd)"
AXL_DIR="$PROJECT_ROOT/axl"
AGENT_B_DIR="$PROJECT_ROOT/agents/agent-b"

AGENT_B_PORT=7100
ROUTER_PORT=9003
SERVICE_NAME="agentmesh"

echo "============================================"
echo "  AgentMesh — Starting Agent B"
echo "============================================"

# ---------------------------------------------------------------------------
# 1. Start Agent B Flask MCP server
# ---------------------------------------------------------------------------
echo ""
echo "[1/3] Starting Agent B MCP service on port $AGENT_B_PORT..."

cd "$AGENT_B_DIR"
python3 server.py &
AGENT_B_PID=$!
echo "       PID: $AGENT_B_PID"

# Wait for Flask to be ready
echo "       Waiting for server to start..."
for i in $(seq 1 15); do
    if curl -s http://127.0.0.1:$AGENT_B_PORT/health > /dev/null 2>&1; then
        echo "       ✓ Agent B is running"
        break
    fi
    if [ $i -eq 15 ]; then
        echo "       ✗ Agent B failed to start"
        kill $AGENT_B_PID 2>/dev/null || true
        exit 1
    fi
    sleep 1
done

# ---------------------------------------------------------------------------
# 2. Start MCP Router
# ---------------------------------------------------------------------------
echo ""
echo "[2/3] Starting MCP Router on port $ROUTER_PORT..."

cd "$AXL_DIR/integrations"
python3 -m mcp_routing.mcp_router --port $ROUTER_PORT &
ROUTER_PID=$!
echo "       PID: $ROUTER_PID"

# Wait for router to be ready
echo "       Waiting for router to start..."
for i in $(seq 1 15); do
    if curl -s http://127.0.0.1:$ROUTER_PORT/health > /dev/null 2>&1; then
        echo "       ✓ MCP Router is running"
        break
    fi
    if [ $i -eq 15 ]; then
        echo "       ✗ MCP Router failed to start"
        kill $AGENT_B_PID 2>/dev/null || true
        kill $ROUTER_PID 2>/dev/null || true
        exit 1
    fi
    sleep 1
done

# ---------------------------------------------------------------------------
# 3. Register the agentmesh service
# ---------------------------------------------------------------------------
echo ""
echo "[3/3] Registering '$SERVICE_NAME' service with MCP Router..."

REGISTER_RESPONSE=$(curl -s -X POST http://127.0.0.1:$ROUTER_PORT/register \
    -H "Content-Type: application/json" \
    -d "{\"service\": \"$SERVICE_NAME\", \"endpoint\": \"http://127.0.0.1:$AGENT_B_PORT/mcp\"}")

echo "       Response: $REGISTER_RESPONSE"

# Verify registration
echo ""
echo "       Registered services:"
curl -s http://127.0.0.1:$ROUTER_PORT/services | python3 -m json.tool

# ---------------------------------------------------------------------------
# Done
# ---------------------------------------------------------------------------
echo ""
echo "============================================"
echo "  ✓ Agent B is ready!"
echo "============================================"
echo ""
echo "  Agent B MCP:  http://127.0.0.1:$AGENT_B_PORT/mcp"
echo "  MCP Router:   http://127.0.0.1:$ROUTER_PORT"
echo "  Service name: $SERVICE_NAME"
echo ""
echo "  PIDs: Agent B=$AGENT_B_PID  Router=$ROUTER_PID"
echo ""
echo "  NEXT STEPS:"
echo "  1. Update node-config-2.json to add router_addr/router_port"
echo "  2. Restart Node B:  cd axl && ./node -config node-config-2.json"
echo "  3. Run: bash scripts/verify.sh"
echo ""
echo "  Press Ctrl+C to stop both services."
echo ""

# Cleanup on exit
cleanup() {
    echo ""
    echo "Shutting down..."
    # Deregister service
    curl -s -X DELETE "http://127.0.0.1:$ROUTER_PORT/register/$SERVICE_NAME" > /dev/null 2>&1 || true
    kill $AGENT_B_PID 2>/dev/null || true
    kill $ROUTER_PID 2>/dev/null || true
    echo "Done."
}
trap cleanup EXIT INT TERM

# Keep running
wait
