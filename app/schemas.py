from datetime import datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

CalibrationConclusion = Literal["passed", "failed"]


class CalibrationCreate(BaseModel):
    instrument_id: str = Field(min_length=1, examples=["LCMS-01"])
    passed: bool
    valid_from: datetime = Field(description="含时区；左闭")
    valid_to: datetime = Field(description="含时区；右开")
    note: str | None = None

    @model_validator(mode="after")
    def _check_interval(self) -> "CalibrationCreate":
        if self.valid_from.tzinfo is None or self.valid_to.tzinfo is None:
            raise ValueError("valid_from/valid_to 必须携带时区信息")
        if self.valid_from >= self.valid_to:
            raise ValueError("要求 valid_from < valid_to（区间左闭右开）")
        return self


class CalibrationCorrect(BaseModel):
    """更正：version 必须是当前版本号 + 1（连续版本号）。"""

    version: int = Field(ge=1, description="期望写入的版本号，须等于当前版本+1")
    passed: bool
    valid_from: datetime
    valid_to: datetime
    note: str | None = None

    @model_validator(mode="after")
    def _check_interval(self) -> "CalibrationCorrect":
        if self.valid_from.tzinfo is None or self.valid_to.tzinfo is None:
            raise ValueError("valid_from/valid_to 必须携带时区信息")
        if self.valid_from >= self.valid_to:
            raise ValueError("要求 valid_from < valid_to（区间左闭右开）")
        return self


class CalibrationOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    lineage_id: int
    version: int
    instrument_id: str
    passed: bool
    conclusion: CalibrationConclusion
    valid_from: datetime
    valid_to: datetime
    superseded: bool
    note: str | None
    created_at: datetime


class LineageOut(BaseModel):
    id: int
    instrument_id: str
    current_version: int
    created_at: datetime


class ReportSubmit(BaseModel):
    instrument_id: str = Field(min_length=1, examples=["LCMS-01"])
    test_time: datetime = Field(description="检测时点，须含时区")

    @model_validator(mode="after")
    def _check_tz(self) -> "ReportSubmit":
        if self.test_time.tzinfo is None:
            raise ValueError("test_time 必须携带时区信息")
        return self


class ReleaseBasis(BaseModel):
    lineage_id: int
    version: int
    passed: bool
    valid_from: datetime
    valid_to: datetime
    evaluated_at: datetime


class ReportOut(BaseModel):
    id: int
    instrument_id: str
    test_time: datetime
    status: Literal["published", "blocked"]
    block_reason_code: Literal["no_coverage", "not_passed"] | None = None
    block_reason: str | None = None
    basis: ReleaseBasis | None = None
    created_at: datetime


class DriftItem(BaseModel):
    report_id: int
    instrument_id: str
    test_time: datetime
    snapshot: ReleaseBasis
    currently_releasable: bool
    current_basis: ReleaseBasis | None = None
    block_reason_code: Literal["no_coverage", "not_passed"] | None = None
    block_reason: str | None = None
    changed_from_snapshot: bool


class RecomputeResponse(BaseModel):
    evaluated_at: datetime
    checked: int
    changed_count: int
    changed: list[DriftItem]
    results: list[DriftItem]


class ErrorEnvelope(BaseModel):
    code: str
    message: str
    details: dict | None = None
