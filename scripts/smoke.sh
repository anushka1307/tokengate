#!/usr/bin/env bash
# Smoke-test a running gateway: health, readiness, a completion, a stream, and metrics.
#
#   scripts/smoke.sh                          # against http://localhost:8000
#   BASE_URL=http://localhost:8080 METRICS_TOKEN=... scripts/smoke.sh
#
# Exits non-zero on the first failure. Uses the mock model only: no API keys, no cost.
set -euo pipefail

BASE_URL="${BASE_URL:-http://localhost:8000}"
METRICS_TOKEN="${METRICS_TOKEN:-}"
MODEL="${MODEL:-mock-1}"
CURL=(curl --silent --show-error --fail-with-body --max-time 10)

pass() { printf '  ok    %s\n' "$1"; }
fail() { printf '  FAIL  %s\n' "$1" >&2; exit 1; }

echo "Smoke test against ${BASE_URL}"

"${CURL[@]}" "${BASE_URL}/healthz" | grep -q '"ok"' && pass "/healthz" || fail "/healthz"
"${CURL[@]}" "${BASE_URL}/readyz" | grep -q '"ready"' && pass "/readyz" || fail "/readyz"

body=$(printf '{"model":"%s","messages":[{"role":"user","content":"smoke test"}]}' "$MODEL")
headers=$(mktemp)
trap 'rm -f "$headers"' EXIT
reply=$("${CURL[@]}" -D "$headers" "${BASE_URL}/v1/chat/completions" \
  -H 'content-type: application/json' -d "$body") || fail "completion request"
grep -q '"object":"chat.completion"' <<<"$reply" && pass "completion" || fail "completion shape"
grep -qi '^x-request-id:' "$headers" && pass "X-Request-Id header" || fail "X-Request-Id header"
grep -qi '^x-provider:' "$headers" && pass "X-Provider header" || fail "X-Provider header"

stream_body=$(printf '{"model":"%s","stream":true,"messages":[{"role":"user","content":"smoke test"}]}' "$MODEL")
stream=$("${CURL[@]}" -N "${BASE_URL}/v1/chat/completions" \
  -H 'content-type: application/json' -d "$stream_body") || fail "stream request"
grep -q '^data: \[DONE\]' <<<"$stream" && pass "stream ends with [DONE]" || fail "stream [DONE]"

bad=$(curl --silent --max-time 10 -o /dev/null -w '%{http_code}' "${BASE_URL}/v1/chat/completions" \
  -H 'content-type: application/json' -d '{"model":"no-such-model","messages":[{"role":"user","content":"x"}]}')
[[ "$bad" == "400" ]] && pass "unknown model -> 400" || fail "unknown model gave ${bad}"

if [[ -n "$METRICS_TOKEN" ]]; then
  metrics=$("${CURL[@]}" -H "Authorization: Bearer ${METRICS_TOKEN}" "${BASE_URL}/metrics") \
    || fail "/metrics with token"
  grep -q '^tokengate_requests_total' <<<"$metrics" && pass "/metrics" || fail "/metrics content"
  unauth=$(curl --silent --max-time 10 -o /dev/null -w '%{http_code}' "${BASE_URL}/metrics")
  [[ "$unauth" == "401" ]] && pass "/metrics without token -> 401" || fail "/metrics gave ${unauth}"
else
  echo "  skip  /metrics (set METRICS_TOKEN to check it)"
fi

echo "All smoke checks passed."
