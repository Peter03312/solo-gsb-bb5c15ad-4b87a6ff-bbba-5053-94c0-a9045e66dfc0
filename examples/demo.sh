#!/usr/bin/env bash
# 端到端演示：登记校准 -> 发布 -> 更正 -> 漂移重算
# 用法: ./examples/demo.sh [BASE_URL]
set -euo pipefail

BASE="${1:-http://localhost:8000}"
CT="Content-Type: application/json"

req() {  # req <METHOD> <PATH> [JSON_BODY]
  local method="$1" path="$2" body="${3:-}"
  echo; echo ">>> $method $path ${body:+-d $body}"
  if [[ -n "$body" ]]; then
    curl -sS -X "$method" "$BASE$path" -H "$CT" -d "$body" \
      -w '\n[HTTP %{http_code}]\n'
  else
    curl -sS -X "$method" "$BASE$path" -w '\n[HTTP %{http_code}]\n'
  fi
}

echo "== 0. 健康检查 =="
req GET /health

echo; echo "== 1. 登记首版校准 v1（通过，覆盖 2026 全年） =="
req POST /calibrations '{"instrument_id":"LCMS-01","passed":true,"valid_from":"2026-01-01T00:00:00+08:00","valid_to":"2027-01-01T00:00:00+08:00","note":"年度校准 v1"}'

echo; echo "== 2. 区间重叠登记（预期 409 interval_conflict） =="
req POST /calibrations '{"instrument_id":"LCMS-01","passed":true,"valid_from":"2026-06-01T00:00:00+08:00","valid_to":"2026-08-01T00:00:00+08:00"}'

echo; echo "== 3. 发布报告（检测时点 2026-06-01，预期 201，依据 v1） =="
req POST /reports '{"instrument_id":"LCMS-01","test_time":"2026-06-01T10:00:00+08:00"}'

echo; echo "== 4. 无覆盖时点发布（预期 422 no_coverage） =="
req POST /reports '{"instrument_id":"LCMS-01","test_time":"2025-12-31T23:59:59+08:00"}'

echo; echo "== 5. 更正为 v2：区间缩短到 5 月前（6 月时点失去覆盖） =="
req POST /calibrations/1/corrections '{"version":2,"passed":true,"valid_from":"2026-01-01T00:00:00+08:00","valid_to":"2026-05-01T00:00:00+08:00","note":"搬迁后重校，v1 区间更正缩短"}'

echo; echo "== 6. 跳号更正 v9（预期 409 version_conflict） =="
req POST /calibrations/1/corrections '{"version":9,"passed":true,"valid_from":"2026-01-01T00:00:00+08:00","valid_to":"2027-01-01T00:00:00+08:00"}'

echo; echo "== 7. 历史版本可查（v1 保留、v2 最新） =="
req GET /calibrations/lineages/1

echo; echo "== 8. 重算漂移：报告快照 v1 仍通过，但当前已无覆盖 =="
req GET '/reports/recompute-drift?changed_only=true'

echo; echo "== 9. 已发布报告快照不被改写（basis.version 仍为 1） =="
req GET /reports/1
