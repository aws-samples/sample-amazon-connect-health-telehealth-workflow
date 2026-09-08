# Demo Cache (synthetic)

Pre-generated, FULLY SYNTHETIC API responses used by demo mode. When demo mode is
active (the `X-Demo-Mode: true` header, toggled with `Ctrl+Shift+D` in the UI),
the backend serves these files instead of calling live AWS services. This lets
anyone run the full end-to-end journey locally with no AWS account, credentials,
or Amazon Connect Health enablement.

All patients, identifiers (SYN#####), and clinical content here are fictional and
do not represent real people or real HealthLake resources.

## Files
- `patients_default.json` - patient list
- `synthesize_previsit_<mrn>.json` - pre-visit synthesis (pre-call)
- `patient_insights_<mrn>.json` - longitudinal insights
- `streaming_outputs_default.json` - SOAP note, medical codes, after-visit summary, transcript

## Regenerating
These files are produced by a generator kept outside the repo. To capture your own
data from live services instead, run the backend with `DEMO_RECORD=true` and the
responses will be written here.
