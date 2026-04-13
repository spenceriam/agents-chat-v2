#!/usr/bin/env python3
"""Generate UUID tokens for agents"""
import uuid
import json

AGENTS = [
    ("Data", "#4A90E2"),
    ("Vega", "#E74C3C"),
    ("Hermes", "#27AE60"),
    ("TARS", "#F39C12"),
    ("Mandy", "#9B59B6"),
    ("Spencer", "#1ABC9C"),
]

print("Generated tokens for agents:\n")
agents_dict = {}

for name, color in AGENTS:
    token = str(uuid.uuid4())
    agents_dict[token] = {"name": name, "color": color}
    print(f"{name:10} -> {token}")

print("\n--- agents.json ---")
print(json.dumps(agents_dict, indent=2))

# Save to file
with open("agents.json", "w") as f:
    json.dump(agents_dict, f, indent=2)

print("\n✓ Saved to agents.json")
