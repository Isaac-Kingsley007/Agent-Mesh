#!/usr/bin/env bash
# ============================================================================
# start-agent-a.sh — Start Agent A's MCP service + MCP Router + A2A Server
#
# Usage: bash scripts/start-agent-a.sh
# Run from the openagents project root.
# ============================================================================
set -euo pipefail

PROJECT_ROOT="$(cd "$(dirname "$0")/.." && pwd)"

# Load .env
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

# Validate
if [ -z "${AGENT_A_EVM_ADDRESS:-}" ]; then
    echo "ERROR: AGENT_A_EVM_ADDRESS is not set"
    exit 1
fi

AXL_DIR="$PROJECT_ROOT/axl"
AGENT_A_DIR="$PROJECT_ROOT/agents/agent-a"

AGENT_A_PORT=7200
ROUTER_PORT=9013
A2A_PORT=9014
SERVICE_NAME="agent-a"

echo "============================================"
echo "  AgentMesh — Starting Agent A Services"
echo "============================================"
echo ""
echo "  x402 pay-to wallet: $AGENT_A_EVM_ADDRESS"

# 1. Start Agent A Flask MCP server
echo ""
echo "[1/3] Starting Agent A MCP service on port $AGENT_A_PORT..."

cd "$AGENT_A_DIR"
python3 server.py &
AGENT_A_PID=$!
echo "       PID: $AGENT_A_PID"

echo "       Waiting for server to start..."
for i in $(seq 1 15); do
    if curl -s http://127.0.0.1:$AGENT_A_PORT/health > /dev/null 2>&1; then
        echo "       ✓ Agent A server is running"
        break
    fi
    if [ $i -eq 15 ]; then
        echo "       ✗ Agent A server failed to start"
        kill $AGENT_A_PID 2>/dev/null || true
        exit 1
    fi
    sleep 1
done

# 2. Start MCP Router
echo ""
echo "[2/3] Starting MCP Router on port $ROUTER_PORT..."

cd "$AXL_DIR/integrations"
python3 -m mcp_routing.mcp_router --port $ROUTER_PORT &
ROUTER_PID=$!
echo "       PID: $ROUTER_PID"

echo "       Waiting for router to start..."
for i in $(seq 1 15); do
    if curl -s http://127.0.0.1:$ROUTER_PORT/health > /dev/null 2>&1; then
        echo "       ✓ MCP Router is running"
        break
    fi
    if [ $i -eq 15 ]; then
        echo "       ✗ MCP Router failed to start"
        kill $AGENT_A_PID 2>/dev/null || true
        kill $ROUTER_PID 2>/dev/null || true
        exit 1
    fi
    sleep 1
done

# 3. Register the service
echo ""
echo "[3/3] Registering '$SERVICE_NAME' service with MCP Router..."

REGISTER_RESPONSE=$(curl -s -X POST http://127.0.0.1:$ROUTER_PORT/register \
    -H "Content-Type: application/json" \
    -d "{\"service\": \"$SERVICE_NAME\", \"endpoint\": \"http://127.0.0.1:$AGENT_A_PORT/mcp\"}")

echo "       Response: $REGISTER_RESPONSE"

echo ""
echo "       Registered services:"
curl -s http://127.0.0.1:$ROUTER_PORT/services | python3 -m json.tool

echo ""
echo "============================================"
echo "  ✓ Agent A services are ready!"
echo "============================================"
echo ""
echo "  Agent A MCP:    http://127.0.0.1:$AGENT_A_PORT/mcp"
echo "  MCP Router:     http://127.0.0.1:$ROUTER_PORT"
echo "  Service name:   $SERVICE_NAME"
echo ""
echo "  PIDs: Agent A=$AGENT_A_PID  Router=$ROUTER_PID"
echo ""
echo "  Press Ctrl+C to stop all services."
echo ""

cleanup() {
    echo ""
    echo "Shutting down..."
    curl -s -X DELETE "http://127.0.0.1:$ROUTER_PORT/register/$SERVICE_NAME" > /dev/null 2>&1 || true
    kill $AGENT_A_PID 2>/dev/null || true
    kill $ROUTER_PID 2>/dev/null || true
    echo "Done."
}
trap cleanup EXIT INT TERM

wait
