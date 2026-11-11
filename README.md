# 工时入仓与归属修订 API（FastAPI + SQLite）

财务数仓场景：工时系统明细持续入仓，**少数员工缺失成本中心归属不阻塞整批入仓**；
人事补录归属后，隔离行**自动重查转正**；月结口径按修订号冻结，补录不改写历史。

- **工时入仓**：按批次号接收工时明细（批内唯一工时ID、员工、工作日期、小时数），
  SQLite 保存原始批次；导入时固定当前归属修订号，已归属行进**正式工时表**，
  未归属行留**隔离区**；正式行保留所用修订号，后续补录不改写。
- **自动转正**：人事补录（修订）与**待重查标记**同一事务落库；后台线程在归属写事务
  之外重查隔离行，归属补齐才转正；**转正与隔离状态更新一起提交**；并发或重启不漏查、
  不重复转正。
- **幂等导入**：同批同内容重传返回原批回执（HTTP 200），同批号异内容拒绝（HTTP 409）。
- **下游口径**：下游只读取正式工时表；批次内正式及隔离明细均可查询。

## 目录结构

```
.
├── app/
│   ├── main.py        # FastAPI 路由、异常处理、后台重查线程装配
│   ├── timesheet.py   # 工时入仓：幂等导入、批次/正式/隔离查询
│   ├── recheck.py     # 后台重查：补录后自动重查隔离行并转正
│   ├── service.py     # 归属修订：批次校验、修订提交（同事务落重查标记）、归属查询
│   ├── database.py    # SQLite 连接、WAL、建表
│   ├── schemas.py     # Pydantic 请求/响应模型
│   └── errors.py      # 业务错误类型
├── tests/             # pytest 端到端测试（归属口径 + 工时入仓）
├── scripts/demo.sh    # 命令行端到端演示
├── Dockerfile
├── docker-compose.yml
├── requirements.txt
└── requirements-dev.txt
```

## 一、Docker Compose 启动（交付方式）

```bash
docker compose up -d --build
```

- 服务地址：<http://localhost:8000>
- 交互式 API 文档（Swagger UI）：<http://localhost:8000/docs>
- ReDoc 文档：<http://localhost:8000/redoc>
- SQLite 数据文件位于命名卷 `attribution-data`（容器内 `/data/attribution.db`），容器重建后数据保留。

停止与清理：

```bash
docker compose down          # 停止，保留数据
docker compose down -v       # 停止并删除数据卷（清空全部数据）
```

## 二、通过交互式文档演示（导入 → 补录 → 自动转正）

打开 <http://localhost:8000/docs>，各接口均含示例请求体，可直接 `Try it out`：

1. **POST `/api/revisions`** 提交初始归属（示例：E001→CC-SH-01、E002→CC-BJ-01）→ 返回 `revision_id: 1`
2. **POST `/api/timesheet-batches`** 导入工时批次（示例含无归属的 E003）→
   回执显示 `pinned_revision: 1`、`official_count: 2`、`quarantined_count: 1`
3. **GET `/api/timesheet-batches/{batch_no}`** 查看批次明细 → 2 行正式 + 1 行隔离（`PENDING`）
4. 再次 **POST `/api/timesheet-batches`**：
   - 同批同内容 → HTTP 200，`replayed: true`，返回原批回执；
   - 同批号异内容 → HTTP 409 `BATCH_CONTENT_MISMATCH`
5. **POST `/api/revisions`** 人事补录 E003 的归属 → 返回 `revision_id: 2`；
   补录与待重查标记同事务落库，后台线程随即自动重查
6. 再次 **GET `/api/timesheet-batches/{batch_no}`** → 隔离行已 `PROMOTED`，
   正式明细中新增该行（`source: PROMOTED`，`attribution_revision: 2`）
7. **GET `/api/official-timesheets`** → 下游只读正式工时，3 行全部可见；
   **GET `/api/recheck-requests`** → 查看每次补录触发的重查处理记录

也可以一条命令跑完等价的命令行演示：

```bash
./scripts/demo.sh
```

## 三、API 一览

### 工时入仓

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| POST | `/api/timesheet-batches` | 导入工时批次（201 新批 / 200 同内容重传 / 409 异内容拒绝） |
| GET | `/api/timesheet-batches` | 列出全部批次概要 |
| GET | `/api/timesheet-batches/{batch_no}` | 批次内正式及隔离明细 |
| GET | `/api/official-timesheets` | 下游读取正式工时（可按批次/员工/日期过滤） |
| GET | `/api/recheck-requests` | 待重查标记及处理结果（审计） |

### 归属修订（人事补录）

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| POST | `/api/revisions` | 提交一批归属区间，返回递增修订号；同事务落待重查标记 |
| GET | `/api/revisions` | 列出全部修订号 |
| POST | `/api/attribution/query` | 按指定修订号口径逐条查询工时归属 |
| POST | `/api/revisions/{id}/segments` | 查看某修订号提交的原始区间（审计） |

### 请求示例

导入工时批次：

```json
POST /api/timesheet-batches
{
  "batch_no": "TS-2026-02-001",
  "entries": [
    {"timesheet_id": "T-001", "employee_id": "E001", "work_date": "2026-02-10", "hours": 8},
    {"timesheet_id": "T-002", "employee_id": "E002", "work_date": "2026-02-10", "hours": 7.5},
    {"timesheet_id": "T-003", "employee_id": "E003", "work_date": "2026-02-10", "hours": 6}
  ]
}
```

回执（201）：

```json
{
  "batch_no": "TS-2026-02-001",
  "content_hash": "7055123e…",
  "pinned_revision": 1,
  "row_count": 3,
  "official_count": 2,
  "quarantined_count": 1,
  "replayed": false,
  "created_at": "2026-11-11T18:13:13.371Z"
}
```

人事补录（触发自动转正）：

```json
POST /api/revisions
{
  "note": "HR 补录：E003 归属广州成本中心",
  "entries": [
    {"employee_id": "E003", "cost_center": "CC-GZ-02",
     "effective_from": "2026-01-01", "effective_to": "2026-04-01"}
  ]
}
```

## 四、核心设计

### 1. 导入即固定修订，正式行不改写

导入在单个 `BEGIN IMMEDIATE` 事务内完成：幂等判定 → 固定当前最大修订号
（`pinned_revision`，与并发补录互斥）→ 逐条归属 → 原始批次、正式行、隔离行一起落库。
正式工时表只插入不更新，`attribution_revision` 永久保留归属所用修订号；
之后人事补录只影响新导入与隔离重查，不回写历史正式行。

### 2. 补录与待重查标记共同落库，重查在归属写事务外进行

`POST /api/revisions` 的写事务只负责：修订号 + 归属区间 + `recheck_requests(PENDING)`
标记，三者一起提交或一起回滚。后台线程发现 PENDING 标记后，先在**读快照**中按当前
最大修订号计算可转正行，再在独立写事务中落结果——重查计算从不占用归属写事务。

### 3. 转正与隔离状态更新一起提交，不重复转正

每条隔离行的转正在同一个 `BEGIN IMMEDIATE` 事务内完成：

```sql
UPDATE quarantine_entries
SET status='PROMOTED', resolved_revision=:r
WHERE id=:id AND status='PENDING';   -- 闸门：受影响行数 0 则跳过

INSERT INTO official_timesheets (...) VALUES (...);  -- UNIQUE(batch_no, timesheet_id) 兜底

UPDATE recheck_requests SET status='DONE', promoted_count=:n WHERE id=:req AND status='PENDING';
```

导入、补录、重查任意并发时，写事务相互串行；重复触发或并发重查只会命中
`status='PENDING'` 闸门一次。PENDING 标记持久化在 SQLite 中，**服务重启后后台线程
启动即扫尾**，不会漏查。

### 4. 幂等导入

批次号为主键；内容哈希按 `timesheet_id` 排序后规范化序列化（SHA-256），
明细顺序不影响判定。同批同内容重传返回首次导入的回执（`replayed=true`，HTTP 200），
不产生任何写入；同批号异内容返回 409，原批数据不受影响。

### 5. 归属口径（沿用修订模型）

归属区间只追加存储，按修订号时间旅行查询：

```sql
WHERE employee_id = ?
  AND effective_from <= :work_date
  AND effective_to   > :work_date   -- 左闭右开
  AND revision_id <= :query_revision
ORDER BY revision_id DESC, id DESC
LIMIT 1
```

导入与后台重查共用同一归属函数，两处口径一致。

## 五、本地开发与测试（不用 Docker）

```bash
pip install -r requirements-dev.txt
pytest -q
ATTRIBUTION_DB_PATH=./data/dev.db uvicorn app.main:app --reload
```

环境变量：

| 变量 | 默认 | 说明 |
| --- | --- | --- |
| `ATTRIBUTION_DB_PATH` | `./data/attribution.db` | SQLite 文件位置 |
| `RECHECK_POLL_INTERVAL_MS` | `500` | 后台重查轮询间隔（毫秒） |
| `RECHECK_WORKER_ENABLED` | `1` | 置 `0` 关闭后台重查线程（测试用手动触发） |

## 六、错误响应格式

```json
{
  "error": {
    "code": "BATCH_CONTENT_MISMATCH",
    "message": "批次号 TS-2026-02-001 已存在但内容不一致：……",
    "details": {"batch_no": "TS-2026-02-001", "existing_content_hash": "…", "incoming_content_hash": "…"}
  }
}
```

| code | HTTP 状态码 | 触发条件 |
| --- | --- | --- |
| `BATCH_CONTENT_MISMATCH` | 409 | 同批号异内容重传 |
| `DUPLICATE_TIMESHEET_ID` | 400 | 批内工时ID重复 |
| `BATCH_NOT_FOUND` | 404 | 查询不存在的批次 |
| `INVALID_DATE_RANGE` | 400 | 归属区间起日不早于止日 |
| `OVERLAPPING_SEGMENTS` | 400 | 批内同一员工归属区间重叠 |
| `REVISION_NOT_FOUND` | 404 | 查询/审计了不存在的修订号 |
| Pydantic 校验错误 | 422 | 空批次、小时数非正、字段缺失、日期格式错误等 |
