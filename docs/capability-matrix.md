# Capability Matrix

What each part of the workflow does **in this sample**, and what it maps to in
**production**. This is the honest picture: you can experience the whole journey
locally, run a subset live today, and wire the rest once you have Amazon Connect
Health enablement.

## Status legend

- **Runnable now** — works with a standard AWS account, no Connect Health enablement.
- **Demo-simulated** — replayed from `backend/demo_cache/` in local demo mode; not a live call.
- **Requires enablement** — needs Amazon Connect Health (request via your AWS account team); some steps also require EHR integration.

## Per-step

| Phase | Capability | In this sample | Production |
|---|---|---|---|
| Pre-call | Patient Verification | Demo-simulated (represented in the pre-call flow) | Connect Health Patient Verification Agent — requires enablement **+ EHR integration** |
| Pre-call | **Pre-visit Intake** | **Runnable now** — custom agent, AgentCore gateway → Lambda (see `pre-visit-intake-runbook.md`; some setup is console-only) | Your own custom agent (same code) |
| Pre-call | Appointment Management | Demo-simulated | Connect Health Appointment Management — requires enablement **+ EHR integration** |
| During call | Patient Insights | Demo-simulated (cached pre-visit narrative) | Connect Health Patient Insights — requires enablement; reads HealthLake |
| During call | Ambient Documentation (SOAP) | Demo-simulated (cached SOAP + transcript) | Connect Health Medical Scribe — requires enablement; live audio via the bridge |
| Post-call | Medical Coding | Demo-simulated (cached ICD-10 / CPT) | Connect Health Medical Coding — requires enablement |
| Post-call | **Clinical Data extraction** | **Runnable now** — Amazon Bedrock | Same code |
| Post-call | **FHIR write-back + Care Intelligence** | **Runnable now** — fhir-query Lambda + your HealthLake (Synthea data) | Same code |
| Workspace | **Clinician workspace UI** | **Runnable now** — served by the backend | Same / a third-party app |

> Availability of each Connect Health agent (general availability vs preview)
> varies by agent and changes over time. Confirm current status with your AWS
> account team. _(Maintainer: drop exact availability here if you want it stated.)_

## How to read this

- **See the whole journey today:** run local demo mode (`python server.py --demo`,
  open `http://localhost:5000`, press `Ctrl+Shift+D`). Every row above renders
  end-to-end from synthetic data, with no AWS account.
- **"Runnable now" rows go live** when you add AWS credentials, a HealthLake
  datastore with synthetic Synthea data, and Bedrock access. See `deployment.md`.
- **"Requires enablement" rows** ship with their integration contracts in this
  repo (action-group schemas under `lambdas/`, the Connect flow under
  `contact-flow/`, and config hooks like `DOMAIN_ID`) so you can wire them the
  day you get access.
