"""
Clinical Data Agent — Bedrock-powered extraction of structured clinical data from SOAP notes.
On Approve & Sign, extracts medications, allergies, observations from the SOAP text
and writes them as discrete FHIR resources to HealthLake.

This is the "intelligence layer" between documentation and the patient record —
just like a real EHR processes a signed note into structured data.

Added: July 8, 2026
"""
import json
import boto3
from datetime import datetime

from config import AWS_PROFILE, AWS_REGION, BEDROCK_REGION


EXTRACTION_PROMPT = """You are a clinical data extraction agent. Given a SOAP note from a patient encounter, extract ALL structured clinical data into the following categories.

RULES:
- Extract ONLY what is explicitly stated or clearly implied in the note
- For medications: include name, dose, frequency, route, and whether it's new/continued/changed/discontinued
- For allergies: include substance, reaction, severity if mentioned
- For observations: include vitals (BP, HR, weight, temp, SpO2), lab values (A1C, glucose, etc.), and any measurable findings
- For conditions: include any diagnoses, problems, or clinical findings mentioned (beyond what ICD-10 codes would capture — include symptoms being tracked)
- Use standard terminology where possible (RxNorm for meds, SNOMED for conditions, LOINC for observations)

Return ONLY valid JSON in this exact structure (no markdown, no explanation):
{
    "medications": [
        {
            "name": "medication name",
            "dose": "dose with units",
            "frequency": "frequency (e.g., once daily, BID, PRN)",
            "route": "oral/IV/topical/etc",
            "status": "active|stopped|new|changed",
            "rxnorm_code": "RxNorm code if known, otherwise empty string"
        }
    ],
    "allergies": [
        {
            "substance": "allergen name",
            "reaction": "reaction description",
            "severity": "mild|moderate|severe",
            "category": "medication|food|environment"
        }
    ],
    "observations": [
        {
            "name": "observation name (e.g., Blood Pressure, Weight, HbA1c)",
            "value": "numeric value",
            "unit": "unit of measure",
            "loinc_code": "LOINC code if known, otherwise empty string"
        }
    ],
    "conditions": [
        {
            "name": "condition/diagnosis name",
            "status": "active|resolved|recurring",
            "snomed_code": "SNOMED code if known, otherwise empty string"
        }
    ]
}

If a category has no data, return an empty array for that category.

SOAP NOTE:
{soap_text}
"""


def _get_bedrock_client():
    """Get Bedrock Runtime client."""
    if AWS_PROFILE and AWS_PROFILE not in ("default", ""):
        session = boto3.Session(profile_name=AWS_PROFILE)
    else:
        session = boto3.Session()
    return session.client('bedrock-runtime', region_name=BEDROCK_REGION)


def extract_clinical_data(soap_text):
    """
    Use Bedrock (Claude) to extract structured clinical data from a SOAP note.
    Returns dict with medications, allergies, observations, conditions.
    """
    if not soap_text or len(soap_text.strip()) < 20:
        return {"medications": [], "allergies": [], "observations": [], "conditions": []}

    client = _get_bedrock_client()
    
    prompt = EXTRACTION_PROMPT.replace("{soap_text}", soap_text)
    
    body = json.dumps({
        "anthropic_version": "bedrock-2023-05-31",
        "max_tokens": 4096,
        "messages": [{"role": "user", "content": prompt}],
        "temperature": 0.1  # Low temperature for consistent extraction
    })
    
    response = client.invoke_model(
        modelId="us.anthropic.claude-sonnet-4-5-20250929-v1:0",
        body=body,
        contentType="application/json",
        accept="application/json"
    )
    
    result = json.loads(response['body'].read())
    content = result.get('content', [{}])[0].get('text', '{}')
    
    # Parse the JSON response
    try:
        # Handle case where model wraps in markdown code block
        if content.strip().startswith('```'):
            content = content.strip().split('\n', 1)[1].rsplit('```', 1)[0]
        extracted = json.loads(content)
    except json.JSONDecodeError as e:
        print(f"[CDA] JSON parse error: {e}")
        print(f"[CDA] Raw content: {content[:200]}")
        extracted = {"medications": [], "allergies": [], "observations": [], "conditions": []}
    
    print(f"[CDA] Extracted: {len(extracted.get('medications',[]))} meds, "
          f"{len(extracted.get('allergies',[]))} allergies, "
          f"{len(extracted.get('observations',[]))} obs, "
          f"{len(extracted.get('conditions',[]))} conditions")
    
    return extracted


def build_fhir_medication_request(med, patient_id, patient_name, encounter_id):
    """Build a FHIR MedicationRequest resource from extracted medication data."""
    status_map = {
        "active": "active",
        "new": "active",
        "continued": "active",
        "changed": "active",
        "stopped": "stopped",
        "discontinued": "stopped"
    }
    
    resource = {
        "resourceType": "MedicationRequest",
        "status": status_map.get(med.get("status", "active"), "active"),
        "intent": "order",
        "medicationCodeableConcept": {
            "text": med.get("name", "Unknown medication")
        },
        "subject": {
            "reference": f"Patient/{patient_id}",
            "display": patient_name
        },
        "authoredOn": datetime.utcnow().strftime('%Y-%m-%dT%H:%M:%SZ')
    }
    
    # Add RxNorm coding if available
    if med.get("rxnorm_code"):
        resource["medicationCodeableConcept"]["coding"] = [{
            "system": "http://www.nlm.nih.gov/research/umls/rxnorm",
            "code": med["rxnorm_code"],
            "display": med.get("name", "")
        }]
    
    # Add dosage instruction
    dosage_text_parts = []
    if med.get("dose"):
        dosage_text_parts.append(med["dose"])
    if med.get("route"):
        dosage_text_parts.append(med["route"])
    if med.get("frequency"):
        dosage_text_parts.append(med["frequency"])
    
    if dosage_text_parts:
        resource["dosageInstruction"] = [{
            "text": " ".join(dosage_text_parts)
        }]
    
    # Link to encounter
    if encounter_id:
        resource["encounter"] = {"reference": f"Encounter/{encounter_id}"}
    
    return resource


def build_fhir_allergy_intolerance(allergy, patient_id, patient_name):
    """Build a FHIR AllergyIntolerance resource from extracted allergy data."""
    category_map = {
        "medication": "medication",
        "food": "food",
        "environment": "environment",
        "drug": "medication"
    }
    
    severity_map = {
        "mild": "mild",
        "moderate": "moderate",
        "severe": "severe"
    }
    
    resource = {
        "resourceType": "AllergyIntolerance",
        "clinicalStatus": {
            "coding": [{
                "system": "http://terminology.hl7.org/CodeSystem/allergyintolerance-clinical",
                "code": "active"
            }]
        },
        "verificationStatus": {
            "coding": [{
                "system": "http://terminology.hl7.org/CodeSystem/allergyintolerance-verification",
                "code": "confirmed"
            }]
        },
        "type": "allergy",
        "patient": {
            "reference": f"Patient/{patient_id}",
            "display": patient_name
        },
        "code": {
            "text": allergy.get("substance", "Unknown")
        },
        "recordedDate": datetime.utcnow().strftime('%Y-%m-%d')
    }
    
    # Add category
    cat = category_map.get(allergy.get("category", ""), "")
    if cat:
        resource["category"] = [cat]
    
    # Add reaction
    if allergy.get("reaction") or allergy.get("severity"):
        reaction = {}
        if allergy.get("reaction"):
            reaction["manifestation"] = [{
                "coding": [{"display": allergy["reaction"]}],
                "text": allergy["reaction"]
            }]
        if allergy.get("severity"):
            reaction["severity"] = severity_map.get(allergy["severity"], "mild")
        resource["reaction"] = [reaction]
    
    return resource


def build_fhir_observation(obs, patient_id, patient_name, encounter_id):
    """Build a FHIR Observation resource from extracted observation data."""
    resource = {
        "resourceType": "Observation",
        "status": "final",
        "code": {
            "text": obs.get("name", "Unknown")
        },
        "subject": {
            "reference": f"Patient/{patient_id}",
            "display": patient_name
        },
        "effectiveDateTime": datetime.utcnow().strftime('%Y-%m-%dT%H:%M:%SZ')
    }
    
    # Add LOINC coding if available
    if obs.get("loinc_code"):
        resource["code"]["coding"] = [{
            "system": "http://loinc.org",
            "code": obs["loinc_code"],
            "display": obs.get("name", "")
        }]
    
    # Add value
    if obs.get("value"):
        try:
            numeric_val = float(str(obs["value"]).replace(",", ""))
            resource["valueQuantity"] = {
                "value": numeric_val,
                "unit": obs.get("unit", ""),
                "system": "http://unitsofmeasure.org"
            }
        except (ValueError, TypeError):
            # Non-numeric value — store as CodeableConcept
            resource["valueCodeableConcept"] = {
                "text": str(obs["value"])
            }
    
    # Link to encounter
    if encounter_id:
        resource["encounter"] = {"reference": f"Encounter/{encounter_id}"}
    
    return resource


def build_fhir_condition(condition, patient_id, patient_name, encounter_id):
    """Build a FHIR Condition resource from extracted condition data."""
    status_map = {
        "active": "active",
        "resolved": "resolved",
        "recurring": "recurrence",
        "inactive": "inactive"
    }
    
    resource = {
        "resourceType": "Condition",
        "clinicalStatus": {
            "coding": [{
                "system": "http://terminology.hl7.org/CodeSystem/condition-clinical",
                "code": status_map.get(condition.get("status", "active"), "active")
            }]
        },
        "verificationStatus": {
            "coding": [{
                "system": "http://terminology.hl7.org/CodeSystem/condition-ver-status",
                "code": "confirmed"
            }]
        },
        "code": {
            "text": condition.get("name", "Unknown")
        },
        "subject": {
            "reference": f"Patient/{patient_id}",
            "display": patient_name
        },
        "recordedDate": datetime.utcnow().strftime('%Y-%m-%d')
    }
    
    # Add SNOMED coding if available
    if condition.get("snomed_code"):
        resource["code"]["coding"] = [{
            "system": "http://snomed.info/sct",
            "code": condition["snomed_code"],
            "display": condition.get("name", "")
        }]
    
    # Link to encounter
    if encounter_id:
        resource["encounter"] = {"reference": f"Encounter/{encounter_id}"}
    
    return resource


# =============================================================================
# CODING AGENT — Generates ICD-10 + CPT codes from SOAP note via Bedrock
# =============================================================================

CODING_PROMPT = """You are a medical coding specialist. Given a clinical SOAP note, extract all applicable ICD-10-CM diagnosis codes and CPT procedure codes.

RULES:
- Only code what is explicitly documented in the note
- Include the code, description, and confidence level (high/medium/low)
- For ICD-10-CM: use the most specific code supported by the documentation
- For CPT: include E/M codes based on encounter type and complexity
- Include source text from the note that supports each code

Return ONLY valid JSON in this exact structure (no markdown, no explanation):
{
    "medicalCodes": [
        {
            "type": "ICD10CM",
            "name": "E11.9",
            "description": "Type 2 diabetes mellitus without complications",
            "confidence": 0.95,
            "sourceText": "diabetes follow-up, improved compliance with Metformin"
        },
        {
            "type": "CPT",
            "name": "99214",
            "description": "Office/outpatient visit, established patient, moderate complexity",
            "confidence": 0.90,
            "sourceText": "Follow-up visit, medication management, vital signs reviewed"
        }
    ]
}

SOAP NOTE:
{soap_text}
"""


def generate_medical_codes_from_soap(soap_text):
    """
    Coding Agent: Generate ICD-10-CM and CPT codes from a SOAP note using Bedrock.
    Returns structured codes in the same format as HealthScribe's medicalCodes.json.
    """
    if not soap_text or len(soap_text.strip()) < 20:
        return {"medicalCodes": []}

    client = _get_bedrock_client()

    prompt = CODING_PROMPT.replace("{soap_text}", soap_text)

    body = json.dumps({
        "anthropic_version": "bedrock-2023-05-31",
        "max_tokens": 4096,
        "messages": [{"role": "user", "content": prompt}],
        "temperature": 0.1
    })

    response = client.invoke_model(
        modelId="us.anthropic.claude-sonnet-4-5-20250929-v1:0",
        body=body,
        contentType="application/json",
        accept="application/json"
    )

    result = json.loads(response['body'].read())
    content = result.get('content', [{}])[0].get('text', '{}')

    try:
        if content.strip().startswith('```'):
            content = content.strip().split('\n', 1)[1].rsplit('```', 1)[0]
        extracted = json.loads(content)
    except json.JSONDecodeError as e:
        print(f"[CODING-AGENT] JSON parse error: {e}")
        extracted = {"medicalCodes": []}

    codes = extracted.get("medicalCodes", [])
    print(f"[CODING-AGENT] Generated {len(codes)} codes from SOAP")
    return extracted
