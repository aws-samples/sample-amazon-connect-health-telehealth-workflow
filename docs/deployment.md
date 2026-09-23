# Deployment

Three rungs, from "see it" to "run it live." Most readers start and stop at
Rung 1. Rungs 2 and 3 are additive.

## Rung 1 — Local demo (no AWS)

The fastest way to experience the full end-to-end journey. Everything is served
from `backend/demo_cache/` (synthetic data). No AWS account, credentials, or
Connect Health enablement.

```
pip install -r backend/requirements.txt
python backend/server.py --demo
# open http://localhost:5000, then press Ctrl+Shift+D to enable demo mode
```

You can click through pre-call, during-call (SOAP + transcript), post-call
(coding), and the clinician workspace, all from synthetic responses.

## Rung 2 — Wire the self-serviceable pieces (standard AWS account)

Makes the GA / non-gated pieces live: the custom Pre-visit Intake agent, Clinical
Data extraction, FHIR write-back, Care Intelligence population queries, and the
workspace.

**Prerequisites:** an AWS account with credentials and a region set, Amazon
Bedrock model access, and an Amazon HealthLake datastore loaded with synthetic
Synthea FHIR data.

1. `cp .env.example .env` and set `AWS_REGION`, `HEALTHLAKE_DATASTORE_ID`,
   `BEDROCK_MODEL_ID` / `BEDROCK_REGION`, and the S3 bucket variables.
2. Deploy the `fhir-query` Lambda and the Care Intelligence Bedrock agent
   (`setup-bedrock-agent.sh`, which creates `connect-health-care-intelligence`).
   The custom Pre-visit Intake agent is set up separately, per
   `docs/pre-visit-intake-runbook.md`; several of its steps are console-only.
3. Run the backend without `--demo`, and toggle demo mode off in the UI.

**Loading synthetic data:** generate a Synthea patient bundle and import it into
HealthLake (see the Synthea project and the HealthLake FHIR import docs). Keep
the datastore in the **same account** as any Connect Health domain you add in
Rung 3.

## Rung 3 — Wire the gated Connect Health agents

Makes the native point-of-care agents live: Patient Verification, Appointment
Management, Patient Insights, Ambient Documentation, Medical Coding.

**Prerequisites:** Amazon Connect Health enablement (request via your AWS
account team), an Amazon Connect instance + Contact Flow, and EHR integration
for Verification and Appointment Management.

1. Create a Connect Health domain and set `DOMAIN_ID` in `.env`.
2. Deploy the Java bridge (ECS) for live ambient audio.
3. Import the Contact Flow from `contact-flow/` and wire the action-group
   schemas under `lambdas/` for each agent.

The same backend and UI then invoke the live agents in place of the demo cache.
Confirm each agent's current availability with your AWS account team.

## Security

The sample backend binds to `127.0.0.1` and has **no authentication** by
default. Add authentication and TLS, and review IAM scoping, before exposing it
beyond localhost or handling any real data.
