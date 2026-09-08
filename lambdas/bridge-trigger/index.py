# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""
Lambda triggered from the Contact Flow after Start Media Streaming.
Writes contact metadata to S3 (for SMS notification) then POSTs to bridge.
"""

import json
import os
import urllib.request
import urllib.error
import boto3

BRIDGE_URL     = os.environ["BRIDGE_URL"]
DEFAULT_DOMAIN = os.environ.get("DOMAIN_ID", "")
DEFAULT_SUB    = os.environ.get("SUBSCRIPTION_ID", "")
S3_BUCKET      = os.environ.get("S3_BUCKET", "connect-health-demo-123456789012")

s3 = boto3.client('s3')


def lambda_handler(event, context):
    print("Event:", json.dumps(event))

    contact_data = event.get("Details", {}).get("ContactData", {})
    contact_id = contact_data.get("ContactId")
    if not contact_id:
        return {"status": "error", "error": "no contact_id in event"}

    # Read inline media stream info from the event
    media_streams = contact_data.get("MediaStreams") or {}
    customer_audio = (
        media_streams.get("Customer", {})
                     .get("Audio", {})
    )
    stream_arn = customer_audio.get("StreamARN")
    fragment_number = customer_audio.get("StartFragmentNumber")

    if not stream_arn or not fragment_number:
        msg = (f"No KVS stream info in event for contact {contact_id}. "
               f"Make sure 'Start Media Streaming' precedes this Lambda.")
        print(msg)
        return {"status": "error", "error": msg}

    attributes = contact_data.get("Attributes", {}) or {}
    patient_id = attributes.get("patient_id", "")
    domain_id  = attributes.get("domain_id") or DEFAULT_DOMAIN

    # Write contact metadata to S3 for SMS notification Lambda
    customer_endpoint = contact_data.get("CustomerEndpoint", {})
    customer_phone = customer_endpoint.get("Address", "")
    try:
        metadata = {
            "contactId": contact_id,
            "customerPhone": customer_phone,
            "patientId": patient_id,
            "patientName": attributes.get("patient_name", ""),
        }
        s3.put_object(
            Bucket=S3_BUCKET,
            Key=f"{contact_id}/contact-metadata.json",
            Body=json.dumps(metadata),
            ContentType="application/json"
        )
        print(f"Wrote contact metadata to s3://{S3_BUCKET}/{contact_id}/contact-metadata.json")
    except Exception as e:
        print(f"Warning: failed to write metadata to S3: {e}")

    payload = {
        "contactId":      contact_id,
        "streamArn":      stream_arn,
        "fragmentNumber": fragment_number,
        "domainId":       domain_id,
        "patientId":      patient_id,
    }

    print("Posting to bridge:", json.dumps(payload))

    req = urllib.request.Request(
        BRIDGE_URL,
        data=json.dumps(payload).encode("utf-8"),
        headers={"Content-Type": "application/json"},
        method="POST",
    )

    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            body = resp.read().decode("utf-8")
            print(f"Bridge HTTP {resp.status}: {body}")
            return {"status": "ok", "bridge_response": body}
    except urllib.error.HTTPError as e:
        body = e.read().decode("utf-8") if e.fp else ""
        print(f"Bridge HTTP error {e.code}: {body}")
        return {"status": "error", "code": e.code, "body": body}
    except Exception as e:
        print(f"Bridge request failed: {e}")
        return {"status": "error", "error": str(e)}
