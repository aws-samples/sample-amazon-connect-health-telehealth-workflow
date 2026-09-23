"""
Pre-visit Intake — verifyPatient tool (AWS Lambda)

Custom agent example. NOT a native Amazon Connect Health capability.

This Lambda backs the `verifyPatient` tool used by a custom orchestration
(agentic self-service) AI agent. The AI agent asks the caller for date of
birth and ZIP code, then invokes this tool through an Amazon Bedrock
AgentCore Gateway (MCP). The tool matches the caller against a FHIR data
store and returns identity plus today's appointment, so the agent can
confirm the visit and capture the patient's stated reason for it before
transferring to a clinician.

Scope boundaries, by design:
  * The agent captures what the patient says. It does not assess symptoms,
    assign an urgency level, give advice, or direct the patient anywhere.
  * Nothing here routes the encounter. The contact flow transfers to a queue
    exactly as it would without this agent. The captured reason is attached
    to the encounter for the clinician to read.

Invocation paths supported:
  1. AgentCore MCP  — tool name in context.client_context.custom['bedrockAgentCoreToolName']
                      (format "{target}___{tool}"); event is the input properties map.
  2. Direct invoke  — event = {"dateOfBirth": "...", "zipCode": "..."} (for local testing)

See docs/pre-visit-intake-runbook.md for the console setup (Q in Connect domain,
AgentCore Gateway, MCP integration, security profile). Those steps cannot be
fully expressed as code and must be done in the Connect admin console.
"""
import json, os, boto3, urllib.request, urllib.parse
from botocore.auth import SigV4Auth
from botocore.awsrequest import AWSRequest

# Required. Set HEALTHLAKE_DATASTORE_ID to your own datastore (see .env.example).
# There is deliberately no default: this sample must not point at someone else's data.
DATASTORE_ID = os.environ.get("HEALTHLAKE_DATASTORE_ID", "").strip()
REGION       = os.environ.get("AWS_DEFAULT_REGION", "us-east-1")
BASE_URL     = f"https://healthlake.{REGION}.amazonaws.com/datastore/{DATASTORE_ID}/r4"

# Demo appointment mapping, keyed by the synthetic patient ids shipped in
# backend/demo_cache/. Mirrors what the clinician workspace shows.
# Replace with a real scheduling-system lookup in production.
DEMO_SCHEDULE = {
    "SYN1000100000000000000000000000000000000000000000000000000000000": {"time": "9:00 AM",  "type": "Diabetes Follow-up"},
    "SYN1000200000000000000000000000000000000000000000000000000000000": {"time": "10:00 AM", "type": "Annual Physical"},
    "SYN1000300000000000000000000000000000000000000000000000000000000": {"time": "11:00 AM", "type": "Blood Pressure Check"},
}


def fhir_get(path):
    url = f"{BASE_URL}/{path}"
    creds = boto3.session.Session().get_credentials().get_frozen_credentials()
    req = AWSRequest(method="GET", url=url, headers={"Content-Type": "application/fhir+json"})
    SigV4Auth(creds, "healthlake", REGION).add_auth(req)
    http_req = urllib.request.Request(url, headers=dict(req.headers))
    with urllib.request.urlopen(http_req, timeout=10) as resp:
        return json.loads(resp.read().decode())


def verify_patient(params):
    """Match a patient by date of birth (YYYY-MM-DD) + ZIP code against the FHIR data store."""
    if not DATASTORE_ID:
        print("[PREVISIT] HEALTHLAKE_DATASTORE_ID is not set")
        return {"verified": False, "reason": "not_configured",
                "description": "The identity lookup is not configured."}

    dob = (params.get("dateOfBirth") or params.get("dob") or "").strip()
    zip_code = (params.get("zipCode") or params.get("zip") or "").strip()
    if not dob or not zip_code:
        return {"verified": False, "reason": "missing_input",
                "description": "Both dateOfBirth (YYYY-MM-DD) and zipCode are required."}

    print(f"[PREVISIT] verifyPatient: dob length={len(dob)}, zip length={len(zip_code)} (values redacted, PHI)")
    try:
        bundle = fhir_get(f"Patient?birthdate={urllib.parse.quote(dob)}&_count=25")
    except Exception as e:
        print(f"[PREVISIT] FHIR lookup error: {type(e).__name__}")
        return {"verified": False, "reason": "lookup_error",
                "description": "Could not complete the identity lookup."}

    match = None
    for entry in bundle.get("entry", []):
        p = entry.get("resource", {})
        for addr in p.get("address", []):
            if str(addr.get("postalCode", "")).strip() == zip_code:
                match = p
                break
        if match:
            break

    if not match:
        return {"verified": False, "reason": "no_match",
                "description": "No patient matched the provided date of birth and ZIP code."}

    ns = match.get("name", [{}])[0]
    full = (" ".join(ns.get("given", [])) + " " + ns.get("family", "")).strip()
    pid = match.get("id", "")
    result = {
        "verified": True,
        "patient_id": pid,
        "patient_name": full,
        "description": f"Identity verified for {full}.",
    }
    appt = DEMO_SCHEDULE.get(pid)
    if appt:
        result.update({"hasAppointment": True,
                       "appointmentType": appt["type"],
                       "appointmentTime": appt["time"]})
        result["description"] += f" They have a {appt['type']} appointment today at {appt['time']}."
    else:
        result["hasAppointment"] = False
        result["description"] += " No appointment is scheduled for today."
    return result


MCP_TOOL_MAP = {"verifyPatient": verify_patient}


def lambda_handler(event, context):
    # AgentCore MCP path: tool name lives in the client context
    tool_name = None
    try:
        custom = getattr(getattr(context, "client_context", None), "custom", None)
        if custom and "bedrockAgentCoreToolName" in custom:
            raw = custom["bedrockAgentCoreToolName"]
            tool_name = raw.split("___")[-1] if "___" in raw else raw
    except Exception:
        pass

    if tool_name:
        fn = MCP_TOOL_MAP.get(tool_name)
        if not fn:
            return {"error": f"Unknown MCP tool: {tool_name}"}
        return fn(event if isinstance(event, dict) else {})

    # Direct invoke (local testing)
    return verify_patient(event if isinstance(event, dict) else {})
