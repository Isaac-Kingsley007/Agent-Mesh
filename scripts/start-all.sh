#!/usr/bin/env bash
# ============================================================================
# start-all.sh — Start the entire AgentMesh network in one command
#
# Starts in order:
#   Node A  (api:9002  | router:9013 | a2a:9014)
#   Node B  (api:9012  | router:9003 | no a2a)
#   Node C  (api:9022  | router:9023 | a2a:9024)
#
# Per agent, this script starts:
#   • AXL node        (the P2P mesh node binary)
#   • MCP server      (Flask — server.py)
#   • MCP router      (routes /mcp/{key}/{service} → Flask)
#   • A2A server      (a2a_agent.py — where config has a2a_port)
#   Then registers each service with its AXL node.
#
# Usage:
#   bash scripts/start-all.sh
#
# Stop everything:
#   Ctrl+C  (cleanup trap kills all PIDs)
# ============================================================================
set -euo pipefail

PROJECT_ROOT="$(cd "$(dirname "$0")/.." && pwd)"

# ── Load .env ───────────────────────────────────────────────────────────────
if [ -f "$PROJECT_ROOT/.env" ]; then
    echo "Loading $PROJECT_ROOT/.env"
    set -a; source "$PROJECT_ROOT/.env"; set +a
fi

# ── Activate venv ───────────────────────────────────────────────────────────
if [ -f "$PROJECT_ROOT/.venv/bin/activate" ]; then
    source "$PROJECT_ROOT/.venv/bin/activate"
fi

# ── Validate required env vars ──────────────────────────────────────────────
MISSING=0
for VAR in \
    AGENT_A_EVM_ADDRESS AGENT_A_EVM_PRIVATE_KEY \
    AGENT_B_WALLET_ADDRESS AGENT_B_EVM_PRIVATE_KEY \
    AGENT_C_WALLET_ADDRESS AGENT_C_EVM_PRIVATE_KEY \
    GEMINI_API_KEY; do
    if [ -z "${!VAR:-}" ]; then
        echo "ERROR: $VAR is not set"
        MISSING=1
    fi
done
[ "$MISSING" -eq 1 ] && echo "" && echo "Fix missing vars in .env then retry." && exit 1

# ── Paths ────────────────────────────────────────────────────────────────────
AXL_DIR="$PROJECT_ROOT/axl"
AGENT_A_DIR="$PROJECT_ROOT/agents/agent-a"
AGENT_B_DIR="$PROJECT_ROOT/agents/agent-b"
AGENT_C_DIR="$PROJECT_ROOT/agents/agent-c"
INTEGRATIONS_DIR="$AXL_DIR/integrations"

# ── Port map (must match node-config*.json exactly) ─────────────────────────
#   Agent A — node-config.json
A_AXL_API="http://127.0.0.1:9002"
A_AXL_CONFIG="node-config.json"
A_MCP_PORT=7200
A_ROUTER_PORT=9013
A_A2A_PORT=9014
A_SERVICE_NAME="agent-a"

#   Agent B — node-config-2.json  (no a2a_port in config)
B_AXL_API="http://127.0.0.1:9012"
B_AXL_CONFIG="node-config-2.json"
B_MCP_PORT=7100
B_ROUTER_PORT=9003
B_SERVICE_NAME="agentmesh"

#   Agent C — node-config-3.json
C_AXL_API="http://127.0.0.1:9022"
C_AXL_CONFIG="node-config-3.json"
C_MCP_PORT=7300
C_ROUTER_PORT=9023
C_A2A_PORT=9024
C_SERVICE_NAME="agent-c"

# ── PID tracking (all background processes) ──────────────────────────────────
ALL_PIDS=()

# ── Helper: wait for an HTTP endpoint to respond ─────────────────────────────
wait_for() {
    local url="$1" label="$2" max="${3:-20}"
    for i in $(seq 1 "$max"); do
        if curl -s "$url" > /dev/null 2>&1; then
            echo "       ✓ $label is ready"
            return 0
        fi
        sleep 1
    done
    echo "       ✗ $label failed to start (timeout)"
    return 1
}

# ── Helper: register an MCP service with its router ─────────────────────────
register_mcp() {
    local router_port="$1" service="$2" endpoint="$3"
    curl -s -X POST "http://127.0.0.1:$router_port/register" \
        -H "Content-Type: application/json" \
        -d "{\"service\": \"$service\", \"endpoint\": \"$endpoint\"}" \
        > /dev/null
    echo "       ✓ MCP service '$service' registered → $endpoint"
}

# ── Helper: register A2A server with AXL node ───────────────────────────────
register_a2a() {
    local axl_api="$1" service="$2" a2a_port="$3"
    # Try the a2a-specific endpoint first, fall back to generic register
    curl -s -X POST "$axl_api/register/a2a" \
        -H "Content-Type: application/json" \
        -d "{\"service\": \"$service\", \"endpoint\": \"http://127.0.0.1:$a2a_port\", \"type\": \"a2a\"}" \
        > /dev/null 2>&1 || true
    curl -s -X POST "$axl_api/register" \
        -H "Content-Type: application/json" \
        -d "{\"service\": \"$service\", \"endpoint\": \"http://127.0.0.1:$a2a_port\"}" \
        > /dev/null 2>&1 || true
    echo "       ✓ A2A service '$service' registered → port $a2a_port"
}

# ── Cleanup: kill everything on Ctrl+C or exit ───────────────────────────────
cleanup() {
    echo ""
    echo "════════════════════════════════════════"
    echo "  Shutting down AgentMesh..."
    echo "════════════════════════════════════════"

    # Deregister MCP services gracefully
    curl -s -X DELETE "http://127.0.0.1:$A_ROUTER_PORT/register/$A_SERVICE_NAME" > /dev/null 2>&1 || true
    curl -s -X DELETE "http://127.0.0.1:$B_ROUTER_PORT/register/$B_SERVICE_NAME" > /dev/null 2>&1 || true
    curl -s -X DELETE "http://127.0.0.1:$C_ROUTER_PORT/register/$C_SERVICE_NAME" > /dev/null 2>&1 || true

    # Kill all background processes
    for pid in "${ALL_PIDS[@]}"; do
        kill "$pid" 2>/dev/null || true
    done

    echo "  Done. All processes stopped."
}
trap cleanup EXIT INT TERM

# ════════════════════════════════════════════════════════════════════════════
echo ""
echo "════════════════════════════════════════════════════════════"
echo "  AgentMesh — Starting All Agents"
echo "════════════════════════════════════════════════════════════"
echo ""

# ════════════════════════════════════════════════════════════════════════════
echo "── AGENT A ─────────────────────────────────────────────────"
echo "   AXL: $A_AXL_API  |  MCP: $A_MCP_PORT  |  Router: $A_ROUTER_PORT  |  A2A: $A_A2A_PORT"
echo ""

# [A1] AXL Node A
echo "  [A1] Starting AXL Node A..."
cd "$AXL_DIR"
./node -config "$A_AXL_CONFIG" &
AXL_A_PID=$!
ALL_PIDS+=($AXL_A_PID)
echo "       PID: $AXL_A_PID"
wait_for "$A_AXL_API/topology" "AXL Node A"

# [A2] Agent A MCP server
echo "  [A2] Starting Agent A MCP server (port $A_MCP_PORT)..."
cd "$AGENT_A_DIR"
python3 server.py &
MCP_A_PID=$!
ALL_PIDS+=($MCP_A_PID)
echo "       PID: $MCP_A_PID"
wait_for "http://127.0.0.1:$A_MCP_PORT/health" "Agent A MCP server"

# [A3] MCP Router A
echo "  [A3] Starting MCP Router A (port $A_ROUTER_PORT)..."
cd "$INTEGRATIONS_DIR"
python3 -m mcp_routing.mcp_router --port $A_ROUTER_PORT &
ROUTER_A_PID=$!
ALL_PIDS+=($ROUTER_A_PID)
echo "       PID: $ROUTER_A_PID"
wait_for "http://127.0.0.1:$A_ROUTER_PORT/health" "MCP Router A"

# [A4] Register Agent A MCP service
echo "  [A4] Registering MCP service '$A_SERVICE_NAME'..."
register_mcp "$A_ROUTER_PORT" "$A_SERVICE_NAME" "http://127.0.0.1:$A_MCP_PORT/mcp"

# [A5] Agent A A2A server
echo "  [A5] Starting Agent A A2A server (port $A_A2A_PORT)..."
cd "$AGENT_A_DIR"
export AGENT_A_AXL_API="$A_AXL_API"
export AGENT_A_A2A_PORT="$A_A2A_PORT"
python3 a2a_agent.py &
A2A_A_PID=$!
ALL_PIDS+=($A2A_A_PID)
echo "       PID: $A2A_A_PID"
wait_for "http://127.0.0.1:$A_A2A_PORT/health" "Agent A A2A server"

# [A6] Register Agent A A2A service
echo "  [A6] Registering A2A service '$A_SERVICE_NAME'..."
register_a2a "$A_AXL_API" "$A_SERVICE_NAME" "$A_A2A_PORT"

echo ""
echo "  ✓ Agent A fully started"
echo ""

# ════════════════════════════════════════════════════════════════════════════
echo "── AGENT B ─────────────────────────────────────────────────"
echo "   AXL: $B_AXL_API  |  MCP: $B_MCP_PORT  |  Router: $B_ROUTER_PORT  |  A2A: none"
echo ""

# [B1] AXL Node B
echo "  [B1] Starting AXL Node B..."
cd "$AXL_DIR"
./node -config "$B_AXL_CONFIG" &
AXL_B_PID=$!
ALL_PIDS+=($AXL_B_PID)
echo "       PID: $AXL_B_PID"
wait_for "$B_AXL_API/topology" "AXL Node B"

# [B2] Agent B MCP server
echo "  [B2] Starting Agent B MCP server (port $B_MCP_PORT)..."
cd "$AGENT_B_DIR"
python3 server.py &
MCP_B_PID=$!
ALL_PIDS+=($MCP_B_PID)
echo "       PID: $MCP_B_PID"
wait_for "http://127.0.0.1:$B_MCP_PORT/health" "Agent B MCP server"

# [B3] MCP Router B
echo "  [B3] Starting MCP Router B (port $B_ROUTER_PORT)..."
cd "$INTEGRATIONS_DIR"
python3 -m mcp_routing.mcp_router --port $B_ROUTER_PORT &
ROUTER_B_PID=$!
ALL_PIDS+=($ROUTER_B_PID)
echo "       PID: $ROUTER_B_PID"
wait_for "http://127.0.0.1:$B_ROUTER_PORT/health" "MCP Router B"

# [B4] Register Agent B MCP service
echo "  [B4] Registering MCP service '$B_SERVICE_NAME'..."
register_mcp "$B_ROUTER_PORT" "$B_SERVICE_NAME" "http://127.0.0.1:$B_MCP_PORT/mcp"

# Note: Agent B has no a2a_port in node-config-2.json so no A2A server here.
# Add a2a_port to node-config-2.json and drop a2a_agent.py there to enable it.

echo ""
echo "  ✓ Agent B fully started (MCP only)"
echo ""

# ════════════════════════════════════════════════════════════════════════════
echo "── AGENT C ─────────────────────────────────────────────────"
echo "   AXL: $C_AXL_API  |  MCP: $C_MCP_PORT  |  Router: $C_ROUTER_PORT  |  A2A: $C_A2A_PORT"
echo ""

# [C1] AXL Node C
echo "  [C1] Starting AXL Node C..."
cd "$AXL_DIR"
./node -config "$C_AXL_CONFIG" &
AXL_C_PID=$!
ALL_PIDS+=($AXL_C_PID)
echo "       PID: $AXL_C_PID"
wait_for "$C_AXL_API/topology" "AXL Node C"

# [C2] Agent C MCP server
echo "  [C2] Starting Agent C MCP server (port $C_MCP_PORT)..."
cd "$AGENT_C_DIR"
python3 server.py &
MCP_C_PID=$!
ALL_PIDS+=($MCP_C_PID)
echo "       PID: $MCP_C_PID"
wait_for "http://127.0.0.1:$C_MCP_PORT/health" "Agent C MCP server"

# [C3] MCP Router C
echo "  [C3] Starting MCP Router C (port $C_ROUTER_PORT)..."
cd "$INTEGRATIONS_DIR"
python3 -m mcp_routing.mcp_router --port $C_ROUTER_PORT &
ROUTER_C_PID=$!
ALL_PIDS+=($ROUTER_C_PID)
echo "       PID: $ROUTER_C_PID"
wait_for "http://127.0.0.1:$C_ROUTER_PORT/health" "MCP Router C"

# [C4] Register Agent C MCP service
echo "  [C4] Registering MCP service '$C_SERVICE_NAME'..."
register_mcp "$C_ROUTER_PORT" "$C_SERVICE_NAME" "http://127.0.0.1:$C_MCP_PORT/mcp"

# [C5] Agent C A2A server
echo "  [C5] Starting Agent C A2A server (port $C_A2A_PORT)..."
cd "$AGENT_C_DIR"
export AGENT_C_AXL_API="$C_AXL_API"
export AGENT_C_A2A_PORT="$C_A2A_PORT"
python3 a2a_agent.py &
A2A_C_PID=$!
ALL_PIDS+=($A2A_C_PID)
echo "       PID: $A2A_C_PID"
wait_for "http://127.0.0.1:$C_A2A_PORT/health" "Agent C A2A server"

# [C6] Register Agent C A2A service
echo "  [C6] Registering A2A service '$C_SERVICE_NAME'..."
register_a2a "$C_AXL_API" "$C_SERVICE_NAME" "$C_A2A_PORT"

echo ""
echo "  ✓ Agent C fully started"
echo ""

# ════════════════════════════════════════════════════════════════════════════
# Final status summary
# ════════════════════════════════════════════════════════════════════════════
echo "════════════════════════════════════════════════════════════"
echo "  ✓ AgentMesh is fully running!"
echo "════════════════════════════════════════════════════════════"
echo ""
echo "  AGENT A"
echo "    AXL node:    $A_AXL_API"
echo "    MCP server:  http://127.0.0.1:$A_MCP_PORT/mcp"
echo "    MCP router:  http://127.0.0.1:$A_ROUTER_PORT"
echo "    A2A server:  http://127.0.0.1:$A_A2A_PORT"
echo "    Agent card:  http://127.0.0.1:$A_A2A_PORT/.well-known/agent.json"
echo ""
echo "  AGENT B  (MCP only)"
echo "    AXL node:    $B_AXL_API"
echo "    MCP server:  http://127.0.0.1:$B_MCP_PORT/mcp"
echo "    MCP router:  http://127.0.0.1:$B_ROUTER_PORT"
echo ""
echo "  AGENT C"
echo "    AXL node:    $C_AXL_API"
echo "    MCP server:  http://127.0.0.1:$C_MCP_PORT/mcp"
echo "    MCP router:  http://127.0.0.1:$C_ROUTER_PORT"
echo "    A2A server:  http://127.0.0.1:$C_A2A_PORT"
echo "    Agent card:  http://127.0.0.1:$C_A2A_PORT/.well-known/agent.json"
echo ""
echo "  ALL PIDS: ${ALL_PIDS[*]}"
echo ""
echo "  Re-discover peers after startup:"
echo "    curl -X POST http://127.0.0.1:$A_A2A_PORT/registry/refresh"
echo "    curl -X POST http://127.0.0.1:$C_A2A_PORT/registry/refresh"
echo ""
echo "  Press Ctrl+C to stop everything."
echo ""

# ── Keep script alive — all agents run as background children ────────────────
wait