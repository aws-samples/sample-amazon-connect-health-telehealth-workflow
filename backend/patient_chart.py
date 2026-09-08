"""
Patient Chart — Dynamic HealthLake Data for Clinical Workspace
Fetches real patient clinical data (conditions, medications, allergies, encounters)
from HealthLake and returns it structured for the provider UI.

Added: July 8, 2026
"""
import json
import boto3
import urllib.request
import urllib.parse
from botocore.auth import SigV4Auth
from botocore.awsrequest import AWSRequest

from config import AWS_PROFILE, AWS_REGION, HEALTHLAKE_DATASTORE_ID


HL_BASE = f"https://healthlake.{AWS_REGION}.amazonaws.com"
DATASTORE_ID = HEALTHLAKE_DATASTORE_ID


def _get_creds():
    """Get SigV4 credentials for HealthLake access."""
    if AWS_PROFILE and AWS_PROFILE not in ("default", ""):
        sess = boto3.Session(profile_name=AWS_PROFILE)
    else:
        sess = boto3.Session()
    return sess.get_credentials().get_frozen_credentials()


def _fhir_get(resource_type, params):
    """Execute a FHIR GET query against HealthLake with SigV4."""
    creds = _get_creds()
    query = urllib.parse.urlencode(params)
    url = f"{HL_BASE}/datastore/{DATASTORE_ID}/r4/{resource_type}?{query}&_count=50"
    req_obj = AWSRequest(method="GET", url=url, headers={"Content-Type": "application/fhir+json"})
    SigV4Auth(creds, "healthlake", AWS_REGION).add_auth(req_obj)
    req = urllib.request.Request(url, headers=dict(req_obj.headers))
    try:
        with urllib.request.urlopen(req, timeout=15) as r:
            bundle = json.loads(r.read())
        entries = bundle.get("entry", [])
        return [e["resource"] for e in entries if "resource" in e]
    except Exception as e:
        print(f"[patient_chart] FHIR GET {resource_type} error: {e}")
        return []


def get_patient_chart(patient_id):
    """
    Fetch full clinical chart for a patient from HealthLake.
    Returns structured data: demographics, conditions, medications, allergies, recent encounters.
    """
    result = {
        "success": True,
        "patientId": patient_id,
        "demographics": {},
        "conditions": [],
        "medications": [],
        "allergies": [],
        "encounters": [],
        "observations": []
    }

    # 1. Patient demographics
    patients = _fhir_get("Patient", {"_id": patient_id})
    if patients:
        p = patients[0]
        names = p.get("name", [{}])
        given = " ".join(names[0].get("given", [])) if names else ""
        family = names[0].get("family", "") if names else ""
        result["demographics"] = {
            "name": f"{given} {family}".strip(),
            "dob": p.get("birthDate", ""),
            "gender": p.get("gender", ""),
            "id": p.get("id", "")
        }

    # 2. Conditions (active problems)
    conditions = _fhir_get("Condition", {"subject": f"Patient/{patient_id}"})
    for c in conditions:
        code_obj = c.get("code", {})
        codings = code_obj.get("coding", [{}])
        result["conditions"].append({
            "display": code_obj.get("text", codings[0].get("display", "Unknown") if codings else "Unknown"),
            "code": codings[0].get("code", "") if codings else "",
            "system": codings[0].get("system", "") if codings else "",
            "status": c.get("clinicalStatus", {}).get("coding", [{}])[0].get("code", ""),
            "onsetDate": c.get("onsetDateTime", c.get("onsetPeriod", {}).get("start", ""))
        })

    # 3. Medications
    medications = _fhir_get("MedicationRequest", {"subject": f"Patient/{patient_id}"})
    for m in medications:
        med = m.get("medicationCodeableConcept", {})
        codings = med.get("coding", [{}])
        dosage = m.get("dosageInstruction", [{}])
        result["medications"].append({
            "display": med.get("text", codings[0].get("display", "Unknown") if codings else "Unknown"),
            "status": m.get("status", ""),
            "dosage": dosage[0].get("text", "") if dosage else "",
            "authoredOn": m.get("authoredOn", "")
        })

    # 4. Allergies
    allergies = _fhir_get("AllergyIntolerance", {"patient": f"Patient/{patient_id}"})
    for a in allergies:
        code_obj = a.get("code", {})
        codings = code_obj.get("coding", [{}])
        reactions = a.get("reaction", [{}])
        manifestation = ""
        if reactions and reactions[0].get("manifestation"):
            man_codings = reactions[0]["manifestation"][0].get("coding", [{}])
            manifestation = man_codings[0].get("display", "") if man_codings else ""
        result["allergies"].append({
            "substance": code_obj.get("text", codings[0].get("display", "Unknown") if codings else "Unknown"),
            "severity": reactions[0].get("severity", "") if reactions else "",
            "manifestation": manifestation,
            "category": a.get("category", [""])[0] if a.get("category") else "",
            "status": a.get("clinicalStatus", {}).get("coding", [{}])[0].get("code", "")
        })

    # 5. Recent Encounters (last 10)
    encounters = _fhir_get("Encounter", {"subject": f"Patient/{patient_id}", "_sort": "-date"})
    for e in encounters[:10]:
        enc_type = e.get("type", [{}])
        type_text = enc_type[0].get("text", "") if enc_type else ""
        if not type_text and enc_type:
            codings = enc_type[0].get("coding", [{}])
            type_text = codings[0].get("display", "Encounter") if codings else "Encounter"
        result["encounters"].append({
            "type": type_text,
            "status": e.get("status", ""),
            "date": e.get("period", {}).get("start", ""),
            "id": e.get("id", "")
        })

    # 6. Recent Observations (vitals, labs — last 20)
    observations = _fhir_get("Observation", {"subject": f"Patient/{patient_id}", "_sort": "-date"})
    for o in observations[:20]:
        code_obj = o.get("code", {})
        codings = code_obj.get("coding", [{}])
        value = ""
        if o.get("valueQuantity"):
            vq = o["valueQuantity"]
            value = f"{vq.get('value', '')} {vq.get('unit', '')}"
        elif o.get("valueCodeableConcept"):
            value = o["valueCodeableConcept"].get("text", "")
        result["observations"].append({
            "display": code_obj.get("text", codings[0].get("display", "Unknown") if codings else "Unknown"),
            "value": value,
            "date": o.get("effectiveDateTime", ""),
            "status": o.get("status", "")
        })

    return result


# =============================================================================
# FHIR Write-Back with Clinical Data Agent
# =============================================================================

from clinical_data_agent import (
    extract_clinical_data,
    build_fhir_medication_request,
    build_fhir_allergy_intolerance,
    build_fhir_observation,
    build_fhir_condition
)


def _fhir_post(resource_type, resource_body):
    """Write a FHIR resource to HealthLake."""
    import urllib.request
    creds = _get_creds()
    url = f"{HL_BASE}/datastore/{DATASTORE_ID}/r4/{resource_type}"
    body_bytes = json.dumps(resource_body).encode('utf-8')
    req_obj = AWSRequest(method="POST", url=url, data=body_bytes,
                         headers={"Content-Type": "application/fhir+json",
                                  "Accept": "application/fhir+json"})
    SigV4Auth(creds, "healthlake", AWS_REGION).add_auth(req_obj)
    req = urllib.request.Request(url, data=body_bytes, headers=dict(req_obj.headers), method="POST")
    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            result = json.loads(resp.read())
            return result
    except Exception as e:
        print(f"[FHIR-POST] {resource_type} error: {e}")
        raise


def fhir_writeback_with_extraction(patient_id, patient_name, session_id, soap_text, medical_codes=None):
    """
    Full FHIR write-back pipeline:
    1. Create Encounter
    2. Create DocumentReference (SOAP note)
    3. Create Conditions from ICD-10 codes (existing behavior)
    4. NEW: Extract structured data via Bedrock Clinical Data Agent
    5. NEW: Write MedicationRequests, AllergyIntolerances, Observations
    
    Returns summary of all resources created.
    """
    import base64
    from datetime import datetime
    
    now_iso = datetime.utcnow().strftime('%Y-%m-%dT%H:%M:%SZ')
    today = datetime.utcnow().strftime('%Y-%m-%d')
    
    results = {
        "success": True,
        "encounter_id": None,
        "document_reference_id": None,
        "condition_ids": [],
        "medication_request_ids": [],
        "allergy_intolerance_ids": [],
        "observation_ids": [],
        "extraction_summary": {}
    }
    
    # 1. Create Encounter
    encounter_body = {
        "resourceType": "Encounter",
        "status": "finished",
        "class": {
            "system": "http://terminology.hl7.org/CodeSystem/v3-ActCode",
            "code": "VR",
            "display": "virtual"
        },
        "type": [{
            "coding": [{"system": "http://snomed.info/sct", "code": "11429006", "display": "Telehealth"}],
            "text": "Telehealth Visit"
        }],
        "subject": {"reference": f"Patient/{patient_id}", "display": patient_name},
        "period": {"start": f"{today}T00:00:00Z", "end": now_iso},
        "identifier": [{"system": "urn:connecthealth:sessionId", "value": session_id}]
    }
    
    try:
        enc_result = _fhir_post("Encounter", encounter_body)
        results["encounter_id"] = enc_result.get("id", "")
        print(f"[FHIR-WB] Encounter: {results['encounter_id']}")
    except Exception as e:
        print(f"[FHIR-WB] Encounter FAILED: {e}")
    
    encounter_id = results["encounter_id"]
    
    # 2. Create DocumentReference (SOAP note)
    if soap_text:
        soap_b64 = base64.b64encode(soap_text.encode('utf-8')).decode('utf-8')
        doc_ref_body = {
            "resourceType": "DocumentReference",
            "status": "current",
            "type": {"coding": [{"system": "http://loinc.org", "code": "11506-3", "display": "Progress note"}]},
            "subject": {"reference": f"Patient/{patient_id}", "display": patient_name},
            "date": now_iso,
            "content": [{"attachment": {"contentType": "text/plain", "data": soap_b64, "title": f"SOAP Note - {today}"}}],
            "identifier": [{"system": "urn:connecthealth:sessionId", "value": session_id}]
        }
        if encounter_id:
            doc_ref_body["context"] = {"encounter": [{"reference": f"Encounter/{encounter_id}"}]}
        
        try:
            doc_result = _fhir_post("DocumentReference", doc_ref_body)
            results["document_reference_id"] = doc_result.get("id", "")
            print(f"[FHIR-WB] DocumentReference: {results['document_reference_id']}")
        except Exception as e:
            print(f"[FHIR-WB] DocumentReference FAILED: {e}")
    
    # 3. Create Conditions from ICD-10 medical codes (existing behavior)
    if medical_codes:
        raw_codes = medical_codes.get('medicalCodes') or medical_codes
        code_array = raw_codes if isinstance(raw_codes, list) else []
        icd10_codes = [c for c in code_array if c.get('type') == 'ICD10CM']
        for code in icd10_codes[:5]:
            code_value = code.get('name', '').strip()
            code_desc = code.get('description', code_value)
            if not code_value:
                continue
            condition_body = {
                "resourceType": "Condition",
                "clinicalStatus": {"coding": [{"system": "http://terminology.hl7.org/CodeSystem/condition-clinical", "code": "active"}]},
                "verificationStatus": {"coding": [{"system": "http://terminology.hl7.org/CodeSystem/condition-ver-status", "code": "confirmed"}]},
                "code": {"coding": [{"system": "http://hl7.org/fhir/sid/icd-10-cm", "code": code_value, "display": code_desc}], "text": code_desc},
                "subject": {"reference": f"Patient/{patient_id}", "display": patient_name},
                "recordedDate": today
            }
            if encounter_id:
                condition_body["encounter"] = {"reference": f"Encounter/{encounter_id}"}
            try:
                cond_result = _fhir_post("Condition", condition_body)
                results["condition_ids"].append(cond_result.get("id", ""))
                print(f"[FHIR-WB] Condition (ICD-10): {code_value}")
            except Exception as e:
                print(f"[FHIR-WB] Condition {code_value} FAILED: {e}")
    
    # 4. NEW — Clinical Data Agent: Extract structured data from SOAP via Bedrock
    print("[FHIR-WB] Running Clinical Data Agent extraction...")
    extracted = extract_clinical_data(soap_text)
    results["extraction_summary"] = {
        "medications_extracted": len(extracted.get("medications", [])),
        "allergies_extracted": len(extracted.get("allergies", [])),
        "observations_extracted": len(extracted.get("observations", [])),
        "conditions_extracted": len(extracted.get("conditions", []))
    }
    
    # 5. Write MedicationRequests
    for med in extracted.get("medications", []):
        resource = build_fhir_medication_request(med, patient_id, patient_name, encounter_id)
        try:
            med_result = _fhir_post("MedicationRequest", resource)
            results["medication_request_ids"].append(med_result.get("id", ""))
            print(f"[FHIR-WB] MedicationRequest: {med.get('name')}")
        except Exception as e:
            print(f"[FHIR-WB] MedicationRequest FAILED ({med.get('name')}): {e}")
    
    # 6. Write AllergyIntolerances
    for allergy in extracted.get("allergies", []):
        resource = build_fhir_allergy_intolerance(allergy, patient_id, patient_name)
        try:
            allergy_result = _fhir_post("AllergyIntolerance", resource)
            results["allergy_intolerance_ids"].append(allergy_result.get("id", ""))
            print(f"[FHIR-WB] AllergyIntolerance: {allergy.get('substance')}")
        except Exception as e:
            print(f"[FHIR-WB] AllergyIntolerance FAILED ({allergy.get('substance')}): {e}")
    
    # 7. Write Observations
    for obs in extracted.get("observations", []):
        resource = build_fhir_observation(obs, patient_id, patient_name, encounter_id)
        try:
            obs_result = _fhir_post("Observation", resource)
            results["observation_ids"].append(obs_result.get("id", ""))
            print(f"[FHIR-WB] Observation: {obs.get('name')}")
        except Exception as e:
            print(f"[FHIR-WB] Observation FAILED ({obs.get('name')}): {e}")
    
    # 8. Write additional Conditions from extraction (beyond ICD-10 codes)
    for condition in extracted.get("conditions", []):
        resource = build_fhir_condition(condition, patient_id, patient_name, encounter_id)
        try:
            cond_result = _fhir_post("Condition", resource)
            results["condition_ids"].append(cond_result.get("id", ""))
            print(f"[FHIR-WB] Condition (extracted): {condition.get('name')}")
        except Exception as e:
            print(f"[FHIR-WB] Condition FAILED ({condition.get('name')}): {e}")
    
    # Summary
    total = (1 + (1 if results["document_reference_id"] else 0) + 
             len(results["condition_ids"]) + len(results["medication_request_ids"]) +
             len(results["allergy_intolerance_ids"]) + len(results["observation_ids"]))
    print(f"[FHIR-WB] COMPLETE — {total} resources written to HealthLake")
    
    return results
