# Amazon Connect Health — Unified Clinical AI Workspace

> **For demonstration and educational use only.** Not a production-ready
> implementation. Do not use it to process real patient data (PHI/PII) without
> your own security testing, compliance review, and hardening. **All patients
> and data in this repository are synthetic.**

A reference implementation of a telehealth clinical workflow on Amazon Connect
Health, from pre-call intake through post-call documentation and FHIR
write-back, presented in a single clinician workspace. A patient calls, the
clinician accepts, and pre-visit prep, live ambient documentation, coding, and
follow-up happen in one place.

It ships a **local demo mode** so you can experience the entire journey with no
AWS account, plus the **integration contracts** to wire live services as you
enable them.

## What this sample is (and is not)

- **It is:** a reference implementation, a runnable local demo of the full
  journey, and an integration cookbook (schemas, contact flow, config, SDK call
  shapes) for the parts that require enablement.
- **It is not:** a one-click clone. The native Amazon Connect Health agents
  require enablement via your AWS account team, and Patient Verification and
  Appointment Management also require EHR integration.

See **[docs/capability-matrix.md](docs/capability-matrix.md)** for exactly what
runs where.

## Quick start — local demo (no AWS)

Requires Python 3.9 or later.

```bash
git clone https://github.com/aws-samples/sample-amazon-connect-health-telehealth-workflow.git
cd sample-amazon-connect-health-telehealth-workflow/backend
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
python3 server.py
```

Then open <http://127.0.0.1:5000> in a browser.

Everything is served from `backend/demo_cache/` (synthetic data). No AWS
account, credentials, or Connect Health enablement required. Click through
pre-call, during-call (SOAP note + transcript), post-call (coding), and the
clinician workspace.

Demo mode is on by default on localhost, and a `DEMO MODE` badge is shown while
it is active. Press `Ctrl+Shift+D` (or click the badge) to toggle it off and
call live AWS services instead; the choice is remembered per browser.

## Architecture

![Unified Clinical AI Workflow Architecture](docs/architecture.png)

Full description in **[docs/architecture-overview.md](docs/architecture-overview.md)**.

## Components

| Component | Technology | Description |
|-----------|-----------|-------------|
| Clinical workspace UI | HTML/JS/CSS | Clinician workspace (embeddable as a third-party app) |
| Backend API | Python Flask (ECS Fargate) | Orchestrates agents, HealthLake, S3, Bedrock; serves the workspace |
| KVS Bridge | Java (ECS Fargate) | Streams call audio (KVS) to the ambient documentation agent |
| Bridge Trigger | Python Lambda | Invoked by the Contact Flow to start the bridge |
| fhir-query | Python Lambda | Care Intelligence action group; FHIR queries against HealthLake |
| SMS Notification | Python Lambda | S3 event-driven after-visit SMS (optional) |
| Custom Triage agent | Amazon Bedrock (AgentCore) | Worked example of extending the platform with your own agent |
| Clinical Data agent | Amazon Bedrock | Structured extraction from SOAP notes for FHIR write-back |

## Agents in the workflow

Native Amazon Connect Health agents: Patient Verification, Appointment
Management, Patient Insights, Ambient Documentation (Medical Scribe), Medical
Coding. Custom Bedrock agents built in this sample: Triage and Clinical Data.
See the capability matrix for which run locally, which are runnable now, and
which require enablement.

## Deployment

Three rungs, local demo → wire the self-serviceable pieces → wire the gated
Connect Health agents. See **[docs/deployment.md](docs/deployment.md)**.

## Security

- The backend binds to `127.0.0.1` and has **no authentication by default** —
  add authentication and TLS before exposing it beyond localhost.
- No hardcoded credentials; authentication via IAM roles and environment
  variables (`.env`, see `.env.example`).
- Least-privilege IAM; encryption in transit and at rest.
- Synthetic FHIR data only. No real PHI.

## License

MIT-0. See [LICENSE](LICENSE).
