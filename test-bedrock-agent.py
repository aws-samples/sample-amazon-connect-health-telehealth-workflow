#!/usr/bin/env python3
"""
Connect Health — Bedrock Agent Test
Tests all 5 clinical intelligence queries against the care manager agent.
"""
import boto3
import json
import uuid
import time

REGION = "us-east-1"
AGENT_ID = "EXAMPLE123"
AGENT_ALIAS_ID = "TSTALIASID"  # Built-in draft alias for testing

client = boto3.client("bedrock-agent-runtime", region_name=REGION)

QUERIES = [
    ("Completed Appointments", "Show me patients who completed appointments this week"),
    ("Needs Follow-up",        "Which patients need follow-up care?"),
    ("No Shows",               "List patients who missed appointments in the last 30 days"),
    ("Diabetic A1C",           "Show me diabetic patients with overdue A1C tests"),
    ("Patient Summary",        "Give me a summary for Jordan Rivera"),
]

def run_query(label, query_text):
    print(f"\n{'─'*50}")
    print(f"  {label}")
    print(f"  Query: \"{query_text}\"")
    print(f"{'─'*50}")

    session_id = f"test-{uuid.uuid4().hex[:8]}"

    try:
        response = client.invoke_agent(
            agentId=AGENT_ID,
            agentAliasId=AGENT_ALIAS_ID,
            sessionId=session_id,
            inputText=query_text,
        )

        # Consume the streaming EventStream
        full_response = ""
        for event in response["completion"]:
            if "chunk" in event:
                chunk_bytes = event["chunk"].get("bytes", b"")
                if isinstance(chunk_bytes, bytes):
                    full_response += chunk_bytes.decode("utf-8")
                else:
                    full_response += str(chunk_bytes)
            elif "trace" in event:
                trace = event["trace"].get("trace", {})
                if "orchestrationTrace" in trace:
                    orch = trace["orchestrationTrace"]
                    if "invocationInput" in orch:
                        inv = orch["invocationInput"]
                        if "actionGroupInvocationInput" in inv:
                            ag = inv["actionGroupInvocationInput"]
                            print(f"  [→ Calling Lambda: {ag.get('function', ag.get('apiPath', '?'))}]")

        print(full_response if full_response else "[No response text returned]")

    except client.exceptions.ResourceNotFoundException as e:
        print(f"  ❌ Agent not found or not prepared yet: {e}")
    except Exception as e:
        print(f"  ❌ Error: {type(e).__name__}: {e}")

def main():
    print("=" * 50)
    print("  Connect Health Care Intelligence Agent Test")
    print(f"  Agent ID: {AGENT_ID}")
    print("=" * 50)

    # Check agent status first
    mgmt = boto3.client("bedrock-agent", region_name=REGION)
    agent = mgmt.get_agent(agentId=AGENT_ID)
    status = agent["agent"]["agentStatus"]
    print(f"\n  Agent status: {status}")

    if status not in ("PREPARED", "VERSIONED"):
        print(f"  ⚠️  Agent is {status} — may need a moment. Waiting 10s...")
        time.sleep(10)

    for label, query in QUERIES:
        run_query(label, query)
        time.sleep(2)  # brief pause between queries

    print("\n" + "=" * 50)
    print("  ✅ All 5 queries complete")
    print("=" * 50)

if __name__ == "__main__":
    main()
