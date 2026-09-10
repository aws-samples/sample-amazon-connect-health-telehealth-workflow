# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""
connect-health-fhir-query — Care-intelligence FHIR tool (Bedrock Agent + direct invoke)

Queries a single AWS HealthLake FHIR datastore via the REST API using SigV4.
Appointment queries are date-independent (they return the most recent N records).

Configuration (environment variables):
  HEALTHLAKE_DATASTORE_ID   The FHIR datastore ID to query.
  AWS_REGION                Region of the datastore (default us-east-1).
"""
import json, os, urllib.request, urllib.parse
import boto3
from botocore.auth import SigV4Auth
from botocore.awsrequest import AWSRequest

DATASTORE_ID = os.environ.get("HEALTHLAKE_DATASTORE_ID", "<HEALTHLAKE_DATASTORE_ID>")
REGION = os.environ.get("AWS_REGION", "us-east-1")
HL_BASE = f"https://healthlake.{REGION}.amazonaws.com"

# SNOMED / ICD-10 codes for type 2 diabetes (used by the care-gap query)
T2D_CODES = ["44054006", "73211009", "E11"]


def get_creds():
    return boto3.session.Session().get_credentials().get_frozen_credentials()


def fhir_get(path, params):
    creds = get_creds()
    query = urllib.parse.urlencode(params)
    url = f"{HL_BASE}/datastore/{DATASTORE_ID}/r4/{path}?{query}&_count=50"
    req_obj = AWSRequest(method="GET", url=url, headers={"Content-Type": "application/fhir+json"})
    SigV4Auth(creds, "healthlake", REGION).add_auth(req_obj)
    req = urllib.request.Request(url, headers=dict(req_obj.headers))
    try:
        with urllib.request.urlopen(req, timeout=55) as r:
            bundle = json.loads(r.read())
        entries = bundle.get("entry", [])
        print(f"  fhir [{path}] params={params}: {len(entries)} results")
        return [e["resource"] for e in entries if "resource" in e]
    except Exception as e:
        print(f"  fhir ERROR [{path}]: {e}")
        return []


def patient_display(appt):
    """Legacy: reads the denormalized actor.display string. Kept as a fallback.
    Prefer resolve_patient_name(), which reads the authoritative Patient.name."""
    for p in appt.get("participant", []):
        actor = p.get("actor", {})
        if "Patient" in actor.get("reference", ""):
            return actor.get("display", "Unknown Patient")
    return "Unknown Patient"

# id -> resolved name. Module scope so warm invocations reuse it.
_PATIENT_NAME_CACHE = {}

def fhir_read(resource_type, rid, cross_account=False):
    """Read a single FHIR resource by id."""
    creds, ds_id = get_creds(cross_account)
    url = f"{HL_BASE}/datastore/{ds_id}/r4/{resource_type}/{rid}"
    req_obj = AWSRequest(method="GET", url=url, headers={"Content-Type": "application/fhir+json"})
    SigV4Auth(creds, "healthlake", REGION).add_auth(req_obj)
    req = urllib.request.Request(url, headers=dict(req_obj.headers))
    try:
        with urllib.request.urlopen(req, timeout=20) as r:
            return json.loads(r.read())
    except Exception as e:
        print(f"  fhir read ERROR [{resource_type}/{rid}]: {e}")
        return {}

def _name_of(patient_resource):
    ns = patient_resource.get("name") or [{}]
    n = ns[0] if ns else {}
    return (" ".join(n.get("given", [])) + " " + n.get("family", "")).strip()

def _norm_name(v):
    """Casefold and strip accents so an unaccented query matches an accented name."""
    import unicodedata
    return "".join(c for c in unicodedata.normalize("NFKD", v or "")
                   if not unicodedata.combining(c)).casefold().strip()

def name_matches(query, full):
    """Tolerant patient-name match.

    Plain substring comparison fails on dropped middle names ("Mary Johnson" vs
    a record stored as "Mary Anne Johnson") and on accents. Compare accent-folded
    tokens instead: every token the caller supplied must appear in the stored
    name, which also makes first-name-only and full-name queries work.
    """
    q = _norm_name(query).split()
    f = _norm_name(full).split()
    if not q or not f:
        return False
    if all(tok in f for tok in q):
        return True
    return len(q) >= 2 and q[0] == f[0] and q[-1] == f[-1]

def find_patients_by_name(name, cross_account=False):
    """Search for patients by name, tolerant of accents and middle names.

    HealthLake's name index is accent-sensitive and rejects values containing a
    space, so probe one token at a time (given name, then family name) and filter
    the results with name_matches().
    """
    tokens = [t for t in (name or "").split() if t]
    if not tokens:
        return []
    probes = []
    for t in (tokens[0], tokens[-1]):
        if t not in probes:
            probes.append(t)
    seen, matches = set(), []
    for probe in probes:
        for r in fhir_get("Patient", {"name": probe}, cross_account=cross_account):
            rid = r.get("id")
            if rid in seen:
                continue
            if name_matches(name, _name_of(r)):
                seen.add(rid)
                matches.append(r)
        if matches:
            break
    return matches

def resolve_patient_name(appt, cross_account=False):
    """Authoritative patient name for an Appointment.

    FHIR Reference.display is a denormalized convenience copy and goes stale (for
    example after a patient rename), which silently shows the wrong identity in
    care-manager views. Dereference actor.reference and read Patient.name
    instead, falling back to actor.display only if the Patient is unreadable.
    """
    ref_id, display = None, "Unknown Patient"
    for p in appt.get("participant", []):
        actor = p.get("actor", {})
        if "Patient" in actor.get("reference", ""):
            ref_id = actor["reference"].split("/")[-1]
            display = actor.get("display", "Unknown Patient")
            break
    if not ref_id:
        return display
    if ref_id not in _PATIENT_NAME_CACHE:
        _PATIENT_NAME_CACHE[ref_id] = _name_of(fhir_read("Patient", ref_id, cross_account))
    return _PATIENT_NAME_CACHE.get(ref_id) or display


def sort_desc(resources, key="start", limit=10):
    return sorted(resources, key=lambda r: r.get(key, ""), reverse=True)[:limit]


def completed_appointments(params):
    resources = fhir_get("Appointment", {"status": "fulfilled"})
    top = sort_desc(resources, "start", 10)
    results = [{"patient": resolve_patient_name(r), "date": r.get("start", "")[:10], "status": "Completed"} for r in top]
    return {"query_type": "completed_appointments", "count": len(results),
            "description": f"Most recent {len(results)} completed appointments", "results": results}


def needs_followup(params):
    resources = fhir_get("Appointment", {"status": "booked"})
    top = sort_desc(resources, "start", 10)
    results = [{"patient": resolve_patient_name(r), "date": r.get("start", "")[:10], "status": "Follow-up Needed"} for r in top]
    return {"query_type": "needs_followup", "count": len(results),
            "description": f"{len(results)} patients with pending appointments", "results": results}


def no_shows(params):
    resources = fhir_get("Appointment", {"status": "noshow"})
    top = sort_desc(resources, "start", 10)
    results = [{"patient": resolve_patient_name(r), "date": r.get("start", "")[:10], "status": "No-Show", "outreach_needed": True} for r in top]
    return {"query_type": "no_shows", "count": len(results),
            "description": f"{len(results)} patients who missed appointments; outreach needed", "results": results}


def diabetic_overdue_a1c(params):
    results, seen = [], set()
    for code in T2D_CODES:
        for r in fhir_get("Condition", {"code": code}):
            ref = r.get("subject", {}).get("reference", "")
            pid = ref.split("/")[-1] if ref else ""
            name = r.get("subject", {}).get("display", "Unknown")
            if pid and pid not in seen:
                seen.add(pid)
                results.append({"name": name, "condition": r.get("code", {}).get("text", "Type 2 Diabetes"),
                                "a1c_status": "Overdue", "action": "Schedule HbA1c lab order"})
        if results:
            break
    return {"query_type": "diabetic_overdue_a1c", "count": len(results),
            "description": "Diabetic patients with no recent HbA1c on record; care gap identified", "results": results[:20]}


def patient_summary(params):
    name = params.get("name", "")
    if not name:
        return {"error": "name required", "results": []}
    results = []
    for r in find_patients_by_name(name)[:10]:
        results.append({"name": _name_of(r), "dob": r.get("birthDate", ""),
                        "gender": r.get("gender", ""), "id": r.get("id", "")})
    return {"query_type": "patient_summary", "search_name": name, "count": len(results), "results": results}


QUERY_MAP = {
    "/completed_appointments": completed_appointments,
    "/needs_followup":         needs_followup,
    "/no_shows":               no_shows,
    "/diabetic_overdue_a1c":   diabetic_overdue_a1c,
    "/patient_summary":        patient_summary,
}


def bedrock_wrap(event, result, status=200):
    return {"messageVersion": "1.0", "response": {
        "actionGroup": event.get("actionGroup", ""), "apiPath": event.get("apiPath", ""),
        "httpMethod": event.get("httpMethod", "GET"), "httpStatusCode": status,
        "responseBody": {"application/json": {"body": json.dumps(result)}}}}


def lambda_handler(event, context):
    print("EVENT:", json.dumps(event)[:300])
    is_bedrock = "actionGroup" in event
    api_path = event.get("apiPath", "") if is_bedrock else "/" + event.get("query_type", "")
    params = ({p["name"]: p["value"] for p in event.get("parameters", [])} if is_bedrock else event)
    try:
        fn = QUERY_MAP.get(api_path)
        if not fn:
            raise ValueError(f"Unknown: {api_path}")
        result = fn(params)
        return bedrock_wrap(event, result) if is_bedrock else result
    except Exception as e:
        print(f"ERROR: {e}")
        err = {"error": str(e), "results": []}
        return bedrock_wrap(event, err, 500) if is_bedrock else err
