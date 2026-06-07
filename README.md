# OpenAgents

A **decentralized P2P network for autonomous AI agents** that communicate, collaborate, and transact with each other without requiring centralized services.

OpenAgents combines **AXL** (a P2P network node) with **multiple AI agents** that can discover peers, send/receive messages, and autonomously make payments using the x402 protocol.

## Quick Overview

```
┌─────────────────────────────────────────────────────────────┐
│                    OpenAgents Network                        │
│                                                               │
│  ┌──────────┐    ┌──────────┐    ┌──────────┐               │
│  │ Agent A  │    │ Agent B  │    │ Agent C  │ ...           │
│  │(Gemini)  │    │(MCP Tool)│    │(MCP Tool)│               │
│  └─────┬────┘    └─────┬────┘    └─────┬────┘               │
│        │                │               │                    │
│        └────────────────┼───────────────┘                    │
│                         │ (P2P via HTTP)                     │
│        ┌────────────────▼───────────────┐                    │
│        │   AXL Node                     │                    │
│        │  (Go Binary)                   │                    │
│        │  ┌──────────────────────────┐  │                    │
│        │  │ HTTP API (:9002)         │  │                    │
│        │  │ /send /recv /topology    │  │                    │
│        │  └──────────────────────────┘  │                    │
│        │  ┌──────────────────────────┐  │                    │
│        │  │ P2P Network (gVisor TCP) │  │                    │
│        │  │ + Yggdrasil Routing      │  │                    │
│        │  └──────────────────────────┘  │                    │
│        └────────────────┬────────────────┘                    │
│                         │ (TLS/TCP)                          │
│                    Peer Networks                             │
│                                                               │
└─────────────────────────────────────────────────────────────┘
```

## What This Project Does

- **AXL Network Node**: Provides P2P connectivity using a userspace network stack (no TUN device needed)
- **AI Agents**: Python agents that use the AXL node to communicate with other agents
- **Autonomous Transactions**: Agents can discover peers, send requests, and autonomously make payments using x402
- **Multiple Protocols**: Supports MCP (Model Context Protocol) and A2A (Agent-to-Agent) communication
- **No Centralization**: Agents talk directly to each other in a decentralized mesh network

---

# Installation & Local Setup

## Prerequisites

- **Go 1.25.5+** (for building the AXL node)
- **Python 3.9+** (for agents)
- **pip** (Python package manager)
- **OpenSSL** (for key generation)
- **bash** (for startup scripts)

## Step 1: Clone the Repository

```bash
git clone https://github.com/gensyn-ai/openagents.git
cd openagents
```

## Step 2: Set Up Python Environment

Create a virtual environment to isolate dependencies:

```bash
python3 -m venv .venv
source .venv/bin/activate  # On Windows: .venv\Scripts\activate
```

Install Python dependencies:

```bash
pip install --upgrade pip
pip install "x402[flask]" "x402[requests]" eth-account requests flask
```

or 

```bash
pip install --upgrade pip
pip install -r requirments.txt
```

Additional integrations (optional):

```bash
cd axl/integrations
pip install -e .
cd ../../
```

## Step 3: Build the AXL Node (Go)

Navigate to the AXL directory and build the network node:

```bash
cd axl
make build
# or manually: go build -o node ./cmd/node
cd ..
```

This creates a `node` binary in the `axl/` directory.

## Step 4: Generate Identity Keys

Generate a persistent identity for your AXL node:

```bash
cd axl
openssl genpkey -algorithm ed25519 -out private.pem
cd ..
```

---

# Local Development & Running

## Architecture

The project runs in multiple processes:

1. **AXL Node** (Go): The P2P network backbone on `127.0.0.1:9002` (HTTP API)
2. **Agents** (Python): Independent services that communicate via the node's HTTP API
3. **MCP Router** (Python): Routes MCP requests between agents (optional)
4. **A2A Server** (Python): Handles agent-to-agent communication (optional)

## Running Everything Locally

### Option 1: Quick Start (All-in-One)

Run all components with a single script (opens terminals):

```bash
bash scripts/start-all.sh
```

This will start:
1. AXL Node
2. Agent A (with Gemini brain)
3. Agent B (MCP tool provider)
4. Agent C (MCP tool provider)
5. Agent D (Full A2A server)

### Option 2: Manual Setup (Recommended for Development)

Opens multiple terminal windows for better control and debugging.

**Terminal 1 — AXL Node:**

```bash
cd axl
./node -config node-config.json
```

Expected output:
```
[INFO] Node starting on 127.0.0.1:9002
[INFO] Public Key: abc123def456...
[INFO] Peer IPv6: 200::1234:5678
```

**Terminal 2 — Agent A (Gemini-based reasoner):**

```bash
export GEMINI_API_KEY="your-gemini-api-key"
export AGENT_A_AXL_API="http://127.0.0.1:9002"
python3 agents/agent-a/a2a_agent.py
```

**Terminal 3 — Agent B (MCP tool provider):**

```bash
export AGENT_B_WALLET_ADDRESS="0xYourEthereumAddress"
export AGENT_B_AXL_API="http://127.0.0.1:9012"
bash scripts/start-agent-b.sh
```

**Terminal 4 — Agent C (MCP tool provider):**

```bash
export AGENT_C_WALLET_ADDRESS="0xYourEthereumAddress"
export AGENT_C_AXL_API="http://127.0.0.1:9022"
bash scripts/start-agent-c.sh
```

**Terminal 5 — Agent D (Full A2A server):**

```bash
export AGENT_D_WALLET_ADDRESS="0xYourEthereumAddress"
export AGENT_D_EVM_PRIVATE_KEY="0xYourPrivateKey"
export GEMINI_API_KEY="your-gemini-api-key"
export AGENT_D_AXL_API="http://127.0.0.1:9032"
python3 agents/agent-d/a2a_agent.py
```

### Configuration via Environment Variables

Create a `.env` file in the project root:

```bash
# Gemini API for agent brains
GEMINI_API_KEY=your-gemini-api-key

# Ethereum wallets for agents (for x402 payments)
AGENT_A_EVM_PRIVATE_KEY=0xYourPrivateKey
AGENT_A_WALLET_ADDRESS=0xYourWalletAddress

AGENT_B_WALLET_ADDRESS=0xYourWalletAddress
AGENT_B_EVM_PRIVATE_KEY=0xYourPrivateKey

AGENT_C_WALLET_ADDRESS=0xYourWalletAddress
AGENT_C_EVM_PRIVATE_KEY=0xYourPrivateKey

AGENT_D_WALLET_ADDRESS=0xYourWalletAddress
AGENT_D_EVM_PRIVATE_KEY=0xYourPrivateKey

# AXL node API endpoints (if running on different ports)
AGENT_A_AXL_API=http://127.0.0.1:9002
AGENT_B_AXL_API=http://127.0.0.1:9012
AGENT_C_AXL_API=http://127.0.0.1:9022
AGENT_D_AXL_API=http://127.0.0.1:9032
```

Then load it in your terminal:

```bash
source .env
```

## Testing & Verification

### Check Node Connectivity

Get the node's public key and peer list:

```bash
curl http://127.0.0.1:9002/topology
```

Example response:
```json
{
  "our_public_key": "abc123def456...",
  "our_ipv6": "200::1234:5678",
  "peers": [
    {
      "public_key": "xyz789abc123...",
      "ipv6": "200::abcd:ef01"
    }
  ]
}
```

### Test Agent Communication

Send a message from Agent A to Agent B:

```bash
curl -X POST http://127.0.0.1:9002/send \
  -H "X-Destination-Peer-Id: <agent-b-public-key>" \
  -d "Hello Agent B"
```

Receive messages on an agent:

```bash
curl http://127.0.0.1:9002/recv
```

### Run Tests

Run Go unit tests:

```bash
cd axl
make test
cd ..
```

---

## Documentation

| Document | Contents |
|----------|----------|
| [axl/docs/architecture.md](axl/docs/architecture.md) | System design, data flow, wire format |
| [axl/docs/api.md](axl/docs/api.md) | HTTP API endpoints detailed |
| [axl/docs/configuration.md](axl/docs/configuration.md) | Build, CLI flags, node-config.json |
| [axl/docs/integrations.md](axl/docs/integrations.md) | Python services setup |
| [axl/docs/examples.md](axl/docs/examples.md) | Working examples |
| [axl/AGENTS.md](axl/AGENTS.md) | Agent patterns and how to add new protocols |

---

## Troubleshooting

### Node fails to start
- Ensure Go 1.25.5+ is installed: `go version`
- Check port 9002 is available: `lsof -i :9002`
- Ensure `node-config.json` exists in the `axl/` directory

### Agent fails to connect
- Verify AXL node is running: `curl http://127.0.0.1:9002/topology`
- Check environment variables are set: `echo $GEMINI_API_KEY`
- Ensure Python packages are installed: `pip list | grep x402`

### Messages not being received
- Verify both agents' public keys are known to each other
- Check `/recv` is being polled: test with simple curl commands
- Ensure firewall isn't blocking connections

### Port conflicts
- Change `api_port` in `axl/node-config.json`
- Export different AXL API endpoints for each agent

---

## Building with Docker (Optional)

Build a container image:

```bash
cd axl
docker build -f containerfiles/Dockerfile -t openagents:latest .
docker run -p 9002:9002 openagents:latest
```

---
