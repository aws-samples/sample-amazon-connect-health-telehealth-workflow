# Notice: Sample Code and Synthetic Data

This repository contains sample code and synthetic data that demonstrate how
to build clinical workflows on AWS using Amazon Connect Health, AWS HealthLake,
and Amazon Bedrock.

## All Data Is Synthetic

No file in this repository contains:

- Real Protected Health Information (PHI) under HIPAA, HITECH, or any
  international healthcare privacy regulation
- Real Personally Identifiable Information (PII) of any individual
- Real AWS account identifiers, resource ARNs, access keys, or secrets
- Real medical record numbers, NPIs, or other identifiers from any
  production system

Patient names (Jordan Rivera, Sam Patel, Alex Nguyen) and identifiers
(`SYN#####`) are synthetic. The clinical data is illustrative and fictional,
in the style of records produced by [Synthea](https://synthea.mitre.org/),
MITRE's open-source synthetic patient generator. Clinical narratives, vital
signs, and procedure descriptions are illustrative scenarios written for
demonstration purposes only. Phone numbers use reserved example ranges (for
example, 555-0100 through 555-0199) for fictional use.

## Your Responsibility

If you adapt this sample, do not commit real PHI, PII, credentials, or
production identifiers. Load your own HealthLake datastore with synthetic data
(for example, Synthea output) and supply all configuration through environment
variables (see `.env.example`). This sample is for demonstration and education
only and is not intended to process real patient data without your own
security testing, compliance review, and hardening.
