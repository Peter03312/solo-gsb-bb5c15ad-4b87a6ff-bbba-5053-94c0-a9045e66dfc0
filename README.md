# 实验室检测报告放行服务（FastAPI + PostgreSQL）

面向实验室检测报告放行流程的 HTTP API：

- **校准管理**：登记仪器校准记录，含仪器编号、有效起止时间、通过/未通过结论；
  同一仪器**当前版本**的有效区间**不得重叠**，区间按 **左闭右开 `[valid_from, valid_to)`** 判定。
- **校准更正**：更正必须引用原记录（版本链 `lineage_id`）并提交**连续版本号**；
  旧版本保留可查，**仅最新版**参与判定；版本冲突、区间冲突一律拒绝（409）。
- **报告放行**：提交仪器与检测时间，只有该时点被**通过的**当前校准覆盖才能发布，
  响应返回所依据的校准版本；缺失覆盖或未通过时以 422 说明阻断原因。
- **判定快照**：已发布报告固化发布时的判定依据（版本快照）；校准更正后可重算
  当前有效性，列出与快照结论不同的报告，**快照本身永不被改写**。
- **并发安全**：数据库行锁 + 唯一/排他约束，保证并发更正/发布不会产生双最新版，
  也不会依据正在被更正的过期校准放行。

## 目录结构

```
.
├── app/
│   ├── main.py                 # FastAPI 入口
│   ├── config.py               # 环境变量配置
│   ├── db.py                   # psycopg 连接池
│   ├── schemas.py              # 请求/响应模型
│   └── routers/
│       ├── calibrations.py     # 校准登记 / 更正 / 查询
│       └── reports.py          # 报告发布 / 查询 / 漂移重算
├── sql/01_init.sql             # 建表脚本（首次启动自动执行）
├── Dockerfile
├── docker-compose.yml
├── requirements.txt
└── examples/
    ├── api-examples.http       # REST Client 调用示例
    ├── demo.sh                 # 端到端流程演示
    └── concurrency_check.sh    # 并发冲突验证
```

## 一键启动

前置：已安装 Docker 与 Docker Compose 插件。

```bash
docker compose up --build
```

启动后：

| 服务 | 地址 |
| --- | --- |
| API 服务（Swagger UI） | http://localhost:8000/docs |
| OpenAPI 文档 | http://localhost:8000/openapi.json |
| 健康检查 | http://localhost:8000/health |
| PostgreSQL | localhost:5432 |

数据库容器**首次启动**时自动执行 `sql/01_init.sql` 完成初始化
（启用 `btree_gist`、建表、建唯一索引与 GiST 排他约束）。

重置数据库：

```bash
docker compose down -v   # 删除数据卷后重新 up
```

## 配置

配置项通过环境变量传入（可在项目根目录放 `.env`，见下表默认值）：

| 变量 | 默认值 | 说明 |
| --- | --- | --- |
| `DATABASE_URL` | `postgresql://lab:labpass@db:5432/labrelease` | API 使用的数据库连接串 |
| `DB_POOL_MIN_SIZE` | `2` | 连接池最小连接数 |
| `DB_POOL_MAX_SIZE` | `10` | 连接池最大连接数 |
| `POSTGRES_USER` | `lab` | 数据库初始化用户名 |
| `POSTGRES_PASSWORD` | `labpass` | 数据库初始化密码 |
| `POSTGRES_DB` | `labrelease` | 数据库名 |
| `API_HOST_PORT` | `8000` | API 映射到宿主机的端口 |
| `DB_HOST_PORT` | `5432` | PostgreSQL 映射到宿主机的端口 |

修改端口示例：`API_HOST_PORT=18080 docker compose up --build`。

## API 一览

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| `POST` | `/calibrations` | 登记首版校准（v1） |
| `POST` | `/calibrations/{lineage_id}/corrections` | 更正校准（提交连续版本号） |
| `GET` | `/calibrations?instrument_id=&current_only=` | 查询校准（可仅看最新版） |
| `GET` | `/calibrations/lineages` | 校准版本链列表 |
| `GET` | `/calibrations/lineages/{id}` | 查询版本链全部历史版本 |
| `POST` | `/reports` | 提交报告并判定放行 |
| `GET` | `/reports?status=published|blocked` | 报告列表 |
| `GET` | `/reports/{id}` | 报告详情（含发布快照） |
| `GET` | `/reports/recompute-drift?changed_only=true` | 更正后重算并列出结论漂移报告 |

时间字段（`valid_from` / `valid_to` / `test_time`）必须携带时区，
例如 `2026-06-01T10:00:00+08:00`；且要求 `valid_from < valid_to`。

错误响应统一为 FastAPI 形式：`{"detail": {"code": "...", "message": "..."}}`。

## 调用示例

完整流程（成功发布、区间冲突、版本冲突、更正、漂移重算）：

```bash
./examples/demo.sh
```

并发冲突验证（8 个并发更正恰好 1 个成功，其余 409，无双最新版）：

```bash
./examples/concurrency_check.sh
```

### 1. 登记校准（v1，通过）

```bash
curl -X POST http://localhost:8000/calibrations \
  -H 'Content-Type: application/json' \
  -d '{
    "instrument_id": "LCMS-01",
    "passed": true,
    "valid_from": "2026-01-01T00:00:00+08:00",
    "valid_to": "2027-01-01T00:00:00+08:00",
    "note": "年度校准 v1"
  }'
```

同仪器当前版本区间重叠时返回 **409 `interval_conflict`**。

### 2. 发布报告（检测时点被通过校准覆盖）

```bash
curl -X POST http://localhost:8000/reports \
  -H 'Content-Type: application/json' \
  -d '{"instrument_id": "LCMS-01", "test_time": "2026-06-01T10:00:00+08:00"}'
```

成功（201）响应包含所据校准版本快照：

```json
{
  "id": 1,
  "status": "published",
  "basis": {
    "lineage_id": 1, "version": 1, "passed": true,
    "valid_from": "2026-01-01T00:00:00+08:00",
    "valid_to": "2027-01-01T00:00:00+08:00",
    "evaluated_at": "2026-09-30T..."
  }
}
```

阻断时返回 **422 `release_blocked`**：

- `no_coverage`：该时点没有任何当前校准区间覆盖；
- `not_passed`：覆盖该时点的当前校准结论为未通过。

阻断记录也会落库（`status=blocked`），响应 `detail.report_id` 可用于查询。

### 3. 更正校准（连续版本号）

```bash
curl -X POST http://localhost:8000/calibrations/1/corrections \
  -H 'Content-Type: application/json' \
  -d '{
    "version": 2,
    "passed": true,
    "valid_from": "2026-01-01T00:00:00+08:00",
    "valid_to": "2026-05-01T00:00:00+08:00",
    "note": "搬迁后重校，v1 区间更正缩短"
  }'
```

- `version` 必须等于当前最新版本 + 1，否则 **409 `version_conflict`**；
- 与同仪器其它当前版本区间重叠则 **409 `interval_conflict`**；
- 旧版 v1 置为 `superseded=true`，仍可经 `/calibrations/lineages/1` 查询，
  但不再参与任何判定。

### 4. 更正后重算漂移（不改写快照）

```bash
curl 'http://localhost:8000/reports/recompute-drift?changed_only=true'
```

对每条已发布报告按**当前最新校准**重新判定 `test_time`：
`changed_from_snapshot=true` 表示按现行校准该报告已不能放行
（`block_reason_code` 为 `no_coverage` / `not_passed`），并给出当前依据。
报告自身的发布快照保持不变。

## 并发与一致性保证

1. **连续版本号 / 无双最新版**：更正事务先
   `SELECT ... FROM calibration_lineages WHERE id=? FOR UPDATE` 锁定版本链，
   再校验版本号、下线旧版、插入新版。并发更正被行锁串行化，落后者必然撞
   `version_conflict`。数据库层另有
   `WHERE superseded=FALSE` 的部分唯一索引兜底，每条链至多一个最新版。
2. **区间不重叠**：`EXCLUDE USING gist (instrument_id WITH =,
   tstzrange(valid_from, valid_to, '[)') WITH &&) WHERE (superseded=FALSE)`
   在数据库层强制左闭右开区间不重叠，任何并发写入都无法绕过。
3. **发布不依据过期校准**：发布事务用
   `SELECT ... WHERE superseded=FALSE AND valid_from <= t AND t < valid_to
   FOR UPDATE` 锁定命中的当前校准行，与更正事务互斥：
   - 先更正后发布：发布读到的是新最新版，按新版本判定；
   - 先发布后更正：发布持锁直到提交，依据旧版完成放行并固化快照，
     随后才允许更正——快照如实反映“发布时”依据，后续漂移可通过重算接口发现。

## 本地开发（不使用 Docker 运行应用）

```bash
python -m venv .venv && . .venv/bin/activate
pip install -r requirements.txt
export DATABASE_URL='postgresql://lab:labpass@localhost:5432/labrelease'
uvicorn app.main:app --reload --port 8000
```

PostgreSQL 仍可用 `docker compose up -d db` 单独启动。
