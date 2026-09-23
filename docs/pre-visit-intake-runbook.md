# Pre-visit Intake Agent — Setup Runbook (custom agent)

This documents how to build the **custom Pre-visit Intake Agent** shown in the Connect
Health blog. It is a custom agent on Amazon Connect, **not** a native Amazon Connect
Health capability, and it is reference material, not a turnkey deploy. Several steps must
be done in the Amazon Connect **admin console** and cannot be expressed as code. They are
called out below.

## What you are building
A native agentic self-service Orchestration AI agent (Nova Sonic voice) that, in one
conversation: verifies the caller (DOB + ZIP) via a custom `verifyPatient` MCP tool,
confirms their appointment, captures the patient's stated reason for the visit, and hands
off to a clinician via a Return-to-Control `Escalate` tool. The verified patient id and
the captured reason land on the contact so the clinician workspace opens the right chart
with that context already present.

## Scope boundaries (deliberate)
The agent records what the patient says and passes it to a clinician. It does **not**:
- assess symptoms, or assign an urgency or severity level
- give medical advice, or tell the patient what care they need
- direct the patient to emergency services or anywhere else
- route the encounter. The contact flow transfers to a queue exactly as it would
  without this agent, and nothing routes on the agent's output

If you extend this agent, keep a human in the loop for anything that resembles a clinical
or care-routing judgement. Adding an agent-assigned urgency level, in particular, changes
what this is.

## Prerequisites
- The workflow deployed (see docs/deployment.md): Connect instance, FHIR datastore with
  synthetic data, clinician workspace, media-streaming bridge.
- Amazon Q in Connect available in your Connect instance and region.
- boto3 >= 1.40 if you script any qconnect steps (older SDKs lack the ORCHESTRATION
  agent model).

## Artifacts
In `lambdas/pre-visit-intake/`:
- `index.py` — the `verifyPatient` Lambda (DOB + ZIP -> FHIR -> patient + appointment)
- `orchestration-prompt.yaml` — the agent's MESSAGES-format prompt
- `escalate-tool-schema.json` — the Return-to-Control Escalate tool schema
- `agentcore-gateway-config.json` — reference gateway and target config

## Steps

### 1. Deploy the verifyPatient Lambda (code)
Deploy `index.py` as a Lambda. Set `HEALTHLAKE_DATASTORE_ID` to your own datastore; there
is no default. Grant the function read access to the datastore. Test directly with a
synthetic patient from `backend/demo_cache/`, for example
`{"dateOfBirth":"1968-04-12","zipCode":"02139"}`, which should return `verified=true`.

### 2. Create a Q in Connect domain  [CONSOLE]
Connect admin console -> AI Agents -> **Add domain** -> create a new domain (default
encryption). This creates the assistant AND wires the Lex service-linked role's access.
Creating the assistant via the API instead of the console will NOT wire this correctly.

### 3. Create the AgentCore Gateway + target (verifyPatient)  [API or console]
Use `agentcore-gateway-config.json` as reference. The JWT authorizer must use your Connect
instance OIDC discovery URL and set `allowedAudience` to the gateway id, and
`supportedVersions` must include `2025-03-26`. Add a Lambda target pointing at the Lambda
from step 1 with the verifyPatient tool schema. The gateway role needs
`lambda:InvokeFunction` on that Lambda.

### 4. Register the gateway as an MCP integration  [CONSOLE — required]
Connect admin console -> Integrations -> Add integration -> **MCP server** -> select your
gateway. This is what makes the tool discoverable. An MCP integration created purely via
the AppIntegrations API did not populate tools in our testing; use the console wizard.

### 5. Create the Orchestration AI agent  [CONSOLE]
AI agent designer -> AI agents -> Create -> type **Orchestration**, copy from
**SelfServiceOrchestrator**. Create an AI prompt from `orchestration-prompt.yaml`
(ORCHESTRATION type, MESSAGES format) and attach it. Add tools:
- `verifyPatient` (the MCP tool discovered from your gateway)
- `Escalate` (Return-to-Control) using `escalate-tool-schema.json`

### 6. Grant the MCP tool on the security profile  [CONSOLE — required]
Users -> Security profiles -> (the profile the agent uses) -> grant ACCESS to the
verifyPatient MCP tool and its namespace. Without this the tool shows "Insufficient" and
the agent cannot call it. This cannot be done via the update-security-profile API for the
gateway namespace; use the console.

### 7. Wire the contact flow
Flow order: set logging -> analytics (RealTime Contact Lens) -> set voice -> welcome
(recording disclaimer only, no greeting) -> CreateWisdomSession (assistant +
orchestration agent version) -> UpdateContactData (WisdomSessionArn) -> Get customer
input with the Conversational AI bot whose QinConnect intent points at your assistant ->
Compare on `$.Lex.SessionAttributes.Tool == Escalate` -> set contact attributes
(`patient_id` = `$.Lex.SessionAttributes.patientId`, plus the captured reason and
visitSummary) -> start media streaming (Audio, Customer From) -> trigger bridge Lambda ->
transfer to queue. See `contact-flow/` for the reference structure.

Note the transfer target is the same queue you would use without this agent. Do not
branch the transfer on anything the agent produced.

## Known manual/console dependencies (why this is reference, not turnkey)
Steps 2, 4, 5, and 6 are console actions with no reliable code equivalent today. Budget
time for the JWT and audience config (step 3) and tool discovery (step 4); they are the
fiddly parts. Once discovery and the security-profile grant are correct, the agent calls
verifyPatient on a live call.
