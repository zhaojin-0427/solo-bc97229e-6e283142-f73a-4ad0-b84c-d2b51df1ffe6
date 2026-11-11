#!/usr/bin/env bash
# 端到端演示：工时入仓 -> 未归属隔离 -> 人事补录 -> 后台自动转正 -> 下游读取正式工时。
# 用法：
#   docker compose up -d --build       # 先启动服务
#   ./scripts/demo.sh                  # 本机演示（默认 BASE=http://localhost:8000）
set -euo pipefail

BASE="${BASE:-http://localhost:8000}"

pretty() {
  if command -v jq >/dev/null 2>&1; then jq .;
  elif command -v python3 >/dev/null 2>&1; then python3 -m json.tool;
  else cat; fi
}

post() { # $1=path  $2=json body
  curl -sS -X POST "${BASE}$1" -H 'Content-Type: application/json' -d "$2"
}

echo "==================================================================="
echo "0. 健康检查  GET /health"
echo "==================================================================="
curl -sS "${BASE}/health" | pretty
echo

echo "==================================================================="
echo "1. 人事初始归属（修订 1）：E001/E002 有归属，E003 暂无"
echo "==================================================================="
post /api/revisions '{
  "note": "HR 初始归属 2026Q1",
  "entries": [
    {"employee_id": "E001", "cost_center": "CC-SH-01",
     "effective_from": "2026-01-01", "effective_to": "2026-04-01"},
    {"employee_id": "E002", "cost_center": "CC-BJ-01",
     "effective_from": "2026-01-01", "effective_to": "2026-04-01"}
  ]
}' | pretty
echo

echo "==================================================================="
echo "2. 导入工时批次 TS-2026-02-001（3 行：2 行已归属入正式表，E003 进隔离区）"
echo "   导入时固定当前归属修订号（=1），少数员工缺失归属不阻塞整批入仓"
echo "==================================================================="
post /api/timesheet-batches '{
  "batch_no": "TS-2026-02-001",
  "entries": [
    {"timesheet_id": "T-001", "employee_id": "E001", "work_date": "2026-02-10", "hours": 8},
    {"timesheet_id": "T-002", "employee_id": "E002", "work_date": "2026-02-10", "hours": 7.5},
    {"timesheet_id": "T-003", "employee_id": "E003", "work_date": "2026-02-10", "hours": 6}
  ]
}' | pretty
echo

echo "==================================================================="
echo "3. 同批同内容重传 -> 返回原批回执（HTTP 200, replayed=true），不重复入仓"
echo "==================================================================="
curl -sS -o /tmp/demo_replay.json -w 'HTTP %{http_code}\n' \
  -X POST "${BASE}/api/timesheet-batches" -H 'Content-Type: application/json' -d '{
  "batch_no": "TS-2026-02-001",
  "entries": [
    {"timesheet_id": "T-001", "employee_id": "E001", "work_date": "2026-02-10", "hours": 8},
    {"timesheet_id": "T-002", "employee_id": "E002", "work_date": "2026-02-10", "hours": 7.5},
    {"timesheet_id": "T-003", "employee_id": "E003", "work_date": "2026-02-10", "hours": 6}
  ]
}'
pretty < /tmp/demo_replay.json
echo

echo "==================================================================="
echo "4. 同批号异内容重传 -> 拒绝（HTTP 409 BATCH_CONTENT_MISMATCH）"
echo "==================================================================="
curl -sS -o /tmp/demo_mismatch.json -w 'HTTP %{http_code}\n' \
  -X POST "${BASE}/api/timesheet-batches" -H 'Content-Type: application/json' -d '{
  "batch_no": "TS-2026-02-001",
  "entries": [
    {"timesheet_id": "T-001", "employee_id": "E001", "work_date": "2026-02-10", "hours": 9}
  ]
}'
pretty < /tmp/demo_mismatch.json
echo

echo "==================================================================="
echo "5. 查询批次明细：正式 2 行 + 隔离 1 行（PENDING，等待补录）"
echo "==================================================================="
curl -sS "${BASE}/api/timesheet-batches/TS-2026-02-001" | pretty
echo

echo "==================================================================="
echo "6. 人事补录（修订 2）：E003 归属 CC-GZ-02"
echo "   补录与待重查标记同一事务落库；重查计算在归属写事务之外由后台线程完成"
echo "==================================================================="
post /api/revisions '{
  "note": "HR 补录：E003 归属广州成本中心",
  "entries": [
    {"employee_id": "E003", "cost_center": "CC-GZ-02",
     "effective_from": "2026-01-01", "effective_to": "2026-04-01"}
  ]
}' | pretty
echo

echo "==================================================================="
echo "7. 后台自动重查隔离行：归属补齐 -> 自动转正（稍等片刻即可见）"
echo "==================================================================="
sleep 1
curl -sS "${BASE}/api/timesheet-batches/TS-2026-02-001" | pretty
echo

echo "==================================================================="
echo "8. 待重查标记处理情况（PENDING -> DONE，记录转正行数）"
echo "==================================================================="
curl -sS "${BASE}/api/recheck-requests" | pretty
echo

echo "==================================================================="
echo "9. 下游只读取正式工时：3 行全部可见（含转正行，保留各自所用修订号）"
echo "==================================================================="
curl -sS "${BASE}/api/official-timesheets?batch_no=TS-2026-02-001" | pretty
echo

echo "==================================================================="
echo "10. 口径冻结验证：正式行保留归属所用修订号，后续补录不改写"
echo "    修订 3 将 E001 改派 CC-NEW；已入仓的 T-001 仍是 CC-SH-01 / 修订 1"
echo "==================================================================="
post /api/revisions '{
  "note": "HR 调岗：E001 二月起改派新成本中心",
  "entries": [
    {"employee_id": "E001", "cost_center": "CC-NEW",
     "effective_from": "2026-02-01", "effective_to": "2026-03-01"}
  ]
}' | pretty
sleep 1
curl -sS "${BASE}/api/official-timesheets?batch_no=TS-2026-02-001&employee_id=E001" | pretty
echo

echo "演示完成。交互式文档：${BASE}/docs"
