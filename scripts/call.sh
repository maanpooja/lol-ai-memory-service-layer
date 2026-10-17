#!/usr/bin/env bash
# Call one memory-mcp tool on the deployed server and print its result.
#
# Usage:
#   scripts/call.sh TOKEN TOOL 'JSON_ARGUMENTS'
#   scripts/call.sh "$ALICE" list_memories '{}'
#   scripts/call.sh "$ALICE" search_memory '{"query":"coffee"}'
#
# MCP_URL overrides the server (default: the Cloud Run service).
set -uo pipefail

URL="${MCP_URL:-https://memory-mcp-140715294513.us-central1.run.app}"
TOKEN="${1:-}"
TOOL="${2:?usage: scripts/call.sh TOKEN TOOL 'JSON_ARGUMENTS'}"
ARGS="${3:-}"
[ -n "$ARGS" ] || ARGS="{}"

if [ -z "$TOKEN" ]; then
  echo "No token given. Sign in first: ALICE=\$(uv run --group agent python agent/auth0_login.py --print)" >&2
  exit 1
fi

body="$(mktemp)"
status=$(curl -s -o "$body" -w "%{http_code}" -X POST "$URL/mcp" \
  -H "Authorization: Bearer $TOKEN" \
  -H "Content-Type: application/json" -H "Accept: application/json, text/event-stream" \
  -d "{\"jsonrpc\":\"2.0\",\"id\":1,\"method\":\"tools/call\",\"params\":{\"name\":\"$TOOL\",\"arguments\":$ARGS}}")

if [ "$status" != "200" ]; then
  echo "HTTP $status" >&2
  [ "$status" = "401" ] && echo "Token missing, invalid or expired. Sign in again." >&2
  rm -f "$body"
  exit 1
fi

sed -n 's/^data: //p' "$body" | python3 -c '
import json, sys
response = json.load(sys.stdin)
if "error" in response:
    print("ERROR:", response["error"].get("message")); sys.exit(1)
result = response["result"]
if result.get("isError"):
    print("ERROR:", result["content"][0]["text"]); sys.exit(1)
print(json.dumps(result.get("structuredContent"), indent=2, ensure_ascii=False))
'
code=$?
rm -f "$body"
exit $code
