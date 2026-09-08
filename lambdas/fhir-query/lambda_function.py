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
    for p in appt.get("participant", []):
        actor = p.get("actor", {})
        if "Patient" in actor.get("reference", ""):
            return actor.get("display", "Unknown Patient")
    return "Unknown Patient"


def sort_desc(resources, key="start", limit=10):
    return sorted(resources, key=lambda r: r.get(key, ""), reverse=True)[:limit]


def completed_appointments(params):
    resources = fhir_get("Appointment", {"status": "fulfilled"})
    top = sort_desc(resources, "start", 10)
    results = [{"patient": patient_display(r), "date": r.get("start", "")[:10], "status": "Completed"} for r in top]
    return {"query_type": "completed_appointments", "count": len(results),
            "description": f"Most recent {len(results)} completed appointments", "results": results}


def needs_followup(params):
    resources = fhir_get("Appointment", {"status": "booked"})
    top = sort_desc(resources, "start", 10)
    results = [{"patient": patient_display(r), "date": r.get("start", "")[:10], "status": "Follow-up Needed"} for r in top]
    return {"query_type": "needs_followup", "count": len(results),
            "description": f"{len(results)} patients with pending appointments", "results": results}


def no_shows(params):
    resources = fhir_get("Appointment", {"status": "noshow"})
    top = sort_desc(resources, "start", 10)
    results = [{"patient": patient_display(r), "date": r.get("start", "")[:10], "status": "No-Show", "outreach_needed": True} for r in top]
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
    first_name = name.split()[0] if " " in name else name
    results = []
    for r in fhir_get("Patient", {"name": first_name})[:10]:
        ns = r.get("name", [{}])
        given = " ".join(ns[0].get("given", [])) if ns else ""
        family = ns[0].get("family", "") if ns else ""
        full = f"{given} {family}".strip()
        if name.lower() in full.lower() or full.lower() in name.lower():
            results.append({"name": full, "dob": r.get("birthDate", ""),
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
