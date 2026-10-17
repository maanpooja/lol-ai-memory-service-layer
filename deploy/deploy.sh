#!/usr/bin/env bash
# Deploy memory-mcp to Cloud Run with a least-privilege runtime service account,
# and route its audit lines to BigQuery through a Cloud Logging sink.
#
# Each step is separate and safe to rerun:
#   deploy/deploy.sh apis      enable the APIs
#   deploy/deploy.sh role      create/update the custom runtime role
#   deploy/deploy.sh sa        create the runtime service account and bind its roles
#   deploy/deploy.sh deploy    build from source and deploy the Cloud Run service
#   deploy/deploy.sh audit     create the BigQuery dataset and the log sink
#   deploy/deploy.sh url       print the service URL
#
# Settings come from deploy/deploy.env (copy deploy/deploy.env.example).
set -euo pipefail

cd "$(dirname "$0")/.."
# shellcheck disable=SC1091
source deploy/deploy.env

: "${PROJECT_ID:?}" "${REGION:?}" "${SERVICE:?}" "${OIDC_ISSUER:?}" "${OIDC_AUDIENCE:?}" "${TENANT_ID:?}"
SA_NAME="${SERVICE}-runtime"
SA_EMAIL="${SA_NAME}@${PROJECT_ID}.iam.gserviceaccount.com"
ROLE_ID="memoryMcpRuntime"
DATASET="${AUDIT_DATASET:-memory_audit}"
SINK="${SERVICE}-audit"

apis() {
  gcloud services enable --project "$PROJECT_ID" \
    run.googleapis.com cloudbuild.googleapis.com artifactregistry.googleapis.com \
    discoveryengine.googleapis.com dlp.googleapis.com aiplatform.googleapis.com \
    logging.googleapis.com bigquery.googleapis.com iam.googleapis.com
}

role() {
  # Exactly what the server calls: document CRUD and search in Discovery Engine,
  # Gemini generateContent. DLP comes from roles/dlp.user (content API only).
  if gcloud iam roles describe "$ROLE_ID" --project "$PROJECT_ID" >/dev/null 2>&1; then
    gcloud iam roles update "$ROLE_ID" --project "$PROJECT_ID" --file deploy/runtime-role.yaml --quiet
  else
    gcloud iam roles create "$ROLE_ID" --project "$PROJECT_ID" --file deploy/runtime-role.yaml
  fi
}

sa() {
  gcloud iam service-accounts describe "$SA_EMAIL" --project "$PROJECT_ID" >/dev/null 2>&1 ||
    gcloud iam service-accounts create "$SA_NAME" --project "$PROJECT_ID" \
      --display-name "memory-mcp Cloud Run runtime (least privilege)"
  for role in "projects/${PROJECT_ID}/roles/${ROLE_ID}" roles/dlp.user; do
    gcloud projects add-iam-policy-binding "$PROJECT_ID" --member "serviceAccount:${SA_EMAIL}" \
      --role "$role" --condition None --quiet >/dev/null
    echo "bound $role to $SA_EMAIL"
  done
}

deploy() {
  local server_url
  server_url="${SERVER_URL:-$(url 2>/dev/null || true)}"
  # The app does its own OIDC bearer auth (Auth0 tokens), so Cloud Run IAM must let
  # requests through; /mcp still answers 401 without a valid token.
  gcloud run deploy "$SERVICE" --project "$PROJECT_ID" --region "$REGION" --source . --quiet \
    --service-account "$SA_EMAIL" --allow-unauthenticated \
    --cpu 1 --memory 512Mi --min-instances 0 --max-instances "${MAX_INSTANCES:-3}" --timeout 120 \
    --set-env-vars "^@^PROJECT_ID=${PROJECT_ID}@DE_LOCATION=${DE_LOCATION:-global}@DATA_STORE_ID=${DATA_STORE_ID:-memories}@ENGINE_ID=${ENGINE_ID:-memories-app}@GEMINI_MODEL=${GEMINI_MODEL:-gemini-3.5-flash}@GEMINI_LOCATION=${GEMINI_LOCATION:-global}@GEMINI_THINKING_LEVEL=${GEMINI_THINKING_LEVEL:-MINIMAL}@DLP_LOCATION=${DLP_LOCATION:-global}@AUTH_MODE=oidc@OIDC_ISSUER=${OIDC_ISSUER}@OIDC_AUDIENCE=${OIDC_AUDIENCE}@TENANT_ID=${TENANT_ID}@SERVER_URL=${server_url}"
}

audit() {
  bq --project_id "$PROJECT_ID" show "$DATASET" >/dev/null 2>&1 ||
    bq --project_id "$PROJECT_ID" mk --dataset --location "${BQ_LOCATION:-US}" \
      --description "memory-mcp audit lines (ids and counters only, no content)" "$DATASET"
  local filter="resource.type=\"cloud_run_revision\" AND resource.labels.service_name=\"${SERVICE}\" AND jsonPayload.event=\"tool_call\""
  if ! gcloud logging sinks describe "$SINK" --project "$PROJECT_ID" >/dev/null 2>&1; then
    gcloud logging sinks create "$SINK" "bigquery.googleapis.com/projects/${PROJECT_ID}/datasets/${DATASET}" \
      --project "$PROJECT_ID" --log-filter "$filter" --use-partitioned-tables
  fi
  local writer
  writer="$(gcloud logging sinks describe "$SINK" --project "$PROJECT_ID" --format 'value(writerIdentity)')"
  # The sink's writer may only write to this one dataset: a WRITER entry in its access list.
  # (bq add-iam-policy-binding on datasets needs allowlisting, so edit the access list instead.)
  local access
  access="$(mktemp)"
  bq --project_id "$PROJECT_ID" show --format=prettyjson "$DATASET" |
    python3 -c 'import json,sys; d=json.load(sys.stdin); w=sys.argv[1].split(":",1)[1]; a=d.setdefault("access",[]); e={"role":"WRITER","userByEmail":w}; a.append(e) if e not in a else None; json.dump({"access":a},sys.stdout)' "$writer" >"$access"
  bq --project_id "$PROJECT_ID" update --source "$access" "$DATASET"
  rm -f "$access"
}

url() {
  gcloud run services describe "$SERVICE" --project "$PROJECT_ID" --region "$REGION" --format 'value(status.url)'
}

"${1:?usage: deploy/deploy.sh apis|role|sa|deploy|audit|url}"
