#!/usr/bin/env bash
# Smoke test a deployed (or local) SeasonSaathi: one call per feature, PASS/FAIL each.
# Usage: scripts/smoke_test.sh https://seasonsaathi-xxxx.a.run.app   (or http://localhost:8080)
set -uo pipefail
URL="${1:?usage: scripts/smoke_test.sh URL}"
URL="${URL%/}"
PY="${PYTHON:-python3}"
pass=0; fail=0

check() {   # name, python expression on `d` (parsed JSON), curl args...
  local name="$1" expr="$2"; shift 2
  local body code
  body=$(curl -s --max-time 150 -w '\n%{http_code}' "$@")
  code="${body##*$'\n'}"; body="${body%$'\n'*}"
  if [ "$code" = "200" ] && printf '%s' "$body" | "$PY" -c "import json,sys; d=json.load(sys.stdin); sys.exit(0 if ($expr) else 1)" 2>/dev/null; then
    echo "PASS  $name"; pass=$((pass + 1))
  else
    echo "FAIL  $name (HTTP $code): $(printf '%s' "$body" | head -c 300)"; fail=$((fail + 1))
  fi
}

check "GET  /health" "d['status'] == 'ok'" "$URL/health"
check "GET  /api/whoami (vertex + bigquery)" "d['genai_backend'] == 'vertex' and d['data_backend'] in ('bigquery', 'csv')" "$URL/api/whoami"
check "POST /api/f1/interview" "d['type'] in ('question', 'final') and 'completeness_pct' in d" \
  -X POST "$URL/api/f1/interview" -H 'content-type: application/json' \
  -d '{"history":[{"role":"user","text":"We rent 3 acres and grew soybean. We have 2 cows. Our water comes from a well."}],"lang":"en"}'
check "POST /api/f3/HH-DEMO-OK" "d['verifier_status'] in ('PASS', 'FAIL') and d['orchestrator_used'] in ('adk', 'plain') and d['options']" \
  -X POST "$URL/api/f3/HH-DEMO-OK"
check "POST /api/f4/HH-DEMO-LIVESTOCK" "d['applicable'] and d['recommended'] != 'SELL_NOW' and d['options'][0]['cash_needed']['fact_id']" \
  -X POST "$URL/api/f4/HH-DEMO-LIVESTOCK"
check "POST /api/f2/PLOT-HH-DEMO-OK" "d['stress_level'] in ('LOW', 'MED', 'HIGH') and (d['deduped'] or d['next_step'])" \
  -X POST "$URL/api/f2/PLOT-HH-DEMO-OK"
sleep 5   # Pub/Sub push -> /api/alerts/receive
check "GET  /api/alerts?hh_id=HH-DEMO-OK" "isinstance(d, list) and len(d) >= 1" "$URL/api/alerts?hh_id=HH-DEMO-OK"

echo "----"
echo "$pass passed, $fail failed"
[ "$fail" -eq 0 ]
