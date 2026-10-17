#!/usr/bin/env bash
# Deploy the ShiftMate web app (chat page + ADK agent) to Cloud Run.
#
#   deploy/shiftmate.sh role     custom role shiftmateRuntime (Gemini only)
#   deploy/shiftmate.sh sa       service account shiftmate-runtime@ with that role
#   deploy/shiftmate.sh build    Cloud Build: shiftmate/Dockerfile -> Artifact Registry
#   deploy/shiftmate.sh deploy   deploy the image to Cloud Run
#   deploy/shiftmate.sh url      print the service URL
#
# Settings: deploy/deploy.env (shared with the memory server) plus AUTH0_DOMAIN and
# AUTH0_SPA_CLIENT_ID. The memory server URL is SERVER_URL from deploy.env.
set -euo pipefail

cd "$(dirname "$0")/.."
# shellcheck disable=SC1091
source deploy/deploy.env

: "${PROJECT_ID:?}" "${REGION:?}" "${OIDC_ISSUER:?}" "${OIDC_AUDIENCE:?}" "${SERVER_URL:?}"
APP=shiftmate
SA_EMAIL="${APP}-runtime@${PROJECT_ID}.iam.gserviceaccount.com"
ROLE_ID=shiftmateRuntime
IMAGE="${REGION}-docker.pkg.dev/${PROJECT_ID}/cloud-run-source-deploy/${APP}:latest"

role() {
  if gcloud iam roles describe "$ROLE_ID" --project "$PROJECT_ID" >/dev/null 2>&1; then
    gcloud iam roles update "$ROLE_ID" --project "$PROJECT_ID" --file deploy/shiftmate-role.yaml --quiet
  else
    gcloud iam roles create "$ROLE_ID" --project "$PROJECT_ID" --file deploy/shiftmate-role.yaml
  fi
}

sa() {
  gcloud iam service-accounts describe "$SA_EMAIL" --project "$PROJECT_ID" >/dev/null 2>&1 ||
    gcloud iam service-accounts create "${APP}-runtime" --project "$PROJECT_ID" \
      --display-name "ShiftMate Cloud Run runtime (Gemini only)"
  gcloud projects add-iam-policy-binding "$PROJECT_ID" --member "serviceAccount:${SA_EMAIL}" \
    --role "projects/${PROJECT_ID}/roles/${ROLE_ID}" --condition None --quiet >/dev/null
  echo "bound ${ROLE_ID} to ${SA_EMAIL}"
}

build() {
  gcloud builds submit --project "$PROJECT_ID" --region "$REGION" \
    --config deploy/shiftmate-cloudbuild.yaml --substitutions "_IMAGE=${IMAGE}" .
}

deploy() {
  : "${AUTH0_DOMAIN:?set AUTH0_DOMAIN in deploy/deploy.env}" "${AUTH0_SPA_CLIENT_ID:?set AUTH0_SPA_CLIENT_ID in deploy/deploy.env}"
  # Public page; /api/chat requires a valid Auth0 token. One instance (sessions live in
  # memory) kept warm for the evaluation window.
  gcloud run deploy "$APP" --project "$PROJECT_ID" --region "$REGION" --image "$IMAGE" --quiet \
    --service-account "$SA_EMAIL" --allow-unauthenticated \
    --cpu 1 --memory 1Gi --min-instances "${SHIFTMATE_MIN_INSTANCES:-1}" --max-instances 1 --concurrency 40 --timeout 180 \
    --set-env-vars "^@^GOOGLE_GENAI_USE_VERTEXAI=TRUE@GOOGLE_CLOUD_PROJECT=${PROJECT_ID}@GOOGLE_CLOUD_LOCATION=global@SHIFTMATE_MODEL=${SHIFTMATE_MODEL:-gemini-3.5-flash}@SHIFTMATE_THINKING_LEVEL=${SHIFTMATE_THINKING_LEVEL:-MINIMAL}@MEMORY_MCP_URL=${SERVER_URL}/mcp@OIDC_ISSUER=${OIDC_ISSUER}@OIDC_AUDIENCE=${OIDC_AUDIENCE}@AUTH0_DOMAIN=${AUTH0_DOMAIN}@AUTH0_SPA_CLIENT_ID=${AUTH0_SPA_CLIENT_ID}"
}

url() {
  echo "https://${APP}-$(gcloud projects describe "$PROJECT_ID" --format 'value(projectNumber)').${REGION}.run.app"
}

"${1:?usage: deploy/shiftmate.sh role|sa|build|deploy|url}"
