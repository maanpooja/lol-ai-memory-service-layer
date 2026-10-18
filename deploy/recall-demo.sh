#!/usr/bin/env bash
# Deploy the Recall demo web app (app switcher: ShiftMate, BankBuddy, FiberFix) to Cloud Run.
#
#   deploy/recall-demo.sh role     custom role recallDemoRuntime (Gemini only)
#   deploy/recall-demo.sh sa       service account recall-demo-runtime@ with that role
#   deploy/recall-demo.sh build    Cloud Build: recall_demo/Dockerfile -> Artifact Registry
#   deploy/recall-demo.sh deploy   deploy the image to Cloud Run
#   deploy/recall-demo.sh url      print the service URL
#
# Each example app is a separate organisation with its own Recall memory service
# (deploy/deploy.sh tenant ...): ShiftMate -> memory-mcp, BankBuddy -> memory-mcp-bank,
# FiberFix -> memory-mcp-telecom. Settings come from deploy/deploy.env.
set -euo pipefail

cd "$(dirname "$0")/.."
# shellcheck disable=SC1091
source deploy/deploy.env

: "${PROJECT_ID:?}" "${REGION:?}" "${OIDC_ISSUER:?}" "${OIDC_AUDIENCE:?}"
APP=recall-demo
SA_EMAIL="${APP}-runtime@${PROJECT_ID}.iam.gserviceaccount.com"
ROLE_ID=recallDemoRuntime
IMAGE="${REGION}-docker.pkg.dev/${PROJECT_ID}/cloud-run-source-deploy/${APP}:latest"
NUMBER="$(gcloud projects describe "$PROJECT_ID" --format 'value(projectNumber)')"
memory_url() { echo "https://$1-${NUMBER}.${REGION}.run.app/mcp"; }

role() {
  if gcloud iam roles describe "$ROLE_ID" --project "$PROJECT_ID" >/dev/null 2>&1; then
    gcloud iam roles update "$ROLE_ID" --project "$PROJECT_ID" --file deploy/recall-demo-role.yaml --quiet
  else
    gcloud iam roles create "$ROLE_ID" --project "$PROJECT_ID" --file deploy/recall-demo-role.yaml
  fi
}

sa() {
  gcloud iam service-accounts describe "$SA_EMAIL" --project "$PROJECT_ID" >/dev/null 2>&1 ||
    gcloud iam service-accounts create "${APP}-runtime" --project "$PROJECT_ID" \
      --display-name "Recall demo Cloud Run runtime (Gemini only)"
  gcloud projects add-iam-policy-binding "$PROJECT_ID" --member "serviceAccount:${SA_EMAIL}" \
    --role "projects/${PROJECT_ID}/roles/${ROLE_ID}" --condition None --quiet >/dev/null
  echo "bound ${ROLE_ID} to ${SA_EMAIL}"
}

build() {
  gcloud builds submit --project "$PROJECT_ID" --region "$REGION" \
    --config deploy/recall-demo-cloudbuild.yaml --substitutions "_IMAGE=${IMAGE}" .
}

deploy() {
  : "${AUTH0_DOMAIN:?set AUTH0_DOMAIN in deploy/deploy.env}" "${AUTH0_SPA_CLIENT_ID:?set AUTH0_SPA_CLIENT_ID in deploy/deploy.env}"
  # Public page; /api/chat requires a valid Auth0 token. One instance (sessions live in
  # memory) kept warm for the evaluation window.
  gcloud run deploy "$APP" --project "$PROJECT_ID" --region "$REGION" --image "$IMAGE" --quiet \
    --service-account "$SA_EMAIL" --allow-unauthenticated \
    --cpu 1 --memory 1Gi --min-instances "${RECALL_DEMO_MIN_INSTANCES:-1}" --max-instances 1 --concurrency 40 --timeout 180 \
    --set-env-vars "^@^GOOGLE_GENAI_USE_VERTEXAI=TRUE@GOOGLE_CLOUD_PROJECT=${PROJECT_ID}@GOOGLE_CLOUD_LOCATION=global@RECALL_DEMO_MODEL=${RECALL_DEMO_MODEL:-gemini-3.5-flash}@RECALL_DEMO_THINKING_LEVEL=${RECALL_DEMO_THINKING_LEVEL:-MINIMAL}@MEMORY_MCP_URL_SHIFTMATE=$(memory_url memory-mcp)@MEMORY_MCP_URL_BANKBUDDY=$(memory_url memory-mcp-bank)@MEMORY_MCP_URL_FIBERFIX=$(memory_url memory-mcp-telecom)@OIDC_ISSUER=${OIDC_ISSUER}@OIDC_AUDIENCE=${OIDC_AUDIENCE}@AUTH0_DOMAIN=${AUTH0_DOMAIN}@AUTH0_SPA_CLIENT_ID=${AUTH0_SPA_CLIENT_ID}"
}

url() { echo "https://${APP}-${NUMBER}.${REGION}.run.app"; }

"${1:?usage: deploy/recall-demo.sh role|sa|build|deploy|url}"
