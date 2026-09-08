# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
FROM public.ecr.aws/docker/library/python:3.11-slim

WORKDIR /app

# Install dependencies
COPY backend/requirements.txt .
# Install base requirements (boto3 from requirements.txt will be overridden)
RUN pip install --no-cache-dir -r requirements.txt

# Install private SDK (Connect Health Medical Coding gated preview)
COPY backend/botocore-1.43.27-py3-none-any.whl /tmp/
COPY backend/boto3-1.43.27-py3-none-any.whl /tmp/
RUN pip install --no-cache-dir --force-reinstall /tmp/botocore-1.43.27-py3-none-any.whl /tmp/boto3-1.43.27-py3-none-any.whl && rm /tmp/*.whl

# Copy backend application code
COPY backend/server.py .
COPY backend/config.py .
COPY backend/auth.py .
COPY backend/demo_mode.py .
COPY backend/live_transcript.py .
COPY backend/patient_chart.py .
COPY backend/clinical_data_agent.py .
COPY backend/demo_cache/ ./demo_cache/

# Copy frontend (served by Flask as static files from ../frontend)
COPY frontend/ /frontend/

# Copy Act 2 Care Intelligence UI
COPY care-intelligence/ /care-intelligence/

# Expose port
EXPOSE 5000

# Make all files readable, then run as non-root user
RUN chmod -R a+r /app/ /frontend/ /care-intelligence/
RUN adduser --disabled-password --gecos '' appuser
USER appuser

# Health check
HEALTHCHECK --interval=30s --timeout=3s --start-period=10s --retries=3 \
    CMD python -c "import urllib.request; urllib.request.urlopen('http://localhost:5000/api/health')" || exit 1

# Run the server
CMD ["python", "server.py"]
