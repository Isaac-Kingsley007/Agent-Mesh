import json

config = {
    "PrivateKeyPath": "private-2.pem",
    "Peers": ["tls://127.0.0.1:9001"],
    "Listen": [],
    "api_port": 9012,
    "tcp_port": 7001,
    "router_addr": "http://127.0.0.1",
    "router_port": 9003
}

with open("/home/isaac/hackathons/openagents/axl/node-config-2.json", "w") as f:
    json.dump(config, f, indent=2)
    f.write("\n")

print("✓ Updated node-config-2.json")
print(json.dumps(config, indent=2))
