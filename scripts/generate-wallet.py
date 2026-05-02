#!/usr/bin/env python3
"""Generate a new EVM wallet for Agents and print the .env lines."""
from eth_account import Account
import secrets

private_key = "0x" + secrets.token_hex(32)
acct = Account.from_key(private_key)

print("Add these to your .env file:")
print(f"AGENT_EVM_PRIVATE_KEY={private_key}")
print(f"AGENT_WALLET_ADDRESS={acct.address}")
print()
print(f"Agent address: {acct.address}")
print(f"Fund with test USDC at: https://faucet.circle.com (select Base Sepolia)")
