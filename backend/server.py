# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
#!/usr/bin/env python3
"""
AWS Patient Insights Backend Proxy Server

This server acts as a proxy between the browser frontend and AWS Patient Insights API,
handling SigV4 authentication using your local AWS credentials.
"""

import json
import time
import sys
from flask import Flask, request, jsonify, send_from_directory
from flask_cors import CORS
import boto3
import os
from botocore.session import Session as BotocoreSession

from config import (
    AWS_PROFILE, AWS_REGION, SERVICE_NAME, SERVICE_ENDPOINT, DOMAIN_ID,
    HEALTHLAKE_DATASTORE_ID, S3_OUTPUT_BUCKET,
    STREAMING_OUTPUT_BUCKET, STREAMING_OUTPUT_REGION,
    SMS_AWS_PROFILE, SMS_REGION, SMS_ORIGINATION_NUMBER,
    DEMO_CACHE, CORS_ORIGINS,
    SERVER_HOST, SERVER_PORT, DEBUG,
    BEDROCK_MODEL_ID, BEDROCK_REGION, BEDROCK_MAX_TOKENS
)
from demo_mode import is_demo_request, get_cached_response, save_to_cache, DEMO_RECORD
import live_transcript
from patient_chart import get_patient_chart, fhir_writeback_with_extraction
from clinical_data_agent import generate_medical_codes_from_soap
from auth import init_auth

# Demo mode - load cached S3 data instead of running jobs
# Set via command line: python server.py --demo
DEMO_MODE = '--demo' in os.sys.argv

def _safe_error(e, context="request"):
    """Log full error server-side, return generic message to client."""
    import traceback
    print(f"[ERROR] {context}: {str(e)}", flush=True)
    traceback.print_exc()
    sys.stdout.flush()
    return jsonify({"success": False, "error": "An internal error occurred"}), 500

app = Flask(__name__, static_folder='../frontend')
CORS(app, origins=CORS_ORIGINS, expose_headers=["Authorization"])

# Initialize Cognito authentication (no-op if COGNITO_USER_POOL_ID not set)
init_auth(app)


# =============================================================================
# DEMO MODE API
# =============================================================================

@app.route('/api/demo/status', methods=['GET'])
def demo_status():
    """Check if demo mode is active for this request."""
    return jsonify({
        "demoMode": is_demo_request(),
        "recording": DEMO_RECORD
    })


# =============================================================================
# SERVE FRONTEND
# =============================================================================

@app.route('/')
def serve_index():
    """Serve the main UI."""
    return send_from_directory(app.static_folder, 'index.html')

@app.route('/<path:path>')
def serve_static(path):
    """Serve static files from frontend."""
    return send_from_directory(app.static_folder, path)

# Global client instance
_client = None

def get_client():
    """Get or create the Connect Health service client."""
    global _client
    if _client is None:
        # In ECS, use default credentials (IAM role). Locally, use profile if set.
        if AWS_PROFILE and AWS_PROFILE not in ("default", ""):
            session = boto3.Session(profile_name=AWS_PROFILE)
        else:
            session = boto3.Session()
        _client = session.client(
            service_name='connecthealth',
            region_name=AWS_REGION,
            endpoint_url=SERVICE_ENDPOINT
        )
    return _client


# =============================================================================
# HEALTH CHECK
# =============================================================================

@app.route('/api/health', methods=['GET'])
def health_check():
    """Health check endpoint."""
    return jsonify({"status": "ok", "service": "patient-insights-proxy"})


@app.route("/api/debug/client-methods", methods=["GET"])
def debug_client_methods():
    """Temporary debug: list available methods on the ConnectHealth client."""
    try:
        client = get_client()
        methods = [m for m in dir(client) if not m.startswith("_") and callable(getattr(client, m))]
        api_methods = [m for m in methods if not m.startswith("get_") or "get_paginator" not in m]
        return jsonify({"success": True, "methods": api_methods, "service": str(type(client))})
    except Exception as e:
        return jsonify({"success": False, "error": str(e)})



@app.route("/api/active-session", methods=["GET"])
def get_active_session():
    """Return the most recent bridge session ID from CloudWatch logs."""
    import re as _re
    try:
        client = boto3.client("logs", region_name="us-east-1")
        import time as _time
        start_ms = int((_time.time() - 300) * 1000)  # Last 5 minutes
        resp = client.filter_log_events(
            logGroupName="/ecs/connect-health-bridge-dev",
            filterPattern='"Starting bridge"',
            startTime=start_ms,
            limit=5
        )
        events = resp.get("events", [])
        if events:
            last_event = events[-1]["message"]
            match = _re.search(r"contact=([a-f0-9\-]+)", last_event)
            if match:
                return jsonify({"success": True, "sessionId": match.group(1)})
        return jsonify({"success": True, "sessionId": None})
    except Exception as e:
        return jsonify({"success": False, "error": str(e)})


@app.route('/api/patients', methods=['GET'])
def list_patients():
    """List all patients from HealthLake."""
    # Demo mode: return cached patient list
    if is_demo_request():
        cached = get_cached_response('patients')
        if cached:
            return jsonify(cached)

    try:
        # In ECS, use default credentials (IAM role). Locally, use profile if set.
        if AWS_PROFILE and AWS_PROFILE not in ("default", ""):
            session = boto3.Session(profile_name=AWS_PROFILE)
        else:
            session = boto3.Session()
        credentials = session.get_credentials()
        
        # Build HealthLake URL
        url = f"https://healthlake.{AWS_REGION}.amazonaws.com/datastore/{HEALTHLAKE_DATASTORE_ID}/r4/Patient?_count=100"
        
        # Sign the request
        from botocore.auth import SigV4Auth
        from botocore.awsrequest import AWSRequest
        import requests
        
        headers = {'Content-Type': 'application/fhir+json', 'Accept': 'application/fhir+json'}
        request = AWSRequest(method='GET', url=url, headers=headers)
        SigV4Auth(credentials, 'healthlake', AWS_REGION).add_auth(request)
        
        response = requests.get(url, headers=dict(request.headers), timeout=30)  # nosemgrep: use-raise-for-status — non-200 responses are handled by bundle.get() returning empty, surfaced as empty patient list
        bundle = response.json()
        
        patients = []
        for entry in bundle.get('entry', []):
            patient = entry['resource']
            name = patient.get('name', [{}])[0]
            # Join ALL given names so a middle name is not dropped (a patient
            # stored as given ["Mary","Anne"] must not render as "Mary Johnson",
            # which would then fail name lookups in the Care Intelligence tools).
            given = ' '.join(name.get('given', [])) if name.get('given') else ''
            family = name.get('family', '')

            # Demographics: home address + primary phone
            addr = (patient.get('address') or [{}])[0]
            phone = ''
            for t in (patient.get('telecom') or []):
                if t.get('system') == 'phone' and t.get('value'):
                    phone = t['value']
                    break
            # Prefer a real MR identifier when present, else derive from the id
            mrn = patient['id'][:8]
            for ident in (patient.get('identifier') or []):
                coding = (ident.get('type', {}).get('coding') or [{}])[0]
                if coding.get('code') == 'MR' and ident.get('value'):
                    mrn = ident['value']
                    break
            
            # Calculate age from birthDate
            birth_date = patient.get('birthDate', '')
            age = ''
            if birth_date:
                from datetime import datetime
                try:
                    birth = datetime.strptime(birth_date, '%Y-%m-%d')
                    age = str((datetime.now() - birth).days // 365) + ' yrs'
                except:
                    age = ''
            
            patients.append({
                'id': patient['id'],
                'name': f"{given} {family}".strip(),
                'given': given,
                'family': family,
                'gender': patient.get('gender', '').capitalize(),
                'birthDate': birth_date,
                'age': age,
                'mrn': mrn,
                'zip': addr.get('postalCode', ''),
                'city': addr.get('city', ''),
                'state': addr.get('state', ''),
                'phone': phone
            })
        
        return jsonify({
            "success": True,
            "patients": patients,
            "count": len(patients)
        })
        
    except Exception as e:
        return _safe_error(e, "list_patients")


@app.route('/api/echo', methods=['POST'])
def echo():
    """Test connectivity to AWS Patient Insights API."""
    try:
        client = get_client()
        data = request.get_json() or {}
        message = data.get('message', 'Hello from Patient Insights proxy!')
        
        response = client.echo(string=message)
        return jsonify({
            "success": True,
            "response": response.get('string')
        })
    except Exception as e:
        return _safe_error(e, "echo")


# =============================================================================
# GENERATE MEDICAL CODES API
# =============================================================================





@app.route('/api/admin/update-harness-gateway', methods=['POST'])
def update_harness_gateway():
    """Admin: Full migration — create OAuth provider + update harness gateway."""
    try:
        session_obj = boto3.Session()
        control = session_obj.client('bedrock-agentcore-control', region_name=AWS_REGION)
        
        harness_id = 'care-intel-harness-EXAMPLE'
        new_gateway_arn = 'arn:aws:bedrock-agentcore:us-east-1:123456789012:gateway/connect-health-fhir-gateway-EXAMPLE'
        
        # Step 1: Get Cognito client secret
        cognito = session_obj.client('cognito-idp', region_name=AWS_REGION)
        client_detail = cognito.describe_user_pool_client(
            UserPoolId='us-east-1_EXAMPLE0',
            ClientId='exampleclientid0123456789a'
        )
        client_secret = client_detail['UserPoolClient']['ClientSecret']
        
        # Step 2: Create OAuth2 credential provider
        provider_arn = None
        try:
            resp = control.create_oauth2_credential_provider(
                name='cognito-gateway-oauth',
                credentialProviderVendor='CustomOauth2',
                oauth2ProviderConfigInput={
                    'customOauth2ProviderConfig': {
                        'oauthDiscovery': {
                            'discoveryUrl': 'https://cognito-idp.us-east-1.amazonaws.com/us-east-1_EXAMPLE0/.well-known/openid-configuration'
                        },
                        'clientId': 'exampleclientid0123456789a',
                        'clientSecret': client_secret
                    }
                }
            )
            provider_arn = resp.get('credentialProviderArn', resp.get('providerArn', ''))
            provider_status = f'CREATED: {provider_arn}'
        except Exception as e:
            if 'already exists' in str(e).lower() or 'Conflict' in str(e):
                # Get existing
                try:
                    existing = control.get_oauth2_credential_provider(name='cognito-gateway-oauth')
                    provider_arn = existing.get('credentialProviderArn', existing.get('providerArn', ''))
                    provider_status = f'EXISTS: {provider_arn}'
                except:
                    provider_status = f'EXISTS (ARN unknown)'
            else:
                provider_status = f'ERROR: {str(e)[:200]}'
        
        # Step 3: Update harness with gateway + OAuth outbound auth
        tools_config = [{
            'type': 'agentcore_gateway',
            'name': 'gateway',
            'config': {
                'agentCoreGateway': {
                    'gatewayArn': new_gateway_arn
                }
            }
        }]
        
        # Add outbound auth if we have provider ARN
        if provider_arn:
            tools_config[0]['config']['agentCoreGateway']['outboundAuth'] = {
                'oauth': {
                    'providerArn': provider_arn,
                    'scopes': ['agentcore-gateway/invoke'],
                    'grantType': 'CLIENT_CREDENTIALS'
                }
            }
        
        resp = control.update_harness(
            harnessId=harness_id,
            model={
                'bedrockModelConfig': {
                    'modelId': 'us.anthropic.claude-opus-4-6-v1:0'
                }
            },
            tools=tools_config
        )
        
        return jsonify({
            'success': True,
            'providerStatus': provider_status,
            'harnessStatus': resp.get('status', 'updated'),
            'harnessId': harness_id,
            'newGateway': new_gateway_arn,
            'providerArn': provider_arn
        })
    except Exception as e:
        import traceback
        return jsonify({'success': False, 'error': str(e), 'trace': traceback.format_exc()[:500]}), 500


@app.route('/api/test-harness', methods=['POST'])
def test_harness():
    """Test InvokeHarness API — temporary endpoint for validation."""
    try:
        import uuid
        data = request.get_json(force=True)
        message = data.get('message', 'How many patients completed appointments this week?')
        
        # Use the bedrock-agentcore dataplane client
        if AWS_PROFILE and AWS_PROFILE not in ("default", ""):
            session = boto3.Session(profile_name=AWS_PROFILE)
        else:
            session = boto3.Session()
        
        dataplane = session.client('bedrock-agentcore', region_name=AWS_REGION)
        
        harness_arn = 'arn:aws:bedrock-agentcore:us-east-1:123456789012:harness/care-intel-harness-EXAMPLE'
        session_id = str(uuid.uuid4()) + '-' + str(uuid.uuid4())[:8]
        
        resp = dataplane.invoke_harness(
            harnessArn=harness_arn,
            runtimeSessionId=session_id,
            messages=[
                {
                    'role': 'user',
                    'content': [{'text': message}]
                }
            ]
        )
        
        # Read streaming response
        stream = resp.get('stream', resp.get('body', None))
        full_text = ''
        events = []
        
        if hasattr(stream, '__iter__'):
            for event in stream:
                events.append(str(event)[:200])
                if isinstance(event, dict):
                    # Text deltas from contentBlockDelta
                    delta = event.get('contentBlockDelta', {}).get('delta', {})
                    if 'text' in delta:
                        full_text += delta['text']
                    # Also check top-level text
                    if 'text' in event:
                        full_text += event['text']
        
        return jsonify({
            'success': True,
            'sessionId': session_id,
            'text': full_text,
            'eventCount': len(events),
            'events': events[:10]
        })
        
    except Exception as e:
        import traceback
        return jsonify({
            'success': False,
            'error': str(e),
            'trace': traceback.format_exc()[:500]
        }), 500


@app.route('/api/medical-codes', methods=['POST'])
def generate_medical_codes():
    """
    Native Medical Coding Agent — starts an async medical coding job.
    Uses Connect Health StartMedicalCodingJob / GetMedicalCodingJob (gated preview).
    
    Request body: { "text": "clinical notes...", "patientContext": {...}, "encounterContext": {...} }
    Returns: { "success": true, "medicalCodes": [...] }
    """
    try:
        data = request.get_json()
        
        if not data or 'text' not in data:
            return jsonify({"success": False, "error": "Missing 'text' field"}), 400

        # Optional — if the caller includes the streaming sessionId, successfully
        # generated codes get persisted to S3 (see _persist_medical_codes) so
        # other UI surfaces reading via GET .../medical-codes or .../outputs can
        # find them later, instead of the codes existing only in this one response.
        streaming_session_id = data.get('sessionId')


        client = get_client()
        
        # Build request params
        import uuid
        request_params = {
            'domainId': DOMAIN_ID,
            'text': data['text'],
            'clientToken': str(uuid.uuid4()),
            'outputDataConfig': {
                's3OutputPath': S3_OUTPUT_BUCKET + 'medical-coding/'
            }
        }
        
        if 'patientContext' in data:
            pc = data['patientContext']
            pc.pop('dateOfBirth', None)  # Remove if present — validation issues
            if pc:
                request_params['patientContext'] = pc
        
        if 'encounterContext' in data:
            request_params['encounterContext'] = data['encounterContext']
        
        # Start the async job
        print(f"[CODING] Starting medical coding job...")
        start_response = client.start_medical_coding_job(**request_params)
        job_id = start_response.get('jobId', '')
        print(f"[CODING] Job started: {job_id}")
        
        # Poll for completion (max 60 seconds)
        import time
        for attempt in range(20):
            time.sleep(3)
            status_response = client.get_medical_coding_job(
                domainId=DOMAIN_ID,
                jobId=job_id
            )
            status = status_response.get('status', status_response.get('jobStatus', ''))
            print(f"[CODING] Poll {attempt+1}: {status}")
            
            if status == 'SUCCEEDED':
                # Native API returns the results location at medicalCodingOutput.uri
                # (there is no inline 'medicalCodes' field on this response — must fetch from S3)
                output_uri = status_response.get('medicalCodingOutput', {}).get('uri', '')
                
                # Fetch the results JSON from S3
                if output_uri:
                    try:
                        if AWS_PROFILE and AWS_PROFILE not in ("default", ""):
                            s3_session = boto3.Session(profile_name=AWS_PROFILE)
                        else:
                            s3_session = boto3.Session()
                        s3 = s3_session.client('s3', region_name=AWS_REGION)
                        
                        # Parse S3 URI
                        if output_uri.startswith('s3://'):
                            parts = output_uri[5:].split('/', 1)
                            bucket = parts[0]
                            key = parts[1] if len(parts) > 1 else ''
                        else:
                            bucket = STREAMING_OUTPUT_BUCKET
                            key = output_uri
                        
                        # List objects at the output path to find the results file
                        response = s3.list_objects_v2(Bucket=bucket, Prefix=key, MaxKeys=10)
                        for obj in response.get('Contents', []):
                            if obj['Key'].endswith('.json'):
                                obj_response = s3.get_object(Bucket=bucket, Key=obj['Key'])
                                codes_data = json.loads(obj_response['Body'].read().decode())
                                codes = codes_data.get('medicalCodes', codes_data)
                                _persist_medical_codes(streaming_session_id, codes)
                                return jsonify({"success": True, "medicalCodes": codes})
                        
                        return jsonify({"success": True, "medicalCodes": [], "note": "Job succeeded but no output found"})
                    except Exception as s3_err:
                        print(f"[CODING] S3 fetch error: {s3_err}")
                        return jsonify({"success": True, "medicalCodes": [], "note": f"S3 error: {str(s3_err)}"})
                
                return jsonify({"success": True, "medicalCodes": [], "note": "Job succeeded, no codes in response"})
            
            elif status in ('FAILED', 'STOPPED'):
                error_msg = status_response.get('failureReason', 'Unknown failure')
                print(f"[CODING] Job failed: {error_msg}")
                return jsonify({"success": False, "error": f"Medical coding job failed: {error_msg}"}), 500
        
        # Timeout
        return jsonify({"success": False, "error": "Medical coding job timed out (60s)"}), 504
        
    except Exception as e:
        print(f"[ERROR] generate_medical_codes: {e}")
        import traceback
        traceback.print_exc()
        return _safe_error(e, "generate_medical_codes")


@app.route('/api/patient-insights/start', methods=['POST'])
def start_patient_insights_job():
    """
    Start a patient insights job.
    
    If demo mode or patient has cached S3 data, returns immediately with cached data.
    Otherwise starts a real job.
    """
    try:
        data = request.get_json()
        
        if not data or 'patientId' not in data:
            return jsonify({"success": False, "error": "Missing 'patientId' field"}), 400
        
        patient_id = data['patientId']
        
        # Demo mode: return cached patient insights
        if is_demo_request():
            cached = get_cached_response('patient_insights', patient_id[:8])
            if cached:
                print(f"[DEMO] Returning cached patient insights for {patient_id[:8]}")
                return jsonify({
                    "success": True,
                    "jobArn": f"demo-job-{patient_id[:8]}",
                    "creationTime": str(time.time()),
                    "cached": True,
                    "demoData": cached
                })
        
        # Check cache first (always, not just in demo mode) - for patients with pre-computed insights
        if patient_id in DEMO_CACHE:
            print(f"[CACHE HIT] Using cached S3 data for patient {patient_id[:8]}...")
            return jsonify({
                "success": True,
                "jobArn": f"cached-job-{patient_id[:8]}",
                "creationTime": str(time.time()),
                "cached": True,
                "cachedUri": DEMO_CACHE[patient_id]
            })
        
        client = get_client()
        
        # Build HealthLake endpoint
        healthlake_endpoint = f"https://healthlake.{AWS_REGION}.amazonaws.com/datastore/{HEALTHLAKE_DATASTORE_ID}/r4/"
        
        # Build request
        response = client.start_patient_insights_job(
            domainId=DOMAIN_ID,
            patientContext={
                "patientId": patient_id
            },
            insightsContext={
                "insightsType": "PRE_VISIT"
            },
            encounterContext={
                "encounterReason": data.get('encounterReason', 'Follow-up visit')
            },
            userContext={
                "role": "CLINICIAN",
                "userId": (getattr(request, 'cognito_user', None) or {}).get('sub')
                    or (getattr(request, 'cognito_user', None) or {}).get('email')
                    or 'local-dev-provider'
            },
            inputDataConfig={
                "fhirServer": {
                    "fhirEndpoint": healthlake_endpoint
                }
            },
            outputDataConfig={
                "s3OutputPath": S3_OUTPUT_BUCKET
            }
        )
        
        return jsonify({
            "success": True,
            "jobArn": response.get('jobArn', ''),
            "jobId": response.get('jobId', ''),
            "creationTime": str(response.get('creationTime', ''))
        })
        
    except Exception as e:
        return _safe_error(e, "start_patient_insights_job")


@app.route('/api/patient-insights/job/<path:job_arn>', methods=['GET'])
def get_patient_insights_job(job_arn):
    """Get the status and results of a patient insights job."""
    try:
        # CACHED JOB: Return immediate success with cached URI
        if job_arn.startswith('cached-job-'):
            # Extract patient ID prefix from cached job ARN
            patient_prefix = job_arn.replace('cached-job-', '')
            for patient_id, uri in DEMO_CACHE.items():
                if patient_id.startswith(patient_prefix):
                    print(f"[CACHE] Returning cached S3 URI for {patient_prefix}")
                    return jsonify({
                        "success": True,
                        "jobArn": job_arn,
                        "jobStatus": "SUCCEEDED",
                        "creationTime": str(time.time()),
                        "updatedTime": str(time.time()),
                        "statusDetails": "Cached data",
                        "insightsOutput": {"uri": uri}
                    })
        
        # Legacy demo-job support
        if job_arn.startswith('demo-job-'):
            patient_prefix = job_arn.replace('demo-job-', '')
            for patient_id, uri in DEMO_CACHE.items():
                if patient_id.startswith(patient_prefix):
                    return jsonify({
                        "success": True,
                        "jobArn": job_arn,
                        "jobStatus": "SUCCEEDED",
                        "creationTime": str(time.time()),
                        "updatedTime": str(time.time()),
                        "statusDetails": "Demo mode - cached data",
                        "insightsOutput": {"uri": uri}
                    })
        
        client = get_client()
        
        # Extract jobId from ARN if a full ARN was passed
        # ARN format: arn:aws:health-agent:<region>:<account-id>:domain/<domain-id>/patient-insights-job/<job-id>
        actual_job_id = job_arn
        if 'patient-insights-job/' in job_arn:
            actual_job_id = job_arn.split('patient-insights-job/')[-1]
        
        response = client.get_patient_insights_job(domainId=DOMAIN_ID, jobId=actual_job_id)
        
        result = {
            "success": True,
            "jobArn": response.get('jobArn', ''),
            "jobId": response.get('jobId', ''),
            "jobStatus": response['jobStatus'],
            "creationTime": str(response.get('creationTime', '')),
            "updatedTime": str(response.get('updatedTime', '')),
            "statusDetails": response.get('statusDetails', '')
        }
        
        # Include output URI if job succeeded
        if response['jobStatus'] == 'SUCCEEDED' and 'insightsOutput' in response:
            result['insightsOutput'] = {
                "uri": response['insightsOutput']['uri']
            }
        
        return jsonify(result)
        
    except Exception as e:
        return _safe_error(e, "get_patient_insights_job")


@app.route('/api/patient-insights/run', methods=['POST'])
def run_patient_insights():
    """
    Convenience endpoint: Start job, poll until complete, return results.
    
    This combines start + polling into a single call for simpler frontend integration.
    Note: This can take 30-60 seconds to complete.
    
    Request body: Same as /api/patient-insights/start
    """
    try:
        data = request.get_json()
        
        if not data or 'patientId' not in data:
            return jsonify({"success": False, "error": "Missing 'patientId' field"}), 400
        
        patient_id = data['patientId']
        
        # Demo mode: return cached patient insights
        if is_demo_request():
            cached = get_cached_response('patient_insights', patient_id[:8])
            if cached:
                print(f"[DEMO] Returning cached patient insights for {patient_id[:8]}")
                return jsonify({
                    "success": True,
                    "jobId": f"demo-job-{patient_id[:8]}",
                    "jobStatus": "SUCCEEDED",
                    "insightsOutput": cached
                })
        
        client = get_client()
        
        # Build HealthLake endpoint
        healthlake_endpoint = f"https://healthlake.{AWS_REGION}.amazonaws.com/datastore/{HEALTHLAKE_DATASTORE_ID}/r4/"
        
        # Start the job
        start_response = client.start_patient_insights_job(
            domainId=DOMAIN_ID,
            patientContext={
                "patientId": data['patientId']
            },
            insightsContext={
                "insightsType": "PRE_VISIT"
            },
            encounterContext={
                "encounterReason": data.get('encounterReason', 'Follow-up visit')
            },
            userContext={
                "role": "CLINICIAN",
                "userId": (getattr(request, 'cognito_user', None) or {}).get('sub')
                    or (getattr(request, 'cognito_user', None) or {}).get('email')
                    or 'local-dev-provider'
            },
            inputDataConfig={
                "fhirServer": {
                    "fhirEndpoint": healthlake_endpoint
                }
            },
            outputDataConfig={
                "s3OutputPath": S3_OUTPUT_BUCKET
            }
        )
        
        job_id = start_response.get('jobId', start_response.get('jobArn', ''))
        
        # Poll for completion (max 5 minutes)
        max_attempts = 60
        poll_interval = 5
        
        for attempt in range(max_attempts):
            time.sleep(poll_interval)  # nosemgrep: arbitrary-sleep — intentional polling for async job completion
            
            get_response = client.get_patient_insights_job(domainId=DOMAIN_ID, jobId=job_id)
            status = get_response['jobStatus']
            
            if status == 'SUCCEEDED':
                # Fetch the output from S3
                output_uri = get_response['insightsOutput']['uri']
                output_data = fetch_s3_output(output_uri)
                
                return jsonify({
                    "success": True,
                    "jobId": job_id,
                    "jobStatus": status,
                    "insightsOutput": output_data
                })
            
            elif status == 'FAILED':
                return jsonify({
                    "success": False,
                    "jobId": job_id,
                    "jobStatus": status,
                    "error": get_response.get('statusDetails', 'Job failed')
                }), 500
        
        # Timeout
        return jsonify({
            "success": False,
            "jobId": job_id,
            "error": "Job timed out after 5 minutes"
        }), 504
        
    except Exception as e:
        return _safe_error(e, "run_patient_insights")


def fetch_s3_output(s3_uri):
    """Fetch and parse the insights output from S3."""
    try:
        # Parse S3 URI: s3://bucket/key
        parts = s3_uri.replace("s3://", "").split("/", 1)
        bucket = parts[0]
        key = parts[1] if len(parts) > 1 else ""

        # Validate bucket is one we actually use (derived from config)
        allowed_buckets = {
            S3_OUTPUT_BUCKET.replace("s3://", "").split("/")[0],  # e.g. my-insights-bucket
            STREAMING_OUTPUT_BUCKET,                               # e.g. my-streaming-bucket
        }
        if bucket not in allowed_buckets:
            return {"error": "Access denied: bucket not allowed"}
        
        # In ECS, use default credentials (IAM role). Locally, use profile if set.
        if AWS_PROFILE and AWS_PROFILE not in ("default", ""):
            session = boto3.Session(profile_name=AWS_PROFILE)
        else:
            session = boto3.Session()
        s3 = session.client('s3', region_name=AWS_REGION)
        
        response = s3.get_object(Bucket=bucket, Key=key)
        content = response['Body'].read().decode('utf-8')
        
        return json.loads(content)
    except Exception as e:
        return {"error": f"Failed to fetch S3 output: {str(e)}", "uri": s3_uri}


@app.route('/api/patient-insights/output/<path:s3_uri>', methods=['GET'])
def get_patient_insights_output(s3_uri):
    """Fetch the insights output from S3."""
    # Demo mode: return cached patient insights summary from local file
    if is_demo_request():
        from urllib.parse import unquote
        decoded_uri = unquote(s3_uri)
        # Extract patient ID prefix from the S3 URI path
        # URI format: s3://<bucket>/insights-output/{jobId}/{patientId}/summary.json
        for pid in DEMO_CACHE:
            if pid[:8] in decoded_uri or pid in decoded_uri:
                cached = get_cached_response('patient_insights', pid[:8])
                if cached:
                    return jsonify({"success": True, "data": cached})
                break

    try:
        # The URI comes URL-encoded, decode it
        from urllib.parse import unquote
        decoded_uri = unquote(s3_uri)
        
        # Validate S3 key matches expected pattern: insights-output/{job-id}/{patient-id}/summary.json
        import re
        parts = decoded_uri.replace("s3://", "").split("/", 1)
        key = parts[1] if len(parts) > 1 else ""
        if not re.match(r'^insights-output/[a-zA-Z0-9\-]+/[a-zA-Z0-9\-]+/summary\.json$', key):
            return jsonify({"success": False, "error": "Invalid S3 path"}), 400
        
        output_data = fetch_s3_output(decoded_uri)
        
        if 'error' in output_data:
            return jsonify({"success": False, "error": output_data['error']}), 500
        
        return jsonify({
            "success": True,
            "data": output_data
        })
        
    except Exception as e:
        return _safe_error(e, "get_patient_insights_output")


# =============================================================================
# FHIR RESOURCE LOOKUP API
# =============================================================================

@app.route('/api/fhir/<resource_type>/<resource_id>', methods=['GET'])
def get_fhir_resource(resource_type, resource_id):
    """Fetch a FHIR resource from HealthLake and return key metadata."""
    # Demo mode: return a minimal placeholder so the UI doesn't break
    if is_demo_request():
        return jsonify({
            "success": True,
            "meta": {
                "resourceType": resource_type,
                "id": resource_id[:16] + "...",
                "name": "Demo " + resource_type,
                "status": "final",
                "date": "2025-10-21T00:00:00Z"
            }
        })

    # SSRF mitigation: allowlist of valid FHIR resource types
    ALLOWED_RESOURCE_TYPES = {
        "Observation", "Patient", "Encounter", "Procedure",
        "MedicationRequest", "Condition", "DiagnosticReport",
        "Immunization", "AllergyIntolerance", "DocumentReference"
    }
    if resource_type not in ALLOWED_RESOURCE_TYPES:
        return jsonify({"success": False, "error": "Invalid resource type"}), 400

    # SSRF mitigation: validate resource_id is alphanumeric/hyphens only (no path traversal)
    import re
    if not re.match(r'^[a-zA-Z0-9\-]+$', resource_id):
        return jsonify({"success": False, "error": "Invalid resource ID"}), 400

    # SSRF mitigation: host is a fixed HealthLake endpoint, not user-controlled.
    # Only resource_type (allowlisted above) and resource_id (regex-validated above)
    # are interpolated into the path. The scheme is always HTTPS.
    HEALTHLAKE_BASE_URL = f"https://healthlake.{AWS_REGION}.amazonaws.com/datastore/{HEALTHLAKE_DATASTORE_ID}/r4"

    try:
        if AWS_PROFILE and AWS_PROFILE not in ("default", ""):
            session = boto3.Session(profile_name=AWS_PROFILE)
        else:
            session = boto3.Session()
        credentials = session.get_credentials()

        url = f"{HEALTHLAKE_BASE_URL}/{resource_type}/{resource_id}"

        from botocore.auth import SigV4Auth
        from botocore.awsrequest import AWSRequest
        import requests as req_lib

        headers = {'Content-Type': 'application/fhir+json', 'Accept': 'application/fhir+json'}
        aws_req = AWSRequest(method='GET', url=url, headers=headers)
        SigV4Auth(credentials, 'healthlake', AWS_REGION).add_auth(aws_req)

        response = req_lib.get(url, headers=dict(aws_req.headers), timeout=15)  # nosemgrep: ssrf-requests — URL host is fixed HealthLake endpoint, resource_type is allowlisted, resource_id is regex-validated
        if response.status_code != 200:
            return jsonify({"success": False, "error": f"HealthLake returned {response.status_code}"}), response.status_code

        resource = response.json()

        # Extract useful metadata based on resource type
        meta = {
            "resourceType": resource.get("resourceType"),
            "id": resource.get("id", "")[:16] + "...",
            "status": resource.get("status"),
            "date": resource.get("effectiveDateTime") or resource.get("authoredOn") or resource.get("date"),
        }

        # Observation: test name, value, unit
        if resource_type == "Observation":
            code = resource.get("code", {})
            meta["name"] = code.get("text") or (code.get("coding", [{}])[0].get("display") if code.get("coding") else None) or "Unknown"
            vq = resource.get("valueQuantity", {})
            if vq:
                meta["value"] = vq.get("value")
                meta["unit"] = vq.get("unit", "")
            cat = resource.get("category", [{}])[0].get("coding", [{}])[0].get("code", "")
            meta["category"] = cat

        # MedicationRequest
        elif resource_type == "MedicationRequest":
            med = resource.get("medicationCodeableConcept", {})
            meta["name"] = med.get("text") or (med.get("coding", [{}])[0].get("display") if med.get("coding") else None) or "Unknown"
            # NDC code
            if med.get("coding"):
                meta["code"] = med["coding"][0].get("code", "")
                meta["codeSystem"] = med["coding"][0].get("system", "")
            dosage = resource.get("dosageInstruction", [{}])
            if dosage and isinstance(dosage, list) and len(dosage) > 0:
                meta["dosage"] = dosage[0].get("text", "")
                route = dosage[0].get("route", {})
                if route.get("coding"):
                    meta["route"] = route["coding"][0].get("code", "")
            # Dispense details
            dispense = resource.get("dispenseRequest", {})
            if dispense.get("quantity", {}).get("value") is not None:
                meta["quantity"] = dispense["quantity"]["value"]
            if "numberOfRepeatsAllowed" in dispense:
                meta["refills"] = dispense["numberOfRepeatsAllowed"]

        # Condition
        elif resource_type == "Condition":
            code = resource.get("code", {})
            meta["name"] = code.get("text") or (code.get("coding", [{}])[0].get("display") if code.get("coding") else None) or "Unknown"
            if code.get("coding"):
                meta["code"] = code["coding"][0].get("code", "")
                meta["codeSystem"] = code["coding"][0].get("system", "")
            meta["clinicalStatus"] = resource.get("clinicalStatus", {}).get("coding", [{}])[0].get("code", "")
            # Onset date
            onset = resource.get("onsetDateTime") or resource.get("onsetPeriod", {}).get("start")
            if onset:
                meta["onsetDate"] = onset

        # DiagnosticReport
        elif resource_type == "DiagnosticReport":
            code = resource.get("code", {})
            meta["name"] = code.get("text") or (code.get("coding", [{}])[0].get("display") if code.get("coding") else None) or "Unknown"

        # Encounter
        elif resource_type == "Encounter":
            etype = resource.get("type", [{}])
            if etype and isinstance(etype, list):
                meta["name"] = etype[0].get("text") or (etype[0].get("coding", [{}])[0].get("display") if etype[0].get("coding") else None) or "Visit"
            period = resource.get("period", {})
            meta["date"] = period.get("start", meta.get("date"))
            # Visit type from class code (AMB=Ambulatory, IMP=Inpatient, EMER=Emergency)
            enc_class = resource.get("class", {})
            class_code = enc_class.get("code", "")
            CLASS_MAP = {"AMB": "Ambulatory", "IMP": "Inpatient", "EMER": "Emergency", "HH": "Home Health", "VR": "Virtual"}
            meta["visitType"] = CLASS_MAP.get(class_code, class_code)
            meta["classCode"] = class_code

        # Patient
        elif resource_type == "Patient":
            name = resource.get("name", [{}])[0]
            given = name.get("given", [""])[0] if name.get("given") else ""
            family = name.get("family", "")
            meta["name"] = f"{given} {family}".strip()
            meta["gender"] = resource.get("gender")
            meta["birthDate"] = resource.get("birthDate")
            ms = resource.get("maritalStatus", {})
            if ms.get("coding"):
                ms_code = ms["coding"][0].get("code", "")
                MS_MAP = {"S": "Single", "M": "Married", "D": "Divorced", "W": "Widowed", "A": "Annulled", "L": "Legally Separated", "UNK": "Unknown"}
                meta["maritalStatus"] = MS_MAP.get(ms_code, ms_code)

        # DocumentReference
        elif resource_type == "DocumentReference":
            doc_type = resource.get("type", {})
            meta["name"] = doc_type.get("text") or (doc_type.get("coding", [{}])[0].get("code") if doc_type.get("coding") else None) or "Clinical Note"
            # Check if content has plaintext
            for c in resource.get("content", []):
                att = c.get("attachment", {})
                if att.get("contentType") == "text/plain" and att.get("data"):
                    meta["hasPlaintext"] = True
                    break

        # Return full resource if requested
        if request.args.get('full') == 'true':
            return jsonify({"success": True, "meta": meta, "resource": resource})

        return jsonify({"success": True, "meta": meta})

    except Exception as e:
        return _safe_error(e, "get_fhir_resource")


# =============================================================================
# STREAMING SESSION OUTPUT APIs
# =============================================================================

# Streaming output config imported from config.py

def _find_clinical_notes_prefix(s3, session_id):
    """
    Discover the S3 prefix for clinical notes.
    Handles both old and new path structures:
      Old: {sessionId}/listening-session/{sessionId}/post-stream-action/clinical-notes/
      New: {sessionId}/health-agent-listening-session/.../post-stream-action/clinical-notes/
    """
    try:
        paginator = s3.get_paginator('list_objects_v2')
        for page in paginator.paginate(Bucket=STREAMING_OUTPUT_BUCKET, Prefix=f"{session_id}/", Delimiter=''):
            for obj in page.get('Contents', []):
                key = obj['Key']
                if key.endswith('/clinical-notes/clinicalDoc.json'):
                    return key.rsplit('clinicalDoc.json', 1)[0]
    except Exception as e:
        print(f"[S3] Error discovering prefix for {session_id}: {e}")
    return None


def _persist_medical_codes(session_id, codes):
    """
    Write successfully-generated medical codes to the SAME S3 location that
    get_streaming_medical_codes() and get_streaming_session_outputs() already
    read from (medical-codes/medicalCodes.json under the session's post-stream
    prefix). Without this, codes generated by /api/medical-codes existed only
    inside that one HTTP response — never persisted anywhere — so every other
    UI surface (previsit iframe, page reloads, retry polling) that reads via
    GET .../medical-codes or GET .../outputs would always find nothing, even
    though the coding job itself succeeded. Confirmed via live testing July 10
    2026: jobs reached SUCCEEDED and /api/medical-codes returned 200 with real
    codes multiple times, but GET .../outputs kept showing medicalCodes: null.

    Defensive by design: session_id may be missing (older/unknown callers) or
    the S3 prefix may not exist yet (SOAP note not finished). Either case is
    silently skipped — this must never break the actual response to the caller
    that already has its real codes in hand.
    """
    if not session_id:
        return
    try:
        if AWS_PROFILE and AWS_PROFILE not in ("default", ""):
            session = boto3.Session(profile_name=AWS_PROFILE)
        else:
            session = boto3.Session()
        s3 = session.client('s3', region_name=STREAMING_OUTPUT_REGION)

        base_prefix = _find_clinical_notes_prefix(s3, session_id)
        if not base_prefix:
            print(f"[CODING-PERSIST] No clinical-notes prefix yet for session {session_id} — skipping persist")
            return

        post_stream_prefix = base_prefix.rsplit('clinical-notes/', 1)[0]
        s3_key = post_stream_prefix + "medical-codes/medicalCodes.json"
        s3.put_object(
            Bucket=STREAMING_OUTPUT_BUCKET,
            Key=s3_key,
            Body=json.dumps({"medicalCodes": codes}).encode('utf-8'),
            ContentType='application/json'
        )
        print(f"[CODING-PERSIST] Wrote {len(codes) if isinstance(codes, list) else '?'} codes to s3://{STREAMING_OUTPUT_BUCKET}/{s3_key}")
    except Exception as e:
        print(f"[CODING-PERSIST] Failed to persist codes for session {session_id}: {e}")

@app.route('/api/streaming/session/<session_id>/outputs', methods=['GET'])
def get_streaming_session_outputs(session_id):
    """
    Fetch all outputs from a streaming session.
    
    Returns:
    - clinicalDoc: SOAP notes
    - medicalCodes: ICD10 + CPT codes
    - afterVisitSummary: Patient summary
    - transcript: Raw transcription
    """
    # Validate session_id is a UUID
    import re
    if not (re.match(r'^[0-9a-f\-]{36}$', session_id) or (is_demo_request() and session_id.startswith('demo-'))):
        return jsonify({"success": False, "error": "Invalid session ID format"}), 400

    # Demo mode: return cached streaming outputs
    if is_demo_request():
        cached = get_cached_response('streaming_outputs')
        if cached:
            # Override sessionId to match what was requested
            cached['sessionId'] = session_id
            return jsonify(cached)

    try:
        # In ECS, use default credentials (IAM role). Locally, use profile if set.
        if AWS_PROFILE and AWS_PROFILE not in ("default", ""):
            session = boto3.Session(profile_name=AWS_PROFILE)
        else:
            session = boto3.Session()
        s3 = session.client('s3', region_name=STREAMING_OUTPUT_REGION)
        
        # Discover the correct S3 prefix (path structure changed in newer API versions)
        base_prefix = _find_clinical_notes_prefix(s3, session_id)
        
        outputs = {}
        files_to_fetch = {
            'clinicalDoc': 'clinicalDoc.json',
            'medicalCodes': 'medicalCodes.json',
            'afterVisitSummary': 'afterVisitSummary.json',
            'transcript': 'transcript.json'
        }
        
        if not base_prefix:
            for key in files_to_fetch:
                outputs[key] = None
        else:
            # base_prefix points to .../clinical-notes/
            # medicalCodes.json moved to .../medical-codes/ in newer API versions
            post_stream_prefix = base_prefix.rsplit('clinical-notes/', 1)[0]
            for key, filename in files_to_fetch.items():
                try:
                    if key == 'medicalCodes':
                        # Try new path first (medical-codes/), fall back to old (clinical-notes/)
                        s3_key = post_stream_prefix + "medical-codes/" + filename
                        try:
                            response = s3.get_object(Bucket=STREAMING_OUTPUT_BUCKET, Key=s3_key)
                        except s3.exceptions.NoSuchKey:
                            s3_key = base_prefix + filename
                            response = s3.get_object(Bucket=STREAMING_OUTPUT_BUCKET, Key=s3_key)
                    else:
                        s3_key = base_prefix + filename
                        response = s3.get_object(Bucket=STREAMING_OUTPUT_BUCKET, Key=s3_key)
                    content = response['Body'].read().decode('utf-8')
                    outputs[key] = json.loads(content)
                except s3.exceptions.NoSuchKey:
                    outputs[key] = None
                except Exception as e:
                    outputs[key] = None
        
        return jsonify({
            "success": True,
            "sessionId": session_id,
            "outputs": outputs
        })
        
    except Exception as e:
        return _safe_error(e, "get_streaming_session_outputs")


@app.route('/api/streaming/session/<session_id>/clinical-doc', methods=['GET'])
def get_streaming_clinical_doc(session_id):
    """Fetch just the clinical document (SOAP notes) from a streaming session."""
    # Validate session_id is a UUID
    import re
    if not (re.match(r'^[0-9a-f\-]{36}$', session_id) or (is_demo_request() and session_id.startswith('demo-'))):
        return jsonify({"success": False, "error": "Invalid session ID format"}), 400

    # Demo mode: return cached clinical doc
    if is_demo_request():
        cached = get_cached_response('streaming_outputs')
        if cached and cached.get('outputs', {}).get('clinicalDoc'):
            return jsonify({"success": True, "sessionId": session_id, "clinicalDoc": cached['outputs']['clinicalDoc']})

    try:
        if AWS_PROFILE and AWS_PROFILE not in ("default", ""):
            session = boto3.Session(profile_name=AWS_PROFILE)
        else:
            session = boto3.Session()
        s3 = session.client('s3', region_name=STREAMING_OUTPUT_REGION)
        
        base_prefix = _find_clinical_notes_prefix(s3, session_id)
        if not base_prefix:
            return jsonify({"success": False, "error": "Clinical notes not available yet"}), 404
        
        s3_key = base_prefix + "clinicalDoc.json"
        response = s3.get_object(Bucket=STREAMING_OUTPUT_BUCKET, Key=s3_key)
        content = response['Body'].read().decode('utf-8')
        
        return jsonify({
            "success": True,
            "sessionId": session_id,
            "clinicalDoc": json.loads(content)
        })
        
    except Exception as e:
        return _safe_error(e, "get_streaming_clinical_doc")


@app.route('/api/streaming/session/<session_id>/medical-codes', methods=['GET'])
def get_streaming_medical_codes(session_id):
    """Fetch just the medical codes from a streaming session."""
    # Validate session_id is a UUID
    import re
    if not (re.match(r'^[0-9a-f\-]{36}$', session_id) or (is_demo_request() and session_id.startswith('demo-'))):
        return jsonify({"success": False, "error": "Invalid session ID format"}), 400

    # Demo mode: return cached medical codes
    if is_demo_request():
        cached = get_cached_response('streaming_outputs')
        if cached and cached.get('outputs', {}).get('medicalCodes'):
            return jsonify({"success": True, "sessionId": session_id, "medicalCodes": cached['outputs']['medicalCodes']})

    try:
        if AWS_PROFILE and AWS_PROFILE not in ("default", ""):
            session = boto3.Session(profile_name=AWS_PROFILE)
        else:
            session = boto3.Session()
        s3 = session.client('s3', region_name=STREAMING_OUTPUT_REGION)
        
        base_prefix = _find_clinical_notes_prefix(s3, session_id)
        if not base_prefix:
            return jsonify({"success": False, "error": "Medical codes not available yet"}), 404
        
        # Try new path (medical-codes/) first, fall back to old (clinical-notes/)
        post_stream_prefix = base_prefix.rsplit('clinical-notes/', 1)[0]
        try:
            s3_key = post_stream_prefix + "medical-codes/medicalCodes.json"
            response = s3.get_object(Bucket=STREAMING_OUTPUT_BUCKET, Key=s3_key)
        except s3.exceptions.NoSuchKey:
            s3_key = base_prefix + "medicalCodes.json"
            response = s3.get_object(Bucket=STREAMING_OUTPUT_BUCKET, Key=s3_key)
        content = response['Body'].read().decode('utf-8')
        
        return jsonify({
            "success": True,
            "sessionId": session_id,
            "medicalCodes": json.loads(content)
        })
        
    except Exception as e:
        return _safe_error(e, "get_streaming_medical_codes")


@app.route('/api/streaming/session/<session_id>/after-visit-summary', methods=['GET'])
def get_streaming_after_visit_summary(session_id):
    """Fetch just the after visit summary from a streaming session."""
    # Validate session_id is a UUID
    import re
    if not (re.match(r'^[0-9a-f\-]{36}$', session_id) or (is_demo_request() and session_id.startswith('demo-'))):
        return jsonify({"success": False, "error": "Invalid session ID format"}), 400

    # Demo mode: return cached AVS
    if is_demo_request():
        cached = get_cached_response('streaming_outputs')
        if cached and cached.get('outputs', {}).get('afterVisitSummary'):
            return jsonify({"success": True, "sessionId": session_id, "afterVisitSummary": cached['outputs']['afterVisitSummary']})

    try:
        if AWS_PROFILE and AWS_PROFILE not in ("default", ""):
            session = boto3.Session(profile_name=AWS_PROFILE)
        else:
            session = boto3.Session()
        s3 = session.client('s3', region_name=STREAMING_OUTPUT_REGION)
        
        base_prefix = _find_clinical_notes_prefix(s3, session_id)
        if not base_prefix:
            return jsonify({"success": False, "error": "After visit summary not available yet"}), 404
        
        s3_key = base_prefix + "afterVisitSummary.json"
        response = s3.get_object(Bucket=STREAMING_OUTPUT_BUCKET, Key=s3_key)
        content = response['Body'].read().decode('utf-8')
        
        return jsonify({
            "success": True,
            "sessionId": session_id,
            "afterVisitSummary": json.loads(content)
        })
        
    except Exception as e:
        return _safe_error(e, "get_streaming_after_visit_summary")




@app.route("/api/streaming/session/<session_id>/live-transcript", methods=["GET"])
def streaming_live_transcript(session_id):
    """Phase 2: Live transcript polling from CloudWatch Logs."""
    import re
    if not re.match(r"^[0-9a-f\-]{36}$", session_id):
        return jsonify({"success": False, "error": "Invalid session ID format"}), 400
    return live_transcript.get_segments(session_id)
# =============================================================================
# NARRATIVE SYNTHESIS API (Bedrock)
# =============================================================================

@app.route('/api/synthesize-narrative', methods=['POST'])
def synthesize_narrative():
    """
    Use Bedrock (Claude Sonnet 4.5) to synthesize patient summary sections
    into a cohesive clinical narrative.

    Request body:
    {
        "sections": {
            "overview": "...",
            "chronic and active conditions": "...",
            "current medications": "...",
            "recent care": "...",
            "recent results": "..."
        },
        "patientName": "Alex Nguyen",
        "patientAge": "62 yrs",
        "patientGender": "Female"
    }
    """
    try:
        data = request.get_json()
        if not data or 'sections' not in data:
            return jsonify({"success": False, "error": "Missing 'sections' field"}), 400

        sections = data['sections']
        patient_name = data.get('patientName', 'the patient')
        patient_age = data.get('patientAge', '')
        patient_gender = data.get('patientGender', '')

        # Build the prompt
        prompt = f"""<instruction>  # nosemgrep: raw-html-format — f-string builds a Bedrock prompt, not rendered HTML
You are a clinical assistant tasked with synthesizing patient data into a cohesive clinical narrative. Your goal is to create a single, well-structured paragraph suitable for a physician's pre-visit review.

<guidelines>
- Write in third person using professional clinical language
- Be concise while maintaining clinical accuracy
- Synthesize information from all provided sections into a flowing narrative
- Do NOT add, infer, or assume any information not explicitly present in the data
- Do NOT use bullet points, headers, or lists
- Maintain appropriate medical terminology and clinical tone
- Ensure the narrative flows logically from patient demographics through clinical history to current status
</guidelines>

<patient_data>
<demographics>
Patient: {patient_name}, {patient_age} {patient_gender}
</demographics>

<overview>
{sections.get('overview', 'N/A')}
</overview>

<conditions>
Chronic and Active Conditions:
{sections.get('chronic and active conditions', 'None documented')}
</conditions>

<medications>
Current Medications:
{sections.get('current medications', 'None documented')}
</medications>

<recent_care>
Recent Care:
{sections.get('recent care', 'N/A')}
</recent_care>
</patient_data>

<output_requirements>
Synthesize the above patient data into a single cohesive narrative paragraph. The paragraph should:
1. Begin with patient identification and overview
2. Integrate chronic/active conditions naturally
3. Incorporate current medications in context
4. Include recent care encounters
5. Flow as a unified clinical summary without section breaks

Provide ONLY the narrative paragraph without any preamble, introduction, or additional explanation.
</output_requirements>
</instruction>"""
        # NOTE: recent results hardcoded to N/A to keep narrative concise; lab values shown in metric cards
        # To re-enable: replace N/A in <recent_results> with: {sections.get('recent results', 'N/A')}

        # Call Bedrock
        if AWS_PROFILE and AWS_PROFILE not in ("default", ""):
            session = boto3.Session(profile_name=AWS_PROFILE)
        else:
            session = boto3.Session()
        bedrock = session.client('bedrock-runtime', region_name=BEDROCK_REGION)

        body = json.dumps({
            "anthropic_version": "bedrock-2023-05-31",
            "max_tokens": BEDROCK_MAX_TOKENS,
            "messages": [{"role": "user", "content": prompt}]
        })

        response = bedrock.invoke_model(
            modelId=BEDROCK_MODEL_ID,
            contentType='application/json',
            accept='application/json',
            body=body
        )

        result = json.loads(response['body'].read())
        narrative = result.get('content', [{}])[0].get('text', '')

        return jsonify({
            "success": True,
            "narrative": narrative,
            "model": BEDROCK_MODEL_ID,
            "usage": result.get('usage', {})
        })

    except Exception as e:
        return _safe_error(e, "synthesize_narrative")


# =============================================================================
# BEDROCK - VISIT PRIORITIES & CHECKLIST SYNTHESIS
# =============================================================================

@app.route('/api/synthesize-priorities', methods=['POST'])
def synthesize_priorities():
    """
    Use Bedrock (Claude Sonnet 4.5) to generate Visit Priorities and Checklist
    from raw Patient Insights sections.

    Request body:
    {
        "sections": {
            "overview": "...",
            "chronic and active conditions": "...",
            "current medications": "...",
            "recent care": "...",
            "recent results": "..."
        },
        "patientName": "Alex Nguyen",
        "patientAge": "62 yrs",
        "patientGender": "Female",
        "encounterReason": "Follow-up visit"
    }
    """
    try:
        data = request.get_json()
        if not data or 'sections' not in data:
            return jsonify({"success": False, "error": "Missing 'sections' field"}), 400

        sections = data['sections']
        patient_name = data.get('patientName', 'the patient')
        patient_age = data.get('patientAge', '')
        patient_gender = data.get('patientGender', '')
        encounter_reason = data.get('encounterReason', 'Follow-up visit')

        prompt = f"""<instruction>  # nosemgrep: raw-html-format — f-string builds a Bedrock prompt, not rendered HTML
You are a clinical decision-support assistant. Analyze the patient data below and generate structured visit priorities and a visit checklist for the physician's pre-visit review.

<guidelines>
- Base ALL outputs strictly on the provided patient data. Do NOT infer, assume, or fabricate any clinical information.
- Prioritize items by clinical urgency and relevance to the encounter reason.
- Use professional clinical language, concise and actionable.
- For priorities: identify what MUST be addressed this visit (high) vs ongoing management items (moderate).
- For checklist: generate specific, actionable tasks the physician should complete during or after the visit.
- If the data is insufficient to generate meaningful priorities, return minimal safe defaults.
</guidelines>

<patient_data>
<demographics>Patient: {patient_name}, {patient_age} {patient_gender}</demographics>
<encounter_reason>{encounter_reason}</encounter_reason>
<overview>{sections.get('overview', 'N/A')}</overview>
<conditions>{sections.get('chronic and active conditions', 'None documented')}</conditions>
<medications>{sections.get('current medications', 'None documented')}</medications>
<recent_care>{sections.get('recent care', 'N/A')}</recent_care>
<recent_results>{sections.get('recent results', 'N/A')}</recent_results>
</patient_data>

<output_requirements>
Return ONLY valid JSON (no markdown, no preamble) in this exact structure:
{{
  "mustAddress": [
    {{"title": "short title", "description": "one-line clinical detail", "severity": "high"}}
  ],
  "outstanding": [
    {{"title": "short title", "description": "one-line clinical detail", "severity": "moderate"}}
  ],
  "checklist": [
    "Actionable checklist item text"
  ]
}}

Rules:
- mustAddress: 1-4 items that require immediate attention this visit (abnormal labs, new symptoms, urgent follow-ups)
- outstanding: 1-4 items for ongoing management discussion (medication reviews, monitoring, lifestyle)
- checklist: 3-8 specific actionable tasks (e.g., "Review elevated Uric Acid 8.2 mg/dL - gout management", "Confirm Colchicine effectiveness")
- Each item must reference specific data points from the patient record when available (values, dates, medication names)
</output_requirements>
</instruction>"""

        # Call Bedrock
        if AWS_PROFILE and AWS_PROFILE not in ("default", ""):
            session = boto3.Session(profile_name=AWS_PROFILE)
        else:
            session = boto3.Session()
        bedrock = session.client('bedrock-runtime', region_name=BEDROCK_REGION)

        body = json.dumps({
            "anthropic_version": "bedrock-2023-05-31",
            "max_tokens": BEDROCK_MAX_TOKENS,
            "messages": [{"role": "user", "content": prompt}]
        })

        response = bedrock.invoke_model(
            modelId=BEDROCK_MODEL_ID,
            contentType='application/json',
            accept='application/json',
            body=body
        )

        result = json.loads(response['body'].read())
        raw_text = result.get('content', [{}])[0].get('text', '')

        # Parse JSON from response (strip markdown fences if present)
        clean = raw_text.strip()
        if clean.startswith('```'):
            clean = clean.split('\n', 1)[1] if '\n' in clean else clean[3:]
            clean = clean.rsplit('```', 1)[0]
        priorities = json.loads(clean)

        return jsonify({
            "success": True,
            "priorities": priorities,
            "model": BEDROCK_MODEL_ID,
            "usage": result.get('usage', {})
        })

    except json.JSONDecodeError as je:
        print(f"[PRIORITIES] JSON parse error: {je}, raw: {raw_text[:500]}")
        return jsonify({"success": False, "error": "Failed to parse Bedrock response as JSON"}), 502
    except Exception as e:
        return _safe_error(e, "synthesize_priorities")


# =============================================================================
# BEDROCK - UNIFIED PRE-VISIT SYNTHESIS
# =============================================================================

@app.route('/api/synthesize-previsit', methods=['POST'])
def synthesize_previsit():
    """
    Single Bedrock call to generate Patient Story, Since Last Visit, and Visit Priorities.
    Replaces the separate synthesize-narrative and synthesize-priorities endpoints.
    Accepts optional clinicalNarrative for evidence linking.
    """
    try:
        data = request.get_json()
        if not data or 'sections' not in data:
            return jsonify({"success": False, "error": "Missing 'sections' field"}), 400

        # Demo mode: return cached Bedrock response
        if is_demo_request():
            cache_id = data.get('patientId', '')[:8] or 'default'
            cached = get_cached_response('synthesize_previsit', cache_id)
            if cached:
                return jsonify(cached)

        sections = data['sections']
        patient_age = data.get('patientAge', '')
        patient_gender = data.get('patientGender', '')
        clinical_narrative = data.get('clinicalNarrative', [])
        clinical_narrative = data.get('clinicalNarrative', [])

        # Extract last visit date from overview if available
        overview_text = sections.get('overview', '')
        import re
        last_visit_match = re.search(r'last visit was on\s*(\d{2}/\d{2}/\d{4})', overview_text, re.IGNORECASE)
        last_visit_date = last_visit_match.group(1) if last_visit_match else 'unknown date'

        # Build evidence map from ClinicalNarrative
        evidence_map = {}
        evidence_instructions = ""
        if clinical_narrative:
            ref_counter = 0
            for item in clinical_narrative:
                text = (item.get('Text') or '').strip()
                evidence = item.get('Evidence') or []
                if text and evidence and len(text) > 3:
                    ref_counter += 1
                    ref_id = f"REF{ref_counter}"
                    evidence_map[ref_id] = evidence
                    # Only include meaningful text fragments (skip headers like "## Overview")
                    if not text.startswith('#') and len(text) > 5:
                        evidence_instructions += f'- Fact: "{text}" → marker: [{ref_id}]\n'

            if evidence_instructions:
                evidence_instructions = f"""  # nosemgrep: raw-html-format — f-string builds evidence linking instructions for a Bedrock prompt, not rendered HTML
<evidence_linking>
The following facts from the patient record have supporting FHIR evidence. When you include these facts in your output, wrap the relevant phrase with evidence markers using the format [REFn]phrase[/REFn].

{evidence_instructions}

IMPORTANT:
- Wrap the ENTIRE relevant phrase, not individual words. For example: [REF3]idiopathic gout of the right wrist[/REF3]
- Only use markers for facts you actually include in your output
- If you rephrase a fact, still wrap the rephrased version with its marker
- Do NOT nest markers inside each other
- A phrase should only have ONE marker even if the original had multiple evidence items
- Apply markers in ALL sections: Patient Story, Since Last Visit, AND Visit Priorities (titles and descriptions)
</evidence_linking>
"""


        prompt = f"""You are a clinical assistant synthesizing patient data into a structured pre-visit summary for a physician. Your output must follow the exact format specified below.  # nosemgrep: raw-html-format — f-string builds a Bedrock prompt, not rendered HTML

<guidelines>
Third person, professional clinical language; maximally concise
State only clinical facts — omit process details and source attribution
Do NOT add, infer, or assume information not explicitly in the data
Do NOT include the patient’s name — use age and gender only
Lab value labeling: For every lab value reported in the SINCE LAST VISIT section, append a parenthetical tag of "(normal)" or "(abnormal)". Determine the tag using:
• If the source data explicitly flags a value — use that flag
• If the source data provides a reference range — compare to it
• Otherwise — use standard clinical reference ranges (sex-specific where applicable)
Do NOT omit the tag — every lab value in SINCE LAST VISIT must have one.
Do NOT append these tags in PATIENT STORY or VISIT PRIORITIES. In PATIENT STORY, descriptive language like "elevated" may appear in narrative (e.g., "Uric acid was elevated at 9.6 mg/dL") but no parenthetical tag. In VISIT PRIORITIES, state the value and context without a tag.
Do NOT add diagnostic interpretation of lab values — state values and tags only
Do NOT suggest clinical actions or treatment recommendations
Each clinical fact appears once in its most contextually appropriate location — no repetition across sections
Extract medications from ALL data sections (medication lists, pharmacy communications, clinical notes)
</guidelines>

<patient_data>
Age: {patient_age}, Gender: {patient_gender}

Patient and Encounter Overview:
{sections.get('overview', 'N/A')}

Chronic and Active Conditions
{sections.get('chronic and active conditions', 'None documented')}

Recent Care:
{sections.get('recent care', 'N/A')}

Current Medications:
{sections.get('current medications', 'None documented')}

Recent Results:
{sections.get('recent results', 'N/A')}

Since Last Visit:
{sections.get('sinceLastVisit', 'N/A')}

IMPORTANT: ALL events described in the "Since Last Visit" section above occurred AFTER the last visit with this provider. Treat every dated item in this section as a post-visit event that MUST appear in the SINCE LAST VISIT output section. Do NOT discard them based on the overview's stated last visit date.

Trends:
{sections.get('trends', 'N/A')}

CMS-HCC Coding Analysis:
{sections.get('cmsHcc', 'N/A')}

HHS-HCC Coding Analysis:
{sections.get('hhsHcc', 'N/A')}
</patient_data>

<output_format>
Structure output into exactly three sections using Unicode box-drawing characters (─ ═) for rules. Provide ONLY the formatted output — no preamble or explanation.

PATIENT STORY ──────────────────────────────────────────────────────────────────

[Exactly two flowing narrative paragraphs. No bullets, headers, or lists.]

Paragraph 1 — Background and clinical context (4–6 sentences max):
Open: "XX-year-old [sex] presenting for [reason]."
Integrate active diagnoses with active voice ("she carries a diagnosis of…," "he has a history of…"). Group related conditions (e.g., "contusion and pain of right upper arm" not listed separately).
Include disease trajectory with recurrence dates and affected sites where relevant.
Medications: Include ONLY medications that meet at least one of these criteria:
• Documented interaction requiring dose adjustment or monitoring
• Recently prescribed, changed, or discontinued since last visit
• Directly relevant to the active presenting issue or today’s visit reason
For included medications, integrate naturally with dose details. If multiple qualifying medications share the same relevance reason, group concisely.
Do NOT list background medications that are stable and unrelated to the visit.
Every sentence must have a human or clinical subject — no "findings include" or "medications include" constructions.
Do NOT include imaging findings, lab values, or exam findings in Paragraph 1. These belong exclusively in Paragraph 2.

Paragraph 2 — Last visit with this provider (4–6 sentences max):
The last visit with this provider is the visit that occurred BEFORE all events in the Since Last Visit input data. Everything in that data section, by definition, happened after the last visit with this provider. Use the earliest date in the Since Last Visit data as a boundary.
If the Patient and Encounter Overview states a "last visit" date that conflicts with this, defer to the Since Last Visit boundary.
If the source data contains no detailed clinical information about the last visit with this provider, state only what is known (e.g., "On 07/18/2024, she presented for routine ambulatory care and laboratory studies were ordered").
Do NOT fold details from visits documented in Since Last Visit into this paragraph.
Open: "On [date], [he/she] presented with…" folding exam findings directly into the presentation.
Fold patient-reported details into the sentence they most naturally modify.
Do NOT repeat any information already stated in Paragraph 1.
Lab selectivity: include ONLY the abnormal value(s) that directly informed the visit’s diagnosis or assessment. Omit normal values entirely.
For imaging, state key findings concisely — combine related findings where possible.
Combine diagnosis and workup into a single closing sentence.
Do not refer to "the provider" in third person.

SINCE LAST VISIT ──────────────────────────────────────────────────────────────────

[Chronologically ordered items with indented "·" sub-items. Use a single header format for ALL items:]

Header format: Descriptive label (date):
One factual summary sentence of what occurred.
· finding, order, or medication change

For encounters/visits, use the visit type as the label (e.g., "Clinic Visit (10/06/2024):", "Urgent Care Visit (11/01/2024):").
If provider/specialty is undocumented, infer from clinical actions.
For non-encounter items (lab results, pharmacy communications, imaging), use a descriptive label (e.g., "Laboratory Results (07/19/2024):", "Pharmacy Communication (10/21/2024):").

Rules:
Every header gets one summary sentence, then "·" sub-items
One fact per sub-item; related values may share a sub-item
Do NOT sub-categorize under labels like "Diagnoses:", "Vitals:" — flat sequence of "·" items
Summarize unremarkable panels concisely (e.g., "Normal CBC values")
Every lab value must include (normal) or (abnormal) tag per the guidelines
Group same-date, same-source actions under one header
After drafting this section, verify completeness: re-scan ALL source data sections for any event, documentation, lab result, or vital dated AFTER the last visit with this provider. Any dated item that falls after this date MUST appear here.
If nothing occurred: "No events documented since the last visit."

VISIT PRIORITIES ──────────────────────────────────────────────────────────────────

TODAY’S VISIT — MUST ADDRESS

[Item Name]
[Single-line factual context ≤15 words — value, date, or reason it requires attention]

Include: abnormal labs, active symptoms re-documented since last visit, medication interactions, any condition from the CMS-HCC or HHS-HCC coding analysis data.
For HCC items, format as: condition name [CMS-HCC], [HHS-HCC], or [CMS-HCC / HHS-HCC] with "Last assessed [date]; confirm if still present."
Do not list the visit reason itself as a priority.

OUTSTANDING — CONFIRM STATUS

[Item Name]
[Single-line factual context ≤15 words — when ordered/identified, what is unknown]

Include: ordered tests with unknown completion, findings without documented follow-up, pending referrals.
If either sub-section has no items, state "None identified."

══════════════════════════════════════════════════════════════════════════════
</output_format>


{evidence_instructions}"""

        # Call Bedrock
        if AWS_PROFILE and AWS_PROFILE not in ("default", ""):
            session = boto3.Session(profile_name=AWS_PROFILE)
        else:
            session = boto3.Session()
        bedrock = session.client('bedrock-runtime', region_name=BEDROCK_REGION)

        body = json.dumps({
            "anthropic_version": "bedrock-2023-05-31",
            "max_tokens": BEDROCK_MAX_TOKENS,
            "messages": [{"role": "user", "content": prompt}]
        })

        response = bedrock.invoke_model(
            modelId=BEDROCK_MODEL_ID,
            contentType='application/json',
            accept='application/json',
            body=body
        )

        result = json.loads(response['body'].read())
        raw_text = result.get('content', [{}])[0].get('text', '')

        # Parse the structured output into sections
        parsed = _parse_previsit_output(raw_text, last_visit_date)

        # Log evidence marker usage
        ref_count = raw_text.count('[REF')
        print(f"[PREVISIT] Evidence markers in output: {ref_count}, evidence_map entries: {len(evidence_map)}")
        
        # Log raw sections for debugging
        print(f"[PREVISIT] Raw output (first 300): {raw_text[:300]}")
        print(f"[PREVISIT] Has 'SINCE LAST': {'SINCE LAST' in raw_text}")
        
        # Log since last visit bullets for debugging
        slv = parsed.get("sinceLastVisit", [])
        print(f"[PREVISIT] Since Last Visit categories: {len(slv)}")
        for i, b in enumerate(slv[:3]):
            print(f"  [{i}] {str(b)[:120]}")

        response_data = {
            "success": True,
            "patientStory": parsed.get("patientStory", ""),
            "sinceLastVisit": parsed.get("sinceLastVisit", []),
            "visitPriorities": parsed.get("visitPriorities", {}),
            "evidenceMap": evidence_map,
            "raw": raw_text,
            "model": BEDROCK_MODEL_ID,
            "usage": result.get('usage', {})
        }

        # Record for demo cache if recording is enabled
        cache_id = data.get('patientId', '')[:8] or 'default'
        save_to_cache('synthesize_previsit', cache_id, response_data)

        return jsonify(response_data)

    except Exception as e:
        return _safe_error(e, "synthesize_previsit")


def _parse_previsit_output(text, last_visit_date):
    """Parse the structured Bedrock output into sections."""
    result = {"patientStory": "", "sinceLastVisit": [], "visitPriorities": {}}

    # Split by section headers
    import re
    # Find Patient Story section
    story_match = re.search(r'PATIENT STORY\s*[-─═]+\s*\n(.*?)(?=SINCE LAST VISIT|$)', text, re.DOTALL | re.IGNORECASE)
    if story_match:
        result["patientStory"] = story_match.group(1).strip()

    # Find Since Last Visit section
    since_match = re.search(r'SINCE LAST VISIT[^\n]*\n[-─═]+\s*\n(.*?)(?=VISIT PRIORITIES|$)', text, re.DOTALL | re.IGNORECASE)
    if not since_match:
        # Try without the horizontal rule line
        since_match = re.search(r'SINCE LAST VISIT[^\n]*\n\n(.*?)(?=VISIT PRIORITIES|$)', text, re.DOTALL | re.IGNORECASE)
    if since_match:
        raw_since = since_match.group(1).strip()
        print(f"[PARSER] Raw Since Last Visit text ({len(raw_since)} chars): {raw_since[:300]}")
        # Parse category/encounter headers with · sub-items
        categories = []
        current_category = None
        for line in since_match.group(1).strip().split('\n'):
            line = line.strip()
            if not line:
                continue
            # Check if it's a header: category header (ends with ":") or encounter header (date pattern with —)
            is_category_header = line.endswith(':') and not line.startswith(('•', '-', '·', '*'))
            is_encounter_header = bool(re.match(r'^\d{2}/\d{2}/\d{4}\s*[—\-]', line)) or bool(re.match(r'^\w+\s+\d{1,2},?\s+\d{4}\s*[—\-]', line))
            if is_category_header or is_encounter_header:
                current_category = {"header": line.rstrip(':'), "items": []}
                categories.append(current_category)
            elif line.startswith(('·', '•', '-', '*')):
                item_text = line.lstrip('·•-* ').strip()
                if current_category:
                    current_category["items"].append(item_text)
                else:
                    current_category = {"header": "", "items": [item_text]}
                    categories.append(current_category)
            elif current_category and current_category["items"]:
                # Continuation of previous sub-item
                current_category["items"][-1] += ' ' + line
            elif current_category and not current_category["items"]:
                # Summary sentence after header — store separately, don't add as bullet item
                current_category["_summary"] = line
            else:
                # Standalone line — treat as implicit header
                current_category = {"header": line, "items": []}
                categories.append(current_category)
        result["sinceLastVisit"] = categories

    # Find Visit Priorities section
    priorities_match = re.search(r'VISIT PRIORITIES\s*[-─═]+\s*\n(.*?)(?=═|$)', text, re.DOTALL | re.IGNORECASE)
    if priorities_match:
        priorities_text = priorities_match.group(1).strip()
        must_address = []
        outstanding = []

        # Split into must-address and outstanding
        must_section = re.search(r"TODAY'S VISIT.*?MUST ADDRESS\s*\n(.*?)(?=OUTSTANDING|$)", priorities_text, re.DOTALL | re.IGNORECASE)
        outstanding_section = re.search(r"OUTSTANDING.*?CONFIRM STATUS\s*\n(.*?)$", priorities_text, re.DOTALL | re.IGNORECASE)

        if must_section:
            lines = [l.strip().lstrip('\u2022-\u00b7* ') for l in must_section.group(1).strip().split('\n') if l.strip()]
            # Pair consecutive lines: odd lines are titles, even lines are descriptions
            i = 0
            while i < len(lines):
                title = lines[i].strip()
                clean_title = re.sub(r'\[/?REF\d+\]', '', title).strip()
                if not clean_title:
                    i += 1
                    continue
                desc = ''
                if i + 1 < len(lines):
                    desc = lines[i + 1].strip()
                    i += 2
                else:
                    i += 1
                if title and title.lower() not in ('none documented.', 'none documented', 'none identified.', 'none identified', 'none.', 'none'):
                    hcc_re = r'\s*\[(?:CMS-HCC|HHS-HCC|CMS-HCC\s*/\s*HHS-HCC)\]'
                    must_address.append({"title": re.sub(hcc_re, '', title).strip(), "description": re.sub(hcc_re, '', desc).strip(), "severity": "high"})

        if outstanding_section:
            lines = [l.strip().lstrip('\u2022-\u00b7* ') for l in outstanding_section.group(1).strip().split('\n') if l.strip()]
            i = 0
            while i < len(lines):
                title = lines[i].strip()
                clean_title = re.sub(r'\[/?REF\d+\]', '', title).strip()
                if not clean_title:
                    i += 1
                    continue
                desc = ''
                if i + 1 < len(lines):
                    desc = lines[i + 1].strip()
                    i += 2
                else:
                    i += 1
                if title and title.lower() not in ('none documented.', 'none documented', 'none identified.', 'none identified', 'none.', 'none'):
                    hcc_re = r'\s*\[(?:CMS-HCC|HHS-HCC|CMS-HCC\s*/\s*HHS-HCC)\]'
                    outstanding.append({"title": re.sub(hcc_re, '', title).strip(), "description": re.sub(hcc_re, '', desc).strip(), "severity": "moderate"})

        result["visitPriorities"] = {
            "mustAddress": must_address,
            "outstanding": outstanding
        }

    return result


# =============================================================================
# SMS NOTIFICATION API
# =============================================================================

@app.route('/api/send-sms', methods=['POST'])
def send_sms():
    """
    Send SMS notification to patient using AWS Pinpoint SMS (from separate account).
    Uses the origination number configured in SMS_ORIGINATION_NUMBER.
    
    In demo mode (authenticated), returns a fake success response — never sends real SMS.
    
    Request body:
    {
        "phoneNumber": "+15551234567",  # E.164 format
        "message": "Your follow-up appointment reminder..."
    }
    """
    try:
        data = request.get_json()
        
        if not data:
            return jsonify({"success": False, "error": "Missing request body"}), 400

        # Demo mode: return fake success — NEVER send real SMS in demo mode
        # Auth is already enforced by the before_request hook; this only returns canned data.
        if is_demo_request():
            return jsonify({
                "success": True,
                "messageId": "demo-msg-" + str(int(time.time())),
                "message": "SMS sent successfully (demo mode)"
            })
        
        phone_number = data.get('phoneNumber')
        message = data.get('message')
        
        if not phone_number:
            return jsonify({"success": False, "error": "Missing 'phoneNumber' field"}), 400
        
        if not message:
            return jsonify({"success": False, "error": "Missing 'message' field"}), 400
        
        # Validate E.164 format (starts with + and has 10-15 digits)
        import re
        if not re.match(r'^\+[1-9]\d{9,14}$', phone_number):
            return jsonify({
                "success": False, 
                "error": "Invalid phone number format. Use E.164 format (e.g., +15551234567)"
            }), 400
        
        # Create Pinpoint SMS client using separate account profile
        sms_session = boto3.Session(profile_name=SMS_AWS_PROFILE)
        pinpoint_sms = sms_session.client('pinpoint-sms-voice-v2', region_name=SMS_REGION)
        
        # Send SMS via Pinpoint SMS Voice V2 API
        response = pinpoint_sms.send_text_message(
            DestinationPhoneNumber=phone_number,
            OriginationIdentity=SMS_ORIGINATION_NUMBER,
            MessageBody=message,
            MessageType='TRANSACTIONAL'
        )
        
        return jsonify({
            "success": True,
            "messageId": response.get('MessageId'),
            "message": "SMS sent successfully"
        })
        
    except Exception as e:
        print(f"[SMS ERROR] {str(e)}")
        return jsonify({"success": False, "error": "Failed to send SMS"}), 500


# =============================================================================
# MAIN
# =============================================================================



@app.route("/api/debug/transcript/<session_id>", methods=["GET"])
def debug_transcript(session_id):
    """Debug endpoint - directly queries CloudWatch without threading."""
    import boto3, re as regex, json as jjson
    client = boto3.client('logs', region_name='us-east-1')
    import time
    start_time_ms = int((time.time() - 7200) * 1000)  # 2 hours back
    
    try:
        resp = client.filter_log_events(
            logGroupName='/ecs/connect-health-bridge-dev',
            filterPattern='transcript',
            startTime=start_time_ms,
            limit=50,
        )
        events = resp.get('events', [])
        
        # Parse and filter for this session
        segments = []
        raw_messages = []
        for event in events[:10]:
            msg = event.get('message', '')
            raw_messages.append(msg[:200])
            matches = regex.findall(r'\[Session:[^\]]+\]\s*(\{"type":"transcript"[^}]*\})', msg)
            for json_str in matches:
                try:
                    obj = jjson.loads(json_str)
                    if obj.get('sessionId') == session_id:
                        segments.append({'text': obj.get('text',''), 'final': obj.get('final', False)})
                except:
                    pass
        
        return jsonify({
            'success': True,
            'total_events': len(events),
            'matched_segments': len(segments),
            'segments': segments[:20],
            'raw_first_3': raw_messages[:3],
        })
    except Exception as e:
        return jsonify({'success': False, 'error': str(e)})


# ══════════════════════════════════════════════════════════════════════════════
# ACT 2 — Care Intelligence Workspace
# ══════════════════════════════════════════════════════════════════════════════

# Directory holding the Act 2 Care Intelligence UI. Resolved relative to this
# module so it works both locally (<repo>/care-intelligence) and in the
# container (/care-intelligence), mirroring static_folder='../frontend'.
CARE_INTELLIGENCE_DIR = os.path.join(
    os.path.dirname(os.path.abspath(__file__)), '..', 'care-intelligence'
)

@app.route('/care-intelligence', methods=['GET'])
@app.route('/care-intelligence/', methods=['GET'])
def serve_care_intelligence_index():
    """Serve the Act 2 Care Intelligence UI."""
    return send_from_directory(CARE_INTELLIGENCE_DIR, 'index.html')

@app.route('/care-intelligence/<path:filename>', methods=['GET'])
def serve_care_intelligence_static(filename):
    """Serve static assets for the Act 2 Care Intelligence UI."""
    return send_from_directory(CARE_INTELLIGENCE_DIR, filename)




@app.route('/api/coding-agent', methods=['POST'])
def coding_agent():
    """
    Coding Agent — generates ICD-10-CM and CPT codes from SOAP note.
    Called by the frontend when HealthScribe doesn't produce codes.
    
    Body: { "soapText": "..." }
    Returns: { "success": true, "medicalCodes": [...] }
    """
    try:
        data = request.get_json(force=True)
        soap_text = data.get('soapText', '')

        if not soap_text:
            return jsonify({'success': False, 'error': 'soapText is required'}), 400

        result = generate_medical_codes_from_soap(soap_text)
        return jsonify({
            'success': True,
            'medicalCodes': result.get('medicalCodes', [])
        })

    except Exception as e:
        app.logger.error(f"Coding agent error: {e}")
        return jsonify({'success': False, 'error': str(e)}), 500


@app.route('/api/fhir-writeback', methods=['POST'])
def fhir_writeback():
    """
    Write post-call clinical data back to HealthLake.
    Called when provider clicks Approve & Sign.
    
    Uses the Clinical Data Agent (Bedrock) to extract structured data from the SOAP note
    and writes ALL clinical categories as discrete FHIR resources:
    - Encounter (visit record)
    - DocumentReference (SOAP text)
    - Conditions (from ICD-10 codes + Bedrock extraction)
    - MedicationRequests (extracted from SOAP)
    - AllergyIntolerances (extracted from SOAP)
    - Observations (vitals/labs extracted from SOAP)
    
    Body: { patientId, sessionId, patientName, soapText, medicalCodes }
    """
    try:
        data = request.get_json(force=True)
        patient_id = data.get('patientId', '').strip()
        session_id = data.get('sessionId', '').strip()
        patient_name = data.get('patientName', 'Unknown Patient')
        soap_text = data.get('soapText', '')
        medical_codes = data.get('medicalCodes')

        if not patient_id:
            return jsonify({'success': False, 'error': 'patientId is required'}), 400
        if not soap_text:
            return jsonify({'success': False, 'error': 'soapText is required'}), 400

        # Validate patient_id format
        import re
        if not re.match(r'^[a-zA-Z0-9\-]+$', patient_id):
            return jsonify({"success": False, "error": "Invalid patient ID"}), 400

        results = fhir_writeback_with_extraction(
            patient_id=patient_id,
            patient_name=patient_name,
            session_id=session_id,
            soap_text=soap_text,
            medical_codes=medical_codes
        )

        return jsonify(results)

    except Exception as e:
        app.logger.error(f"FHIR writeback error: {e}")
        return jsonify({'success': False, 'error': str(e)}), 500


@app.route('/api/patient-chart/<patient_id>', methods=['GET'])
def patient_chart_endpoint(patient_id):
    """
    Fetch real clinical data from HealthLake for provider pre-visit review.
    Returns: demographics, conditions, medications, allergies, encounters, observations.
    Called by Clinical Workspace when provider clicks patient or consultation starts.
    """
    try:
        # Validate patient_id format
        import re
        if not re.match(r'^[a-zA-Z0-9\-]+$', patient_id):
            return jsonify({"success": False, "error": "Invalid patient ID"}), 400

        chart_data = get_patient_chart(patient_id)
        return jsonify(chart_data)

    except Exception as e:
        app.logger.error(f"Patient chart error: {e}")
        return jsonify({"success": False, "error": str(e)}), 500


# ============================================================================
# CARE INTELLIGENCE AGENT — Dual Mode (AgentCore / Classic)
# Set USE_AGENTCORE=true to use AgentCore Harness, false for Classic Bedrock Agent
# ============================================================================
import os
USE_AGENTCORE = os.environ.get('USE_AGENTCORE', 'true').lower() == 'true'

HARNESS_ARN = 'arn:aws:bedrock-agentcore:us-east-1:123456789012:harness/care-intel-harness-EXAMPLE'
CLASSIC_AGENT_ID = 'EXAMPLE123'
CLASSIC_ALIAS_ID = 'TSTALIASID'


@app.route('/api/bedrock-agent/invoke', methods=['POST'])
def invoke_bedrock_agent():
    """
    Care Intelligence chat endpoint.
    Routes to AgentCore Harness (default) or Classic Bedrock Agent (fallback).
    Toggle via USE_AGENTCORE env var.
    """
    try:
        body = request.get_json(force=True)
        session_id = body.get('sessionId', 'default-session')
        # AgentCore requires runtimeSessionId to be >= 33 characters and
        # rejects shorter values before the request reaches the agent. Pad
        # deterministically so any client is safe and multi-turn context
        # (which is keyed on this id) stays stable across turns.
        if len(session_id) < 33:
            session_id = (session_id + '-' + session_id + '-agentcore-session-pad')[:48]
        input_text = body.get('inputText', '')

        if not input_text:
            return jsonify({'success': False, 'error': 'inputText is required'}), 400

        if AWS_PROFILE:
            boto_session = boto3.Session(profile_name=AWS_PROFILE)
        else:
            boto_session = boto3.Session()

        if USE_AGENTCORE:
            return _invoke_agentcore(boto_session, session_id, input_text)
        else:
            return _invoke_classic(boto_session, session_id, input_text, body)

    except Exception as e:
        app.logger.error(f"Care Intelligence agent error: {e}")
        return jsonify({'success': False, 'error': str(e)}), 500


def _invoke_agentcore(boto_session, session_id, input_text):
    """Invoke via AgentCore Harness (InvokeHarness API)."""
    dataplane = boto_session.client('bedrock-agentcore', region_name=AWS_REGION)

    resp = dataplane.invoke_harness(
        harnessArn=HARNESS_ARN,
        runtimeSessionId=session_id,
        messages=[
            {
                'role': 'user',
                'content': [{'text': input_text}]
            }
        ]
    )

    # Consume streaming response
    stream = resp.get('stream', resp.get('body', None))
    full_text = ''
    trace_info = None

    if hasattr(stream, '__iter__'):
        for event in stream:
            if isinstance(event, dict):
                # Text content from assistant
                if 'contentBlockDelta' in event:
                    delta = event['contentBlockDelta'].get('delta', {})
                    if 'text' in delta:
                        full_text += delta['text']
                elif 'messageStart' in event:
                    pass
                elif 'messageStop' in event:
                    pass
                elif 'metadata' in event:
                    pass
                # Tool use events for trace
                elif 'contentBlockStart' in event:
                    block = event['contentBlockStart'].get('start', {})
                    if 'toolUse' in block:
                        tool_name = block['toolUse'].get('name', '')
                        trace_info = tool_name
                # Fallback: try to extract text from any structure
                elif 'chunk' in event:
                    chunk_data = event['chunk']
                    if isinstance(chunk_data, dict) and 'bytes' in chunk_data:
                        full_text += chunk_data['bytes'].decode('utf-8', errors='replace')
                    elif isinstance(chunk_data, bytes):
                        full_text += chunk_data.decode('utf-8', errors='replace')

    return jsonify({
        'success': True,
        'response': full_text,
        'trace': trace_info,
        'session': session_id,
        'engine': 'agentcore'
    })


def _invoke_classic(boto_session, session_id, input_text, body):
    """Invoke via Classic Bedrock Agent (InvokeAgent API). Fallback path."""
    agent_id = body.get('agentId', CLASSIC_AGENT_ID)
    agent_alias_id = body.get('agentAliasId', CLASSIC_ALIAS_ID)

    agent_client = boto_session.client('bedrock-agent-runtime', region_name=AWS_REGION)

    resp = agent_client.invoke_agent(
        agentId=agent_id,
        agentAliasId=agent_alias_id,
        sessionId=session_id,
        inputText=input_text,
        enableTrace=True
    )

    full_text = ''
    trace_info = None

    for event in resp.get('completion', []):
        if 'chunk' in event:
            chunk_bytes = event['chunk'].get('bytes', b'')
            if isinstance(chunk_bytes, bytes):
                full_text += chunk_bytes.decode('utf-8', errors='replace')
            else:
                full_text += str(chunk_bytes)
        elif 'trace' in event:
            t = event['trace'].get('trace', {})
            ag = (t.get('orchestrationTrace', {})
                   .get('invocationInput', {})
                   .get('actionGroupInvocationInput', {}))
            if ag:
                path = ag.get('apiPath', '')
                params = ag.get('parameters', [])
                ps = ', '.join(f"{p['name']}={p['value']}" for p in params)
                trace_info = f"{path[1:] if path.startswith('/') else path}"
                if ps:
                    trace_info += f" ({ps})"

    return jsonify({
        'success': True,
        'response': full_text,
        'trace': trace_info,
        'session': session_id,
        'engine': 'classic'
    })


if __name__ == '__main__':
    mode_str = "DEMO MODE (cached S3 data)" if DEMO_MODE else "LIVE MODE (real API calls)"
    print(f"""
╔══════════════════════════════════════════════════════════════╗
║         AWS Patient Insights Backend Proxy                   ║
╠══════════════════════════════════════════════════════════════╣
║  Mode: {mode_str:<52} ║
╠══════════════════════════════════════════════════════════════╣
║  API Endpoints:                                              ║
║    GET  /api/health              - Health check              ║
║    GET  /api/patients            - List patients             ║
║    POST /api/echo                - Test AWS connectivity     ║
║    POST /api/medical-codes       - Generate ICD10/CPT codes  ║
║    POST /api/patient-insights/start - Start insights job     ║
║    GET  /api/patient-insights/job/<arn> - Get job status     ║
║    GET  /api/patient-insights/output/<uri> - Get S3 output   ║
║    POST /api/patient-insights/run - Run full workflow        ║
║    POST /api/send-sms            - Send SMS via SNS          ║
║  Streaming Session Outputs:                                  ║
║    GET  /api/streaming/session/<id>/outputs - All outputs    ║
║    GET  /api/streaming/session/<id>/clinical-doc - SOAP      ║
║    GET  /api/streaming/session/<id>/medical-codes - Codes    ║
║    GET  /api/streaming/session/<id>/after-visit-summary      ║
╠══════════════════════════════════════════════════════════════╣
║  UI: http://localhost:5000                                   ║
╠══════════════════════════════════════════════════════════════╣
║  Config:                                                     ║
║    AWS Profile: {AWS_PROFILE:<43} ║
║    HealthLake:  {HEALTHLAKE_DATASTORE_ID:<43} ║
║    Region:      {AWS_REGION:<43} ║
║    SMS Profile: {SMS_AWS_PROFILE:<43} ║
║    SMS Number:  {SMS_ORIGINATION_NUMBER:<43} ║
╠══════════════════════════════════════════════════════════════╣
║  Usage:                                                      ║
║    python server.py          # Live mode (real API)          ║
║    python server.py --demo   # Demo mode (cached S3 data)    ║
╚══════════════════════════════════════════════════════════════╝
    """)
    
    app.run(host=SERVER_HOST, port=SERVER_PORT, debug=DEBUG)
