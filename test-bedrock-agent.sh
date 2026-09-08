#!/bin/bash
REGION="us-east-1"
AGENT_ID=$(cat /tmp/connect-health-agent-id.txt 2>/dev/null || echo "EXAMPLE123")
AGENT_ALIAS_ID="TSTALIASID"

echo "========================================="
echo "Testing Connect Health Care Intelligence Agent"
echo "Agent ID: $AGENT_ID"
echo "========================================="

run_query() {
  local label=$1
  local query=$2
  echo ""
  echo "─── $label ───"
  echo "Query: \"$query\""
  
  SESSION_ID="test-session-$(date +%s)-$RANDOM"
  
  # Use invoke-inline-agent (new CLI) with streaming output to file
  TMPOUT=$(mktemp)
  
  aws bedrock-agent-runtime invoke-inline-agent \
    --region $REGION \
    --session-id "$SESSION_ID" \
    --input-text "$query" \
    --foundation-model "anthropic.claude-3-5-sonnet-20241022-v2:0" \
    --instruction "You are a clinical care intelligence assistant for Dana Cole, a care manager. Answer questions about patient population data using the provided tools. Present results as clean tables." \
    --action-groups '[{
      "actionGroupName": "fhir-query-tools",
      "actionGroupExecutor": {"lambda": "arn:aws:lambda:us-east-1:123456789012:function:connect-health-fhir-query"},
      "apiSchema": {"s3": {"s3BucketName": "connect-health-demo-123456789012", "s3ObjectKey": "bedrock/connect-health-action-group-schema.json"}}
    }]' \
    --output json > $TMPOUT 2>&1
  
  EXIT_CODE=$?
  
  if [ $EXIT_CODE -eq 0 ]; then
    # Extract text from streaming response
    python3 -c "
import json, sys
with open('$TMPOUT') as f:
    content = f.read()
try:
    data = json.loads(content)
    # Try completion array
    for event in data.get('completion', []):
        if 'chunk' in event:
            b = event['chunk'].get('bytes', '')
            if isinstance(b, str):
                print(b)
        elif 'returnControl' in event:
            print('[Agent is calling Lambda...]')
except:
    print(content[:500])
" 2>/dev/null || cat $TMPOUT | head -20
  else
    cat $TMPOUT | head -30
  fi
  rm -f $TMPOUT
}

run_query "Test 1: Completed Appointments" "Show me patients who completed appointments this week"
run_query "Test 2: Needs Follow-up" "Which patients need follow-up care?"
run_query "Test 3: No Shows" "List patients who missed appointments in the last 30 days"
run_query "Test 4: Diabetic A1C" "Show me diabetic patients with overdue A1C tests"
run_query "Test 5: Patient Summary" "Give me a summary for Jordan Rivera"

echo ""
echo "========================================="
echo "✅ All 5 test queries complete"
echo "========================================="
