#!/usr/bin/env bash
# Deploy SeasonSaathi to Cloud Run with the P6 GCP backends (BigQuery, Cloud Storage, Pub/Sub, Vertex AI).
# Infra (service account, dataset, bucket, topic) must already exist; this script only builds and deploys.
#
#   REGION                 Cloud Run + BigQuery dataset region (e.g. asia-south1)
#   VERTEX_LOCATION        Vertex AI endpoint for Gemini; default "global" (gemini-3.5-flash is only served on
#                          the global endpoint for this project: a regional value gave 404). Never set it to REGION
#                          unless that region serves GEMINI_MODEL.
#   DATA_BACKEND           bigquery (default) | csv (emergency fallback: read-only bundled CSVs)
# No API keys are deployed: .env is excluded (.gcloudignore/.dockerignore) and only the variables below are set.
set -euo pipefail
if [ -f .env ]; then set -a; . ./.env; set +a; fi     # reads GEMINI_MODEL, EMBED_MODEL, GOOGLE_CLOUD_PROJECT

PROJECT="${PROJECT:-${GOOGLE_CLOUD_PROJECT:?set PROJECT}}"
REGION="${REGION:?set REGION, e.g. export REGION=asia-south1}"
VERTEX_LOCATION="${VERTEX_LOCATION:-global}"
SA="${SA:-seasonsaathi-run@${PROJECT}.iam.gserviceaccount.com}"
GCS_BUCKET="${GCS_BUCKET:-${PROJECT}-seasonsaathi}"
DATA_BACKEND="${DATA_BACKEND_DEPLOY:-bigquery}"

ENV_VARS="GENAI_MODE=vertex,GOOGLE_GENAI_USE_VERTEXAI=TRUE,GOOGLE_CLOUD_PROJECT=${PROJECT}"
ENV_VARS+=",GOOGLE_CLOUD_LOCATION=${VERTEX_LOCATION},GEMINI_MODEL=${GEMINI_MODEL:?},EMBED_MODEL=${EMBED_MODEL:?}"
ENV_VARS+=",DATA_BACKEND=${DATA_BACKEND},BQ_DATASET=seasonsaathi,BQ_LOCATION=${REGION},ORCHESTRATOR=adk"
ENV_VARS+=",STORAGE_BACKEND=gcs,GCS_BUCKET=${GCS_BUCKET},ALERTS_MODE=pubsub,PUBSUB_TOPIC=stress-alerts"
ENV_VARS+=",LLM_CACHE=disk,LLM_CACHE_DIR=/tmp/llm-cache,ENGINE_TIMEOUT_S=${ENGINE_TIMEOUT_S:-20}"

gcloud run deploy seasonsaathi --source . --project "$PROJECT" --region "$REGION" \
  --allow-unauthenticated --service-account "$SA" \
  --memory 1Gi --cpu 1 --timeout "${TIMEOUT:-120}" --max-instances 2 --min-instances "${MIN_INSTANCES:-0}" \
  --set-env-vars "$ENV_VARS"
