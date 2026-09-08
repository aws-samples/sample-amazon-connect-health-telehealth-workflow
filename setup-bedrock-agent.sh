#!/bin/bash
set -e

REGION="us-east-1"
ACCOUNT="123456789012"
LAMBDA_ARN="arn:aws:lambda:us-east-1:123456789012:function:connect-health-fhir-query"
S3_BUCKET="connect-health-demo-123456789012"
SCHEMA_KEY="bedrock/connect-health-action-group-schema.json"
AGENT_NAME="connect-health-care-intelligence"
ACTION_GROUP_NAME="fhir-query-tools"

echo "========================================="
echo "Connect Health — Bedrock Agent Setup"
echo "========================================="

# ── Step 1: Upload OpenAPI schema to S3 ──────────────────────────────────────
echo ""
echo "Step 1: Uploading OpenAPI schema to S3..."
aws s3 cp /tmp/connect-health-action-group-schema.json \
  s3://$S3_BUCKET/$SCHEMA_KEY \
  --region $REGION
echo "✅ Schema uploaded: s3://$S3_BUCKET/$SCHEMA_KEY"

# ── Step 2: Create Bedrock Agent IAM Role ────────────────────────────────────
echo ""
echo "Step 2: Creating Bedrock Agent IAM role..."

TRUST_POLICY='{
  "Version": "2012-10-17",
  "Statement": [{
    "Effect": "Allow",
    "Principal": { "Service": "bedrock.amazonaws.com" },
    "Action": "sts:AssumeRole",
    "Condition": {
      "StringEquals": {
        "aws:SourceAccount": "'$ACCOUNT'"
      }
    }
  }]
}'

# Create role (ignore if already exists)
aws iam create-role \
  --role-name ConnectHealthBedrockAgentRole \
  --assume-role-policy-document "$TRUST_POLICY" \
  --region $REGION 2>&1 | grep -v "EntityAlreadyExists" || true

# Attach Bedrock model invocation policy
aws iam put-role-policy \
  --role-name ConnectHealthBedrockAgentRole \
  --policy-name BedrockAgentPolicy \
  --policy-document '{
    "Version": "2012-10-17",
    "Statement": [
      {
        "Effect": "Allow",
        "Action": ["bedrock:InvokeModel"],
        "Resource": "arn:aws:bedrock:us-east-1::foundation-model/*"
      },
      {
        "Effect": "Allow",
        "Action": ["lambda:InvokeFunction"],
        "Resource": "'$LAMBDA_ARN'"
      },
      {
        "Effect": "Allow",
        "Action": ["s3:GetObject"],
        "Resource": "arn:aws:s3:::'$S3_BUCKET'/bedrock/*"
      }
    ]
  }'

AGENT_ROLE_ARN="arn:aws:iam::${ACCOUNT}:role/ConnectHealthBedrockAgentRole"
echo "✅ IAM role ready: $AGENT_ROLE_ARN"

# ── Step 3: Create Bedrock Agent ─────────────────────────────────────────────
echo ""
echo "Step 3: Creating Bedrock Agent..."

AGENT_INSTRUCTION="You are a clinical care intelligence assistant for Dana Cole, a care manager responsible for a panel of patients at a healthcare organization. You help care managers quickly understand their patient population by answering questions in plain clinical language.

You have access to live patient data from HealthLake. When a care manager asks a question, use the appropriate tool to retrieve the data and respond clearly.

Guidelines:
- Always present patient data as a clean, structured table when there are multiple results
- Include patient name, relevant clinical details, and any dates in your response
- Be concise and clinically precise — care managers are busy professionals
- If a query returns no results, say so clearly and suggest a broader search
- Never fabricate patient data — only report what the tools return
- For patient summaries, include conditions, recent appointments, and any care gaps

You support the following types of queries:
1. Completed appointments — who was seen this week or in a given period
2. Patients needing follow-up — booked or pending appointments
3. No-show patients — patients who missed appointments and may need outreach
4. Diabetic patients with overdue A1C — chronic disease management gaps
5. Individual patient summaries — by patient name"

CREATE_RESPONSE=$(aws bedrock-agent create-agent \
  --agent-name "$AGENT_NAME" \
  --agent-resource-role-arn "$AGENT_ROLE_ARN" \
  --foundation-model "anthropic.claude-3-5-sonnet-20241022-v2:0" \
  --instruction "$AGENT_INSTRUCTION" \
  --description "Care Intelligence Agent for population health queries against HealthLake" \
  --idle-session-ttl-in-seconds 1800 \
  --region $REGION)

AGENT_ID=$(echo $CREATE_RESPONSE | python3 -c "import sys,json; print(json.load(sys.stdin)['agent']['agentId'])")
echo "✅ Agent created: $AGENT_ID"

# ── Step 4: Add Lambda permission for Bedrock ────────────────────────────────
echo ""
echo "Step 4: Adding Lambda resource policy for Bedrock..."
aws lambda add-permission \
  --function-name connect-health-fhir-query \
  --statement-id BedrockAgentInvoke \
  --action lambda:InvokeFunction \
  --principal bedrock.amazonaws.com \
  --source-account $ACCOUNT \
  --region $REGION 2>&1 | grep -v "ResourceConflictException" || true
echo "✅ Lambda permission added"

# ── Step 5: Create Action Group ──────────────────────────────────────────────
echo ""
echo "Step 5: Creating Action Group (wiring Lambda + schema)..."

# Brief pause for agent to be ready
sleep 5

aws bedrock-agent create-agent-action-group \
  --agent-id "$AGENT_ID" \
  --agent-version "DRAFT" \
  --action-group-name "$ACTION_GROUP_NAME" \
  --description "FHIR query tools for HealthLake — 5 clinical intelligence operations" \
  --action-group-executor '{"lambda": "'$LAMBDA_ARN'"}' \
  --api-schema '{"s3": {"s3BucketName": "'$S3_BUCKET'", "s3ObjectKey": "'$SCHEMA_KEY'"}}' \
  --action-group-state "ENABLED" \
  --region $REGION

echo "✅ Action Group created"

# ── Step 6: Prepare Agent (required before testing) ──────────────────────────
echo ""
echo "Step 6: Preparing agent (building internal model)..."
aws bedrock-agent prepare-agent \
  --agent-id "$AGENT_ID" \
  --region $REGION

echo ""
echo "========================================="
echo "✅ ALL DONE"
echo "========================================="
echo "Agent ID:    $AGENT_ID"
echo "Agent Name:  $AGENT_NAME"
echo "Lambda:      $LAMBDA_ARN"
echo "Schema:      s3://$S3_BUCKET/$SCHEMA_KEY"
echo ""
echo "Next: wait ~30s for preparation, then run the test script."
echo "Agent ID saved for test: $AGENT_ID"

# Save agent ID for test script
echo $AGENT_ID > /tmp/connect-health-agent-id.txt
