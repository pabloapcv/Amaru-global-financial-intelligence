#!/usr/bin/env bash
# End-to-end smoke test of the compose stack: TimescaleDB + API + web.
# Starts the stack, creates a user, signs in, ingests real data, runs research through the
# API, downloads the report and checks the web app. Usage: infrastructure/docker/smoke-test.sh
set -euo pipefail
cd "$(dirname "$0")/../.."
COMPOSE="docker compose -f infrastructure/docker/docker-compose.yml"
API=http://localhost:8010
EMAIL=smoke@example.test
PASSWORD="smoke-test-$(date +%s)-pass"
JAR=$(mktemp)
trap 'rm -f "$JAR"' EXIT

echo "== start db, api, web"
POSTGRES_PASSWORD=smoke $COMPOSE up -d --build --wait db api web

echo "== api health"
curl -sf "$API/health" | tee /dev/stderr | grep -q '"auth_required":true'

echo "== create user and sign in"
echo "$PASSWORD" | $COMPOSE exec -T api atlas create-user "$EMAIL" --workspace local --plan pro --password-stdin
curl -sf -c "$JAR" -H 'content-type: application/json' \
  -d "{\"email\":\"$EMAIL\",\"password\":\"$PASSWORD\"}" "$API/api/v1/auth/login" > /dev/null
test "$(curl -s -o /dev/null -w '%{http_code}' "$API/api/v1/instruments")" = 401   # no cookie -> rejected
curl -sf -b "$JAR" "$API/api/v1/account" | grep -q "$EMAIL"

echo "== ingest (real FRED + CFTC data)"
$COMPOSE exec -T api atlas ingest CMD.SUGAR.ISA FX.USDBRL CMD.CRUDE.BRENT COT.SUGAR11 | grep -v '^INFO' || true

echo "== research through the API"
RUN=$(curl -sf -b "$JAR" -H 'content-type: application/json' \
  -d '{"query":"Is sugar positioning crowded, and what are the drivers?","engine":"deterministic","depth":"standard"}' \
  "$API/api/v1/research" | python3 -c 'import json,sys; print(json.load(sys.stdin)["run_id"])')
for i in $(seq 1 60); do
  STATUS=$(curl -sf -b "$JAR" "$API/api/v1/research/$RUN" | python3 -c 'import json,sys; print(json.load(sys.stdin)["status"])')
  [ "$STATUS" = complete ] && break
  [ "$STATUS" = failed ] && { curl -s -b "$JAR" "$API/api/v1/research/$RUN"; exit 1; }
  sleep 2
done
test "$STATUS" = complete
curl -sf -b "$JAR" "$API/api/v1/research/$RUN" | python3 -c '
import json, sys
r = json.load(sys.stdin)["result"]
v = r["verification"]
print("engine=%s evidence=%d verified=%d/%d" % (r["engine"], len(r["evidence"]), v["numbers_verified"], v["numbers_checked"]))
assert v["passed"], v["unverified_numbers"]
open("/tmp/report_id", "w").write(r["report_id"])'
curl -sf -b "$JAR" "$API/api/v1/reports/$(cat /tmp/report_id)/download?format=html" | grep -q "Appendix A"
curl -sf -b "$JAR" -o /tmp/report.xlsx "$API/api/v1/reports/$(cat /tmp/report_id)/download?format=xlsx"
head -c 2 /tmp/report.xlsx | grep -q PK

echo "== metrics and web"
curl -sf "$API/metrics" | grep -q 'atlas_research_runs{status="complete"'
curl -sf http://localhost:3010/login | grep -q "ATLAS"

echo "SMOKE TEST PASSED"
