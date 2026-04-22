#!/usr/bin/env python3
"""Stress test ACV2 for memory leaks. Sends many requests and checks RSS growth."""

import subprocess
import time
import json
import sys

URL = "http://localhost:8081"
TOKEN = "664500b7-81ab-4a20-9d72-9c1e97ea1e35"  # Hermes
ITERATIONS = 500
BATCH = 50

def get_memory():
    """Get uvicorn process RSS in MB."""
    try:
        result = subprocess.run(
            ["ps", "-o", "rss=", "-p", "1743092"],
            capture_output=True, text=True, timeout=5
        )
        if result.returncode == 0:
            return int(result.stdout.strip()) / 1024  # KB to MB
    except:
        pass
    return 0

def make_request(method, path, json_data=None):
    """Make HTTP request."""
    import urllib.request
    url = f"{URL}{path}"
    data = json.dumps(json_data).encode() if json_data else None
    req = urllib.request.Request(url, data=data, method=method)
    if data:
        req.add_header("Content-Type", "application/json")
    try:
        resp = urllib.request.urlopen(req, timeout=5)
        return resp.status
    except Exception as e:
        return str(e)

def main():
    print("=== ACV2 Memory Leak Stress Test ===")
    print(f"Target: {URL}")
    print(f"Iterations: {ITERATIONS}")
    
    # Baseline
    baseline_mem = get_memory()
    print(f"\nBaseline memory: {baseline_mem:.1f} MB")
    
    checkpoints = []
    
    for batch_num in range(0, ITERATIONS, BATCH):
        batch_end = min(batch_num + BATCH, ITERATIONS)
        for i in range(batch_num, batch_end):
            # Send channel message
            status = make_request("POST", f"/api/v2/channels/general/messages?token={TOKEN}", {
                "content": f"Stress test message #{i}",
                "structured": {"test": True}
            })
            
            # Get messages
            status = make_request("GET", f"/api/v2/channels/general/messages?token={TOKEN}&limit=10")
            
            # Send DM
            status = make_request("POST", f"/api/v2/dms/Data/messages?token={TOKEN}", {
                "content": f"DM #{i}",
            })
            
            # Health check
            status = make_request("GET", "/api/v2/health")
        
        current_mem = get_memory()
        growth = current_mem - baseline_mem
        checkpoints.append((batch_end, current_mem, growth))
        print(f"  After {batch_end} requests: {current_mem:.1f} MB (growth: {growth:+.1f} MB)")
    
    # Check for leak
    final_mem = get_memory()
    total_growth = final_mem - baseline_mem
    per_request = (total_growth * 1024) / ITERATIONS  # KB per request
    
    print(f"\n=== RESULTS ===")
    print(f"Baseline: {baseline_mem:.1f} MB")
    print(f"Final:    {final_mem:.1f} MB")
    print(f"Growth:   {total_growth:+.1f} MB over {ITERATIONS} requests")
    print(f"Per req:  {per_request:+.2f} KB")
    
    if total_growth > 50:  # More than 50MB growth is suspicious
        print("\n⚠️  WARNING: Possible memory leak detected!")
        print("Memory grew significantly. Check for unbounded data structures.")
        return 1
    else:
        print(f"\n✅ OK: Memory growth within acceptable limits (< 50MB)")
        return 0

if __name__ == "__main__":
    sys.exit(main())
