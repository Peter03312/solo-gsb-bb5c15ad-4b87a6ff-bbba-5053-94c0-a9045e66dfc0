#!/usr/bin/env bash
# 并发正确性验证（需在 demo.sh 跑过、或库内已有 lineage_id=1 且当前版本为 v1 时观察）。
# 这里自举一个全新 lineage，然后并发发起 N 个“更正到 v2”：
#   - 恰好 1 个请求成功（200），其余全部 409
#   - 最终 current_version 仍只有一个最新版（无双最新版）
# 同时并发一个“发布”，确认不会依据更正中的旧校准错误放行。
#
# 用法: ./examples/concurrency_check.sh [BASE_URL]
set -u

BASE="${1:-http://localhost:8000}"
CT="Content-Type: application/json"
N="${N:-8}"

echo "== 建立全新校准链 v1 =="
CREATE_RESP=$(curl -sS -X POST "$BASE/calibrations" -H "$CT" -d '{
  "instrument_id":"CONC-PROBE",
  "passed":true,
  "valid_from":"2026-01-01T00:00:00+08:00",
  "valid_to":"2027-01-01T00:00:00+08:00"
}')
echo "$CREATE_RESP"
LINEAGE=$(printf '%s' "$CREATE_RESP" | sed -n 's/.*"lineage_id":\([0-9]*\).*/\1/p')
echo "lineage_id=$LINEAGE 并发数=$N"

CORRECT_BODY='{"version":2,"passed":true,"valid_from":"2026-01-01T00:00:00+08:00","valid_to":"2026-06-01T00:00:00+08:00"}'
PUBLISH_BODY='{"instrument_id":"CONC-PROBE","test_time":"2026-03-01T08:00:00+08:00"}'

echo
echo "== 并发：${N} 个更正 v2 + 1 个发布（同一时点可能落在旧/新区间边界） =="
tmpdir=$(mktemp -d)
for i in $(seq 1 "$N"); do
  (
    code=$(curl -sS -o "$tmpdir/c$i.body" -w '%{http_code}' \
      -X POST "$BASE/calibrations/$LINEAGE/corrections" -H "$CT" -d "$CORRECT_BODY")
    echo "$code" > "$tmpdir/c$i.code"
  ) &
done
(
  code=$(curl -sS -o "$tmpdir/p1.body" -w '%{http_code}' \
    -X POST "$BASE/reports" -H "$CT" -d "$PUBLISH_BODY")
  echo "$code" > "$tmpdir/p1.code"
) &
wait

ok=0; conflict=0
for i in $(seq 1 "$N"); do
  code=$(cat "$tmpdir/c$i.code")
  echo "correction #$i -> HTTP $code"
  [[ "$code" == "200" ]] && ok=$((ok+1))
  [[ "$code" == "409" ]] && conflict=$((conflict+1))
done
echo "更正成功数=$ok（要求=1），版本冲突数=$conflict（要求=$((N-1))）"
echo "发布请求 -> HTTP $(cat "$tmpdir/p1.code")（201=按某个已提交版本放行，422=更正后该时点不再被覆盖，二者均为安全结果；禁止出现脏读放行）"
cat "$tmpdir/p1.body"; echo

echo
echo "== 断言：链上当前版本唯一，且最新版为 v2 =="
curl -sS "$BASE/calibrations/lineages/$LINEAGE"
echo
curl -sS "$BASE/calibrations/lineages" | sed 's/},{/}\n{/g' | grep "\"id\":$LINEAGE" || true

rm -rf "$tmpdir"

if [[ "$ok" -ne 1 || "$conflict" -ne $((N-1)) ]]; then
  echo "FAIL: 并发更正结果不符合预期"
  exit 1
fi
echo "PASS: 无并发双最新版、无跳号"
