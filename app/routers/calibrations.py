from psycopg.errors import ExclusionViolation, UniqueViolation
from fastapi import APIRouter, HTTPException, Query

from app.db import ConnDep
from app.schemas import CalibrationCorrect, CalibrationCreate, CalibrationOut, LineageOut

router = APIRouter(prefix="/calibrations", tags=["calibrations"])

_INSERT_COLUMNS = (
    "lineage_id, version, instrument_id, passed, valid_from, valid_to, note"
)
_SELECT_COLUMNS = (
    "lineage_id, version, instrument_id, passed,"
    " CASE WHEN passed THEN 'passed' ELSE 'failed' END AS conclusion,"
    " valid_from, valid_to, superseded, note, created_at"
)


def _conflict_from_integrity_error(exc: Exception) -> HTTPException:
    # 唯一约束冲突：版本号重复 / 同链出现第二个最新版
    if isinstance(exc, UniqueViolation):
        return HTTPException(
            status_code=409,
            detail={
                "code": "version_conflict",
                "message": "版本冲突：该版本已存在，或同一校准链已存在最新版，请重新读取当前版本后再更正。",
            },
        )
    # GiST 排他约束冲突：同仪器当前版本有效区间重叠
    if isinstance(exc, ExclusionViolation):
        return HTTPException(
            status_code=409,
            detail={
                "code": "interval_conflict",
                "message": "区间冲突：同一仪器当前版本的有效区间不得重叠（左闭右开）。",
            },
        )
    raise exc


@router.post("", response_model=CalibrationOut, status_code=201)
def create_calibration(body: CalibrationCreate, conn=ConnDep) -> CalibrationOut:
    """登记首版校准（version=1）。与同仪器其它当前版本区间重叠则拒绝。"""
    with conn:  # 提交或异常时回滚
        with conn.cursor() as cur:
            cur.execute(
                "INSERT INTO calibration_lineages (instrument_id, current_version)"
                " VALUES (%s, 0) RETURNING id",
                (body.instrument_id,),
            )
            lineage_id = cur.fetchone()["id"]
            try:
                cur.execute(
                    f"INSERT INTO calibrations ({_INSERT_COLUMNS})"
                    " VALUES (%s, 1, %s, %s, %s, %s, %s)"
                    f" RETURNING {_SELECT_COLUMNS}",
                    (
                        lineage_id,
                        body.instrument_id,
                        body.passed,
                        body.valid_from,
                        body.valid_to,
                        body.note,
                    ),
                )
            except (UniqueViolation, ExclusionViolation) as exc:
                raise _conflict_from_integrity_error(exc)
            row = cur.fetchone()
            cur.execute(
                "UPDATE calibration_lineages SET current_version = 1 WHERE id = %s",
                (lineage_id,),
            )
            return CalibrationOut(**row)  # type: ignore[arg-type]


@router.post("/{lineage_id}/corrections", response_model=CalibrationOut)
def correct_calibration(
    lineage_id: int, body: CalibrationCorrect, conn=ConnDep
) -> CalibrationOut:
    """更正校准：引用原记录（lineage_id），提交连续版本号；旧版保留可查。"""
    with conn:
        with conn.cursor() as cur:
            # 锁定版本链，串行化并发更正，防止双最新版
            cur.execute(
                "SELECT id, instrument_id, current_version FROM calibration_lineages"
                " WHERE id = %s FOR UPDATE",
                (lineage_id,),
            )
            lineage = cur.fetchone()
            if lineage is None:
                raise HTTPException(
                    status_code=404,
                    detail={"code": "not_found", "message": "校准记录不存在。"},
                )
            if body.version != lineage["current_version"] + 1:
                raise HTTPException(
                    status_code=409,
                    detail={
                        "code": "version_conflict",
                        "message": (
                            f"版本号必须连续：当前最新版为 v{lineage['current_version']}，"
                            f"应提交 v{lineage['current_version'] + 1}，收到 v{body.version}。"
                        ),
                    },
                )

            # 旧版下线（先 UPDATE 后 INSERT，避免与唯一/排他约束打架）
            cur.execute(
                "UPDATE calibrations SET superseded = TRUE"
                " WHERE lineage_id = %s AND version = %s AND superseded = FALSE",
                (lineage_id, lineage["current_version"]),
            )
            try:
                cur.execute(
                    f"INSERT INTO calibrations ({_INSERT_COLUMNS})"
                    " VALUES (%s, %s, %s, %s, %s, %s, %s)"
                    f" RETURNING {_SELECT_COLUMNS}",
                    (
                        lineage_id,
                        body.version,
                        lineage["instrument_id"],
                        body.passed,
                        body.valid_from,
                        body.valid_to,
                        body.note,
                    ),
                )
            except (UniqueViolation, ExclusionViolation) as exc:
                raise _conflict_from_integrity_error(exc)
            row = cur.fetchone()
            cur.execute(
                "UPDATE calibration_lineages SET current_version = %s WHERE id = %s",
                (body.version, lineage_id),
            )
            return CalibrationOut(**row)  # type: ignore[arg-type]


@router.get("", response_model=list[CalibrationOut])
def list_calibrations(
    conn=ConnDep,
    instrument_id: str | None = Query(default=None),
    current_only: bool = Query(default=False, description="仅返回参与判定的最新版"),
) -> list[CalibrationOut]:
    sql = f"SELECT {_SELECT_COLUMNS} FROM calibrations"
    where, params = [], []
    if instrument_id:
        where.append("instrument_id = %s")
        params.append(instrument_id)
    if current_only:
        where.append("superseded = FALSE")
    if where:
        sql += " WHERE " + " AND ".join(where)
    sql += " ORDER BY instrument_id, lineage_id, version"
    with conn.cursor() as cur:  # 只读，自动提交模式下无需显式事务
        cur.execute(sql, params)
        return [CalibrationOut(**r) for r in cur.fetchall()]


@router.get("/lineages", response_model=list[LineageOut])
def list_lineages(conn=ConnDep) -> list[LineageOut]:
    with conn.cursor() as cur:
        cur.execute(
            "SELECT id, instrument_id, current_version, created_at"
            " FROM calibration_lineages ORDER BY id"
        )
        return [LineageOut(**r) for r in cur.fetchall()]


@router.get("/lineages/{lineage_id}", response_model=list[CalibrationOut])
def get_lineage(lineage_id: int, conn=ConnDep) -> list[CalibrationOut]:
    """查询某条校准链全部版本（旧版保留可查）。"""
    with conn.cursor() as cur:
        cur.execute("SELECT 1 FROM calibration_lineages WHERE id = %s", (lineage_id,))
        if cur.fetchone() is None:
            raise HTTPException(
                status_code=404,
                detail={"code": "not_found", "message": "校准记录不存在。"},
            )
        cur.execute(
            f"SELECT {_SELECT_COLUMNS} FROM calibrations"
            " WHERE lineage_id = %s ORDER BY version",
            (lineage_id,),
        )
        return [CalibrationOut(**r) for r in cur.fetchall()]
