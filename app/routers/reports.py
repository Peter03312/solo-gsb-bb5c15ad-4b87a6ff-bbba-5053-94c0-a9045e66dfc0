from datetime import datetime

from fastapi import APIRouter, HTTPException, Query

from app.db import ConnDep
from app.schemas import DriftItem, ReleaseBasis, ReportOut, ReportSubmit, RecomputeResponse

router = APIRouter(prefix="/reports", tags=["reports"])

# 检测时点落在当前校准的 [valid_from, valid_to) 内才被覆盖。
# FOR UPDATE 锁定命中的当前校准行，与“更正”事务串行化，
# 保证不会依据正在/已经被更正的旧校准放行。
_COVERAGE_SQL = """
SELECT lineage_id, version, instrument_id, passed,
       valid_from, valid_to, superseded, note, created_at, now() AS evaluated_at
FROM calibrations
WHERE instrument_id = %(instrument_id)s
  AND superseded = FALSE
  AND valid_from <= %(test_time)s
  AND %(test_time)s < valid_to
FOR UPDATE OF calibrations
"""

_BLOCK_TEXT = {
    "no_coverage": "该检测时点没有任何当前有效校准区间覆盖",
    "not_passed": "覆盖该时点的当前校准结论为未通过",
}


def _basis(row: dict, prefix: str = "", evaluated_at: datetime | None = None) -> ReleaseBasis:
    def g(name: str):
        return row[prefix + name]

    return ReleaseBasis(
        lineage_id=g("lineage_id"),
        version=g("version"),
        passed=g("passed"),
        valid_from=g("valid_from"),
        valid_to=g("valid_to"),
        evaluated_at=evaluated_at if evaluated_at is not None else g("evaluated_at"),
    )


def _report_out(row: dict) -> ReportOut:
    basis = None
    if row["status"] == "published":
        basis = ReleaseBasis(
            lineage_id=row["snap_lineage_id"],
            version=row["snap_version"],
            passed=row["snap_passed"],
            valid_from=row["snap_valid_from"],
            valid_to=row["snap_valid_to"],
            evaluated_at=row["snap_evaluated_at"],
        )
    return ReportOut(
        id=row["id"],
        instrument_id=row["instrument_id"],
        test_time=row["test_time"],
        status=row["status"],
        block_reason_code=row["block_reason_code"],
        block_reason=row["block_reason"],
        basis=basis,
        created_at=row["created_at"],
    )


@router.post("", response_model=ReportOut, status_code=201)
def submit_report(body: ReportSubmit, conn=ConnDep) -> ReportOut:
    """提交报告：仅当检测时点被“通过”的当前校准覆盖才发布，并记录判定快照。"""
    params = {"instrument_id": body.instrument_id, "test_time": body.test_time}
    try:
        with conn.cursor() as cur:
            cur.execute(_COVERAGE_SQL, params)
            coverage = cur.fetchone()

            if coverage is None or not coverage["passed"]:
                if coverage is None:
                    code = "no_coverage"
                else:
                    code = "not_passed"
                cur.execute(
                    "INSERT INTO reports"
                    " (instrument_id, test_time, status, block_reason_code, block_reason)"
                    " VALUES (%s, %s, 'blocked', %s, %s)"
                    " RETURNING *",
                    (
                        body.instrument_id,
                        body.test_time,
                        code,
                        _BLOCK_TEXT[code],
                    ),
                )
                blocked = _report_out(cur.fetchone())
                conn.commit()  # 阻断记录落库，随后以 422 说明阻断原因
                raise HTTPException(
                    status_code=422,
                    detail={
                        "code": "release_blocked",
                        "message": f"报告被阻断：{_BLOCK_TEXT[code]}。",
                        "block_reason_code": code,
                        "block_reason": _BLOCK_TEXT[code],
                        "report_id": blocked.id,
                    },
                )

            cur.execute(
                "INSERT INTO reports"
                " (instrument_id, test_time, status,"
                "  snap_lineage_id, snap_version, snap_passed,"
                "  snap_valid_from, snap_valid_to, snap_evaluated_at)"
                " VALUES (%s, %s, 'published', %s, %s, %s, %s, %s, %s)"
                " RETURNING *",
                (
                    body.instrument_id,
                    body.test_time,
                    coverage["lineage_id"],
                    coverage["version"],
                    coverage["passed"],
                    coverage["valid_from"],
                    coverage["valid_to"],
                    coverage["evaluated_at"],
                ),
            )
            published = _report_out(cur.fetchone())
            conn.commit()
            return published
    except HTTPException:
        raise
    except Exception:
        conn.rollback()
        raise


@router.get("", response_model=list[ReportOut])
def list_reports(
    conn=ConnDep,
    status: str | None = Query(default=None, pattern="^(published|blocked)$"),
) -> list[ReportOut]:
    sql = "SELECT * FROM reports"
    params: list[str] = []
    if status:
        sql += " WHERE status = %s"
        params.append(status)
    sql += " ORDER BY id"
    with conn.cursor() as cur:
        cur.execute(sql, params)
        return [_report_out(r) for r in cur.fetchall()]


@router.get("/recompute-drift", response_model=RecomputeResponse)
def recompute_drift(
    conn=ConnDep,
    changed_only: bool = Query(
        default=True, description="仅返回当前结论与发布快照不同的报告"
    ),
) -> RecomputeResponse:
    """校准更正后按最新版重算已发布报告的当前有效性；只比对、不改写快照。"""
    sql = """
    SELECT now() AS evaluated_at,
           r.id AS report_id, r.instrument_id, r.test_time,
           r.snap_lineage_id, r.snap_version, r.snap_passed,
           r.snap_valid_from, r.snap_valid_to, r.snap_evaluated_at,
           c.lineage_id AS cur_lineage_id, c.version AS cur_version,
           c.passed AS cur_passed, c.valid_from AS cur_valid_from,
           c.valid_to AS cur_valid_to,
           CASE
               WHEN c.lineage_id IS NULL THEN 'no_coverage'
               WHEN c.passed THEN NULL
               ELSE 'not_passed'
           END AS block_code
    FROM reports r
    LEFT JOIN LATERAL (
        SELECT * FROM calibrations
        WHERE instrument_id = r.instrument_id
          AND superseded = FALSE
          AND valid_from <= r.test_time
          AND r.test_time < valid_to
    ) c ON TRUE
    WHERE r.status = 'published'
      AND (%(changed_only)s IS FALSE OR c.lineage_id IS NULL OR c.passed = FALSE)
    ORDER BY r.id
    """
    with conn.cursor() as cur:
        cur.execute(sql, {"changed_only": changed_only})
        rows = cur.fetchall()

    items: list[DriftItem] = []
    for r in rows:
        snapshot = ReleaseBasis(
            lineage_id=r["snap_lineage_id"],
            version=r["snap_version"],
            passed=r["snap_passed"],
            valid_from=r["snap_valid_from"],
            valid_to=r["snap_valid_to"],
            evaluated_at=r["snap_evaluated_at"],
        )
        current_basis = None
        block_code = r["block_code"]
        if r["cur_lineage_id"] is not None:
            current_basis = ReleaseBasis(
                lineage_id=r["cur_lineage_id"],
                version=r["cur_version"],
                passed=r["cur_passed"],
                valid_from=r["cur_valid_from"],
                valid_to=r["cur_valid_to"],
                evaluated_at=r["evaluated_at"],
            )
        releasable = block_code is None
        items.append(
            DriftItem(
                report_id=r["report_id"],
                instrument_id=r["instrument_id"],
                test_time=r["test_time"],
                snapshot=snapshot,
                currently_releasable=releasable,
                current_basis=current_basis,
                block_reason_code=block_code,
                block_reason=_BLOCK_TEXT.get(block_code) if block_code else None,
                changed_from_snapshot=not releasable,
            )
        )

    # 统计全部已发布报告数量（changed_only 只影响返回明细）
    with conn.cursor() as cur:
        cur.execute("SELECT count(*) AS n FROM reports WHERE status = 'published'")
        total = cur.fetchone()["n"]

    return RecomputeResponse(
        evaluated_at=rows[0]["evaluated_at"] if rows else datetime.now().astimezone(),
        checked=total,
        changed_count=len(items),
        changed=items,
        results=items,
    )


@router.get("/{report_id}", response_model=ReportOut)
def get_report(report_id: int, conn=ConnDep) -> ReportOut:
    with conn.cursor() as cur:
        cur.execute("SELECT * FROM reports WHERE id = %s", (report_id,))
        row = cur.fetchone()
    if row is None:
        raise HTTPException(
            status_code=404, detail={"code": "not_found", "message": "报告不存在。"}
        )
    return _report_out(row)
