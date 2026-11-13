#!/usr/bin/env bash
# Smoke-test a running gateway: health, readiness, a completion, a stream, and metrics.
#
#   scripts/smoke.sh                          # against http://localhost:8000, auth disabled
#   BASE_URL=http://localhost:8080 API_KEY=tg_... METRICS_TOKEN=... scripts/smoke.sh
#   CHECK_RATE_LIMIT=1 API_KEY=tg_... scripts/smoke.sh   # also send requests until a 429
#
# Exits non-zero on the first failure. Uses the mock model only: no provider keys, no cost.
# CHECK_RATE_LIMIT is for test deployments with a low limit; don't point it at real traffic.
set -euo pipefail

BASE_URL="${BASE_URL:-http://localhost:8000}"
METRICS_TOKEN="${METRICS_TOKEN:-}"
MODEL="${MODEL:-mock-1}"
API_KEY="${API_KEY:-}"
CHECK_RATE_LIMIT="${CHECK_RATE_LIMIT:-0}"
CURL=(curl --silent --show-error --fail-with-body --max-time 10)
# The key goes in a header read from stdin (-H @-), so it never shows up in `ps` output.
AUTH=()
if [[ -n "$API_KEY" ]]; then
  AUTH=(-H @-)
fi
auth_header() { [[ -n "$API_KEY" ]] && printf 'Authorization: Bearer %s\n' "$API_KEY" || true; }

pass() { printf '  ok    %s\n' "$1"; }
fail() { printf '  FAIL  %s\n' "$1" >&2; exit 1; }

echo "Smoke test against ${BASE_URL}"

"${CURL[@]}" "${BASE_URL}/healthz" | grep -q '"ok"' && pass "/healthz" || fail "/healthz"
"${CURL[@]}" "${BASE_URL}/readyz" | grep -q '"ready"' && pass "/readyz" || fail "/readyz"

body=$(printf '{"model":"%s","messages":[{"role":"user","content":"smoke test"}]}' "$MODEL")
headers=$(mktemp)
trap 'rm -f "$headers"' EXIT
reply=$(auth_header | "${CURL[@]}" -D "$headers" ${AUTH[@]+"${AUTH[@]}"} "${BASE_URL}/v1/chat/completions" \
  -H 'content-type: application/json' -d "$body") || fail "completion request"
grep -q '"object":"chat.completion"' <<<"$reply" && pass "completion" || fail "completion shape"
grep -qi '^x-request-id:' "$headers" && pass "X-Request-Id header" || fail "X-Request-Id header"
grep -qi '^x-provider:' "$headers" && pass "X-Provider header" || fail "X-Provider header"
if [[ -n "$API_KEY" ]]; then
  grep -qi '^x-ratelimit-remaining:' "$headers" && pass "X-RateLimit-Remaining header" \
    || fail "X-RateLimit-Remaining header"
  nokey=$(curl --silent --max-time 10 -o /dev/null -w '%{http_code}' \
    "${BASE_URL}/v1/chat/completions" -H 'content-type: application/json' -d "$body")
  [[ "$nokey" == "401" ]] && pass "no API key -> 401" || fail "no API key gave ${nokey}"
fi

stream_body=$(printf '{"model":"%s","stream":true,"messages":[{"role":"user","content":"smoke test"}]}' "$MODEL")
stream=$(auth_header | "${CURL[@]}" -N ${AUTH[@]+"${AUTH[@]}"} "${BASE_URL}/v1/chat/completions" \
  -H 'content-type: application/json' -d "$stream_body") || fail "stream request"
grep -q '^data: \[DONE\]' <<<"$stream" && pass "stream ends with [DONE]" || fail "stream [DONE]"

bad=$(auth_header | curl --silent --max-time 10 -o /dev/null -w '%{http_code}' ${AUTH[@]+"${AUTH[@]}"} \
  "${BASE_URL}/v1/chat/completions" \
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

if [[ "$CHECK_RATE_LIMIT" == "1" ]]; then
  [[ -n "$API_KEY" ]] || fail "CHECK_RATE_LIMIT needs API_KEY"
  limited=""
  for _ in $(seq 1 50); do
    code=$(auth_header | curl --silent --max-time 10 -D "$headers" -o /dev/null \
      -w '%{http_code}' ${AUTH[@]+"${AUTH[@]}"} "${BASE_URL}/v1/chat/completions" \
      -H 'content-type: application/json' -d "$body")
    if [[ "$code" == "429" ]]; then limited=1; break; fi
    [[ "$code" == "200" ]] || fail "expected 200 or 429 while filling the bucket, got ${code}"
  done
  [[ -n "$limited" ]] && pass "rate limit -> 429" || fail "no 429 after 50 requests"
  grep -qiE '^retry-after: [1-9][0-9]*' "$headers" && pass "Retry-After header" \
    || fail "Retry-After header"
fi

echo "All smoke checks passed."
