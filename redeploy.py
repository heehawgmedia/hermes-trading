#!/usr/bin/env python3
"""Trigger a Railway redeploy via API. Run after git push."""
import os, sys, urllib.request, json

TOKEN = os.environ.get("RAILWAY_TOKEN", "c81f57b6-76a8-43f6-9822-b1b023ce2ca3")
SERVICE_ID = "8e982bdc-381c-4bbc-a705-a269c624410b"
ENV_ID = "2fe1147d-8f11-4289-96d7-d6251f6d06f9"

query = f'mutation {{ serviceInstanceRedeploy(environmentId: "{ENV_ID}", serviceId: "{SERVICE_ID}") }}'
payload = json.dumps({"query": query}).encode()
req = urllib.request.Request(
    "https://backboard.railway.com/graphql/v2",
    data=payload,
    headers={"Authorization": f"Bearer {TOKEN}", "Content-Type": "application/json"},
)
with urllib.request.urlopen(req, timeout=15) as resp:
    result = json.loads(resp.read())

if result.get("data", {}).get("serviceInstanceRedeploy"):
    print("Railway redeploy triggered.")
    sys.exit(0)
else:
    print(f"Redeploy failed: {result}", file=sys.stderr)
    sys.exit(1)
