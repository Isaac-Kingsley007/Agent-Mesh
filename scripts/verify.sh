#!/usr/bin/env bash
# ============================================================================
# verify.sh — End-to-end verification for Phase 2 + Phase 3 (x402 payments)
#
# Checks: Flask server, MCP router, cross-mesh tool discovery + tool calls,
#         x402 payment gate, and autonomous agent-to-agent paid calls
# Usage: bash scripts/verify.sh
# ============================================================================
set -euo pipefail

NODE_A_API="http://127.0.0.1:9002"
NODE_B_API="http://127.0.0.1:9012"
AGENT_B="http://127.0.0.1:7100"
ROUTER="http://127.0.0.1:9003"
SERVICE="agentmesh"

PASS=0
FAIL=0

check() {
    local label="$1"
    local result="$2"
    if [ $? -eq 0 ] && [ -n "$result" ]; then
        echo "  ✓ $label"
        PASS=$((PASS + 1))
    else
        echo "  ✗ $label"
        FAIL=$((FAIL + 1))
    fi
}

echo "============================================"
echo "  AgentMesh — Phase 2 Verification"
echo "============================================"
echo ""

# ---------------------------------------------------------------------------
# Step 1: Agent B health
# ---------------------------------------------------------------------------
echo "[1] Agent B Flask server..."
RESP=$(curl -s $AGENT_B/health 2>/dev/null || echo "")
check "Health check" "$RESP"
echo "     $RESP"

# ---------------------------------------------------------------------------
# Step 2: Direct MCP — tools/list
# ---------------------------------------------------------------------------
echo ""
echo "[2] Direct MCP tools/list to Agent B..."
RESP=$(curl -s -X POST $AGENT_B/mcp \
    -H "Content-Type: application/json" \
    -d '{"jsonrpc":"2.0","method":"tools/list","id":1,"params":{}}' 2>/dev/null || echo "")
check "tools/list" "$RESP"
echo "     $RESP" | python3 -m json.tool 2>/dev/null || echo "     $RESP"

# ---------------------------------------------------------------------------
# Step 3: Direct MCP — tools/call summarize
# ---------------------------------------------------------------------------
echo ""
echo "[3] Direct MCP tools/call (summarize)..."
RESP=$(curl -s -X POST $AGENT_B/mcp \
    -H "Content-Type: application/json" \
    -d '{"jsonrpc":"2.0","method":"tools/call","id":2,"params":{"name":"summarize","arguments":{"text":"The quick brown fox jumps over the lazy dog. This is a classic pangram used in typing tests. It contains every letter of the English alphabet at least once. Many people use it to test keyboards and fonts."}}}' 2>/dev/null || echo "")
check "summarize call" "$RESP"
echo "     $RESP" | python3 -m json.tool 2>/dev/null || echo "     $RESP"

# ---------------------------------------------------------------------------
# Step 4: Direct MCP — tools/call sentiment
# ---------------------------------------------------------------------------
echo ""
echo "[4] Direct MCP tools/call (sentiment)..."
RESP=$(curl -s -X POST $AGENT_B/mcp \
    -H "Content-Type: application/json" \
    -d '{"jsonrpc":"2.0","method":"tools/call","id":3,"params":{"name":"sentiment","arguments":{"text":"This product is amazing and wonderful! I absolutely love it. Best purchase ever."}}}' 2>/dev/null || echo "")
check "sentiment call" "$RESP"
echo "     $RESP" | python3 -m json.tool 2>/dev/null || echo "     $RESP"

# ---------------------------------------------------------------------------
# Step 5: MCP Router health
# ---------------------------------------------------------------------------
echo ""
echo "[5] MCP Router health..."
RESP=$(curl -s $ROUTER/health 2>/dev/null || echo "")
check "Router health" "$RESP"
echo "     $RESP"

# ---------------------------------------------------------------------------
# Step 6: Registered services
# ---------------------------------------------------------------------------
echo ""
echo "[6] Registered services..."
RESP=$(curl -s $ROUTER/services 2>/dev/null || echo "")
check "Services list" "$RESP"
echo "     $RESP" | python3 -m json.tool 2>/dev/null || echo "     $RESP"

# ---------------------------------------------------------------------------
# Step 7: Node A topology (check if running)
# ---------------------------------------------------------------------------
echo ""
echo "[7] Node A topology..."
RESP=$(curl -s $NODE_A_API/topology 2>/dev/null || echo "")
if [ -n "$RESP" ]; then
    check "Node A up" "$RESP"
    NODE_B_KEY=$(echo "$RESP" | python3 -c "
import sys, json
data = json.load(sys.stdin)
peers = data.get('peers', {})
for key in peers:
    if key != data.get('our_public_key', ''):
        print(key)
        break
" 2>/dev/null || echo "")
    echo "     Our key: $(echo "$RESP" | python3 -c "import sys,json; print(json.load(sys.stdin).get('our_public_key','?'))" 2>/dev/null)"
    if [ -n "$NODE_B_KEY" ]; then
        echo "     Node B key: $NODE_B_KEY"
    else
        # Fallback: try getting Node B key from its own API
        NODE_B_KEY=$(curl -s $NODE_B_API/topology 2>/dev/null | python3 -c "import sys,json; print(json.load(sys.stdin).get('our_public_key',''))" 2>/dev/null || echo "")
        if [ -n "$NODE_B_KEY" ]; then
            echo "     Node B key (from B): $NODE_B_KEY"
        else
            echo "     ⚠ Could not determine Node B key"
        fi
    fi
else
    echo "  ✗ Node A not running — skipping mesh tests"
    FAIL=$((FAIL + 1))
    NODE_B_KEY=""
fi

# ---------------------------------------------------------------------------
# Step 8: Cross-mesh tools/list (Node A → Node B)
# ---------------------------------------------------------------------------
echo ""
echo "[8] Cross-mesh tools/list (Node A → Node B)..."
if [ -n "$NODE_B_KEY" ]; then
    RESP=$(curl -s -X POST "$NODE_A_API/mcp/$NODE_B_KEY/$SERVICE" \
        -H "Content-Type: application/json" \
        -d '{"jsonrpc":"2.0","method":"tools/list","id":1,"params":{}}' 2>/dev/null || echo "")
    check "Mesh tools/list" "$RESP"
    echo "     $RESP" | python3 -m json.tool 2>/dev/null || echo "     $RESP"
else
    echo "  ⊘ Skipped (no Node B key)"
fi

# ---------------------------------------------------------------------------
# Step 9: Cross-mesh tools/call (Node A → Node B → summarize)
# ---------------------------------------------------------------------------
echo ""
echo "[9] Cross-mesh tools/call summarize (Node A → Node B)..."
if [ -n "$NODE_B_KEY" ]; then
    RESP=$(curl -s -X POST "$NODE_A_API/mcp/$NODE_B_KEY/$SERVICE" \
        -H "Content-Type: application/json" \
        -d '{"jsonrpc":"2.0","method":"tools/call","id":2,"params":{"name":"summarize","arguments":{"text":"Artificial intelligence is transforming every industry. Machine learning models can now process natural language, generate images, and even write code. The implications for productivity and creativity are enormous."}}}' 2>/dev/null || echo "")
    check "Mesh summarize" "$RESP"
    echo "     $RESP" | python3 -m json.tool 2>/dev/null || echo "     $RESP"
else
    echo "  ⊘ Skipped (no Node B key)"
fi

# ---------------------------------------------------------------------------
# Step 10: Cross-mesh tools/call (Node A → Node B → sentiment)
# ---------------------------------------------------------------------------
echo ""
echo "[10] Cross-mesh tools/call sentiment (Node A → Node B)..."
if [ -n "$NODE_B_KEY" ]; then
    RESP=$(curl -s -X POST "$NODE_A_API/mcp/$NODE_B_KEY/$SERVICE" \
        -H "Content-Type: application/json" \
        -d '{"jsonrpc":"2.0","method":"tools/call","id":3,"params":{"name":"sentiment","arguments":{"text":"This is terrible and awful. I hate the bugs and crashes. Worst experience ever."}}}' 2>/dev/null || echo "")
    check "Mesh sentiment" "$RESP"
    echo "     $RESP" | python3 -m json.tool 2>/dev/null || echo "     $RESP"
else
    echo "  ⊘ Skipped (no Node B key)"
fi

# ===========================================================================
# Phase 3 — x402 Payment Gate
# ===========================================================================
echo ""
echo "============================================"
echo "  Phase 3 — x402 Payment Gate"
echo "============================================"
echo ""

# ---------------------------------------------------------------------------
# Step 11: POST /mcp without payment should return HTTP 402
# ---------------------------------------------------------------------------
echo "[11] POST /mcp without payment should return 402..."
RESP=$(curl -s -o /dev/null -w "%{http_code}" -X POST $AGENT_B/mcp \
    -H "Content-Type: application/json" \
    -d '{"jsonrpc":"2.0","method":"tools/list","id":1,"params":{}}' 2>/dev/null || echo "000")
if [ "$RESP" = "402" ]; then
    echo "  ✓ Correctly returns 402 Payment Required"
    PASS=$((PASS + 1))
else
    echo "  ✗ Expected 402 but got $RESP (is AGENT_B_WALLET_ADDRESS set?)"
    FAIL=$((FAIL + 1))
fi

# ---------------------------------------------------------------------------
# Step 12: GET /health should still return 200 (unprotected)
# ---------------------------------------------------------------------------
echo ""
echo "[12] GET /health should still return 200 (unprotected)..."
RESP=$(curl -s -o /dev/null -w "%{http_code}" $AGENT_B/health 2>/dev/null || echo "000")
if [ "$RESP" = "200" ]; then
    echo "  ✓ /health is unprotected (200)"
    PASS=$((PASS + 1))
else
    echo "  ✗ /health returned $RESP — should be 200"
    FAIL=$((FAIL + 1))
fi

# ---------------------------------------------------------------------------
# Step 13: Agent A paid call (requires AGENT_A_EVM_PRIVATE_KEY)
# ---------------------------------------------------------------------------
echo ""
echo "[13] Agent A paid call (requires AGENT_A_EVM_PRIVATE_KEY)..."
if [ -z "${AGENT_A_EVM_PRIVATE_KEY:-}" ]; then
    echo "  ⊘ Skipped (AGENT_A_EVM_PRIVATE_KEY not set)"
else
    PROJECT_ROOT="$(cd "$(dirname "$0")/.." && pwd)"
    AGENT_A_RESULT=$(python3 "$PROJECT_ROOT/agents/agent-a/agent.py" 2>&1 | tail -5)
    if echo "$AGENT_A_RESULT" | grep -q "completed all paid calls"; then
        echo "  ✓ Agent A completed paid calls successfully"
        PASS=$((PASS + 1))
    else
        echo "  ✗ Agent A failed"
        echo "     $AGENT_A_RESULT"
        FAIL=$((FAIL + 1))
    fi
fi

# ---------------------------------------------------------------------------
# Summary
# ---------------------------------------------------------------------------
echo ""
echo "============================================"
echo "  Results: $PASS passed, $FAIL failed"
echo "============================================"

echo ""
echo "============================================"
echo "  Final Results: $PASS passed, $FAIL failed"
echo "============================================"

if [ $FAIL -gt 0 ]; then
    exit 1
fi

