-- 实验室检测报告放行服务：初始化数据库结构
-- 区间语义：左闭右开 [valid_from, valid_to)
CREATE EXTENSION IF NOT EXISTS btree_gist;

-- 校准版本链：同一仪器每次“更正”都在同一 lineage 上追加连续版本
CREATE TABLE IF NOT EXISTS calibration_lineages (
    id              BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    instrument_id   TEXT NOT NULL,
    current_version INTEGER NOT NULL DEFAULT 0 CHECK (current_version >= 0),
    created_at      TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- 校准记录（含历史版本，旧版 superseded=TRUE，仅最新版参与判定）
CREATE TABLE IF NOT EXISTS calibrations (
    lineage_id    BIGINT NOT NULL
                  REFERENCES calibration_lineages(id) ON DELETE RESTRICT,
    version       INTEGER NOT NULL CHECK (version >= 1),
    instrument_id TEXT NOT NULL,
    passed        BOOLEAN NOT NULL,
    valid_from    TIMESTAMPTZ NOT NULL,
    valid_to      TIMESTAMPTZ NOT NULL,
    superseded    BOOLEAN NOT NULL DEFAULT FALSE,
    note          TEXT,
    created_at    TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (lineage_id, version),
    CHECK (valid_from < valid_to)
);

-- 双最新版兜底：每条版本链至多一行 superseded=FALSE
CREATE UNIQUE INDEX IF NOT EXISTS uq_calibrations_one_current
    ON calibrations (lineage_id)
    WHERE superseded = FALSE;

-- 区间冲突兜底：同仪器的“当前版本”有效区间不得重叠（左闭右开）
ALTER TABLE calibrations DROP CONSTRAINT IF EXISTS ex_calibrations_current_no_overlap;
ALTER TABLE calibrations
    ADD CONSTRAINT ex_calibrations_current_no_overlap
    EXCLUDE USING gist (
        instrument_id WITH =,
        tstzrange(valid_from, valid_to, '[)') WITH &&
    ) WHERE (superseded = FALSE);

-- 发布判定查询索引
CREATE INDEX IF NOT EXISTS idx_calibrations_instrument_current
    ON calibrations (instrument_id, valid_from, valid_to)
    WHERE superseded = FALSE;

-- 检测报告：published 保留发布时判定快照；blocked 记录阻断原因
CREATE TABLE IF NOT EXISTS reports (
    id                 BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    instrument_id      TEXT NOT NULL,
    test_time          TIMESTAMPTZ NOT NULL,
    status             TEXT NOT NULL CHECK (status IN ('published', 'blocked')),
    block_reason_code  TEXT CHECK (block_reason_code IN ('no_coverage', 'not_passed')),
    block_reason       TEXT,
    -- 发布时刻判定快照（即便校准随后被更正也不改写）
    snap_lineage_id    BIGINT,
    snap_version       INTEGER,
    snap_passed        BOOLEAN,
    snap_valid_from    TIMESTAMPTZ,
    snap_valid_to      TIMESTAMPTZ,
    snap_evaluated_at  TIMESTAMPTZ,
    created_at         TIMESTAMPTZ NOT NULL DEFAULT now(),
    CONSTRAINT fk_reports_snapshot
        FOREIGN KEY (snap_lineage_id, snap_version)
        REFERENCES calibrations (lineage_id, version) ON DELETE RESTRICT,
    CONSTRAINT chk_reports_published_snapshot CHECK (
        status <> 'published' OR (
            snap_lineage_id IS NOT NULL
            AND snap_version IS NOT NULL
            AND snap_passed IS TRUE
            AND snap_valid_from IS NOT NULL
            AND snap_valid_to IS NOT NULL
        )
    )
);

CREATE INDEX IF NOT EXISTS idx_reports_instrument ON reports (instrument_id);
CREATE INDEX IF NOT EXISTS idx_reports_status ON reports (status);
