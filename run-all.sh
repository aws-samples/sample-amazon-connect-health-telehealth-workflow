#!/bin/bash
set -e
REGION="us-east-1"
AGENT_ID="EXAMPLE123"
MODEL="us.anthropic.claude-opus-4-6-v1"

echo "========================================="
echo "Step 1: Updating agent model → $MODEL"
echo "========================================="
aws bedrock-agent update-agent \
  --agent-id $AGENT_ID \
  --agent-name "connect-health-care-intelligence" \
  --agent-resource-role-arn "arn:aws:iam::123456789012:role/ConnectHealthBedrockAgentRole" \
  --foundation-model "$MODEL" \
  --instruction "You are a clinical care intelligence assistant for Dana Cole, a care manager responsible for a panel of patients. Use the available tools to answer questions about patient population data. Present results as clean tables with patient names and relevant clinical details." \
  --region $REGION \
  --query "agent.{status:agentStatus,model:foundationModel}" \
  --output json

echo ""
echo "========================================="
echo "Step 2: Preparing agent..."
echo "========================================="
aws bedrock-agent prepare-agent \
  --agent-id $AGENT_ID \
  --region $REGION

echo "Waiting 20s for preparation..."
sleep 20

echo ""
echo "========================================="
echo "Step 3: Agent status check"
echo "========================================="
aws bedrock-agent get-agent \
  --agent-id $AGENT_ID \
  --region $REGION \
  --query "agent.{status:agentStatus,model:foundationModel}" \
  --output json

echo ""
echo "========================================="
echo "Step 4: Running 5 test queries via Python"
echo "========================================="
python3 - << 'PYEOF'
import boto3, uuid, time

client = boto3.client("bedrock-agent-runtime", region_name="us-east-1")

QUERIES = [
    ("Completed Appointments", "Show me patients who completed appointments this week"),
    ("Needs Follow-up",        "Which patients need follow-up care?"),
    ("No Shows",               "List patients who missed appointments in the last 30 days"),
    ("Diabetic A1C",           "Show me diabetic patients with overdue A1C tests"),
    ("Patient Summary",        "Give me a summary for Jordan Rivera"),
]

print("\n" + "="*52)
print("  Connect Health Care Intelligence Agent")
print("  Model: us.anthropic.claude-opus-4-6-v1")
print("="*52)

for label, query in QUERIES:
    print(f"\n{'─'*52}")
    print(f"  {label}")
    print(f"  → \"{query}\"")
    print(f"{'─'*52}")
    try:
        resp = client.invoke_agent(
            agentId="EXAMPLE123",
            agentAliasId="TSTALIASID",
            sessionId=f"test-{uuid.uuid4().hex[:8]}",
            inputText=query,
            enableTrace=True
        )
        full = ""
        for event in resp["completion"]:
            if "chunk" in event:
                b = event["chunk"].get("bytes", b"")
                full += b.decode("utf-8") if isinstance(b, bytes) else str(b)
            elif "trace" in event:
                t = event["trace"].get("trace", {})
                orch = t.get("orchestrationTrace", {})
                inv = orch.get("invocationInput", {})
                ag = inv.get("actionGroupInvocationInput", {})
                if ag:
                    path = ag.get("apiPath", ag.get("function", "?"))
                    params = ag.get("parameters", [])
                    ps = ", ".join(f"{p['name']}={p['value']}" for p in params) if params else ""
                    print(f"  [→ Lambda: {path}{' ('+ps+')' if ps else ''}]")
        print(full if full else "  [empty response]")
    except Exception as e:
        print(f"  ❌ {type(e).__name__}: {e}")
    time.sleep(2)

print("\n" + "="*52)
print("  ✅ All 5 queries complete")
print("="*52)
PYEOF
