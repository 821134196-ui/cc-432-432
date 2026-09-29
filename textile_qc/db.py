"""SQLite 数据层：连接管理与建表脚本。

所有表均带明确的业务约束；状态机约束在 service 层补充。
时间统一存 UTC ISO8601 字符串。
"""
from __future__ import annotations

import sqlite3
from contextlib import contextmanager
from pathlib import Path

SCHEMA = """
PRAGMA foreign_keys = ON;

-- 织机（来源机台）
CREATE TABLE IF NOT EXISTS loom (
    id          TEXT PRIMARY KEY,
    code        TEXT NOT NULL UNIQUE,
    name        TEXT NOT NULL,
    workshop    TEXT NOT NULL DEFAULT '织造一车间'
);

-- 颜色批次
CREATE TABLE IF NOT EXISTS color_batch (
    id          TEXT PRIMARY KEY,
    code        TEXT NOT NULL UNIQUE,
    color_name  TEXT NOT NULL,
    dye_lot     TEXT NOT NULL
);

-- 布卷（台账主体）
CREATE TABLE IF NOT EXISTS bolt (
    id              TEXT PRIMARY KEY,
    bolt_no         TEXT NOT NULL UNIQUE,          -- 布卷编号
    loom_id         TEXT NOT NULL REFERENCES loom(id),
    color_batch_id  TEXT NOT NULL REFERENCES color_batch(id),
    length_m        REAL NOT NULL CHECK (length_m > 0),   -- 实际长度（米）
    nominal_length_m REAL NOT NULL CHECK (nominal_length_m > 0), -- 标称长度
    width_cm        REAL NOT NULL CHECK (width_cm > 0),    -- 幅宽（厘米）
    status          TEXT NOT NULL DEFAULT 'pending'
        CHECK (status IN ('pending','inspecting','inspected','reinspecting',
                          'reworking','reinspect_after_rework','released','rejected')),
    current_version_id TEXT,
    released_version_id TEXT,
    created_at      TEXT NOT NULL,
    updated_at      TEXT NOT NULL
);

-- 检验版本：初检 v1，退修后新版本 v2、v3……（版本链）
CREATE TABLE IF NOT EXISTS inspection_version (
    id              TEXT PRIMARY KEY,
    bolt_id         TEXT NOT NULL REFERENCES bolt(id),
    version_no      INTEGER NOT NULL CHECK (version_no >= 1),
    kind            TEXT NOT NULL CHECK (kind IN ('initial','post_rework')),
    inspector       TEXT NOT NULL,                  -- 初检人
    supersedes_id   TEXT REFERENCES inspection_version(id), -- 退修后新版本指向旧版本
    rework_order_id TEXT,
    status          TEXT NOT NULL DEFAULT 'open'
        CHECK (status IN ('open','reinspected','released','rejected','superseded')),
    created_at      TEXT NOT NULL,
    UNIQUE (bolt_id, version_no)
);

-- 检验段：一次检验按米数切分的区段（复检只能引用原检验段）
CREATE TABLE IF NOT EXISTS segment (
    id              TEXT PRIMARY KEY,
    version_id      TEXT NOT NULL REFERENCES inspection_version(id),
    seg_index       INTEGER NOT NULL,
    start_m         REAL NOT NULL CHECK (start_m >= 0),
    end_m           REAL NOT NULL CHECK (end_m > start_m),
    note            TEXT NOT NULL DEFAULT '',
    UNIQUE (version_id, seg_index)
);

-- 疵点类型字典
CREATE TABLE IF NOT EXISTS defect_type (
    id              TEXT PRIMARY KEY,
    code            TEXT NOT NULL UNIQUE,          -- warp_break / color_diff / stain / length_short / missing_photo
    name            TEXT NOT NULL,
    default_grade   TEXT NOT NULL CHECK (default_grade IN ('A','B','C'))
);

-- 疵点：按米数定位（落在所属检验段区间内），附照片文件
CREATE TABLE IF NOT EXISTS defect (
    id              TEXT PRIMARY KEY,
    segment_id      TEXT NOT NULL REFERENCES segment(id),
    type_id         TEXT NOT NULL REFERENCES defect_type(id),
    position_m      REAL NOT NULL CHECK (position_m >= 0),
    grade           TEXT NOT NULL CHECK (grade IN ('A','B','C')),
    photo_path      TEXT,                            -- 相对照片根目录的路径；NULL=照片缺失
    photo_required  INTEGER NOT NULL DEFAULT 1,
    recorded_by     TEXT NOT NULL,
    created_at      TEXT NOT NULL,
    related_defect_id TEXT REFERENCES defect(id)     -- 自动挂的“照片缺失”指向原疵点
);

-- 复检任务
CREATE TABLE IF NOT EXISTS reinspection (
    id              TEXT PRIMARY KEY,
    bolt_id         TEXT NOT NULL REFERENCES bolt(id),
    version_id      TEXT NOT NULL REFERENCES inspection_version(id),
    segment_id      TEXT NOT NULL REFERENCES segment(id), -- 只能引用原检验段
    defect_id       TEXT REFERENCES defect(id),
    assigned_to     TEXT NOT NULL,
    status          TEXT NOT NULL DEFAULT 'queued'
        CHECK (status IN ('queued','accepted','completed','overdue','cancelled')),
    result          TEXT CHECK (result IS NULL OR result IN ('confirmed','cleared','escalated')),
    comment         TEXT NOT NULL DEFAULT '',
    claimed_at      TEXT,                            -- 接收时间（接收竞争用）
    claimed_by      TEXT,
    due_at          TEXT NOT NULL,                   -- 复检期限
    completed_at    TEXT,
    created_at      TEXT NOT NULL
);
-- 同一布卷同时只允许一条活跃复检任务（两条产线并发接收只有一条成功）；
-- 完成/取消/退修取消后可重新派发。
CREATE UNIQUE INDEX IF NOT EXISTS ux_reinspection_active_bolt
    ON reinspection(bolt_id)
    WHERE status IN ('queued','accepted','overdue');

-- 退修工单
CREATE TABLE IF NOT EXISTS rework_order (
    id              TEXT PRIMARY KEY,
    bolt_id         TEXT NOT NULL REFERENCES bolt(id),
    version_id      TEXT NOT NULL REFERENCES inspection_version(id), -- 由哪个检验版本发起
    reason          TEXT NOT NULL,
    status          TEXT NOT NULL DEFAULT 'open'
        CHECK (status IN ('open','in_progress','done','cancelled')),
    created_at      TEXT NOT NULL,
    finished_at     TEXT
);

-- 放行记录
CREATE TABLE IF NOT EXISTS release_record (
    id              TEXT PRIMARY KEY,
    bolt_id         TEXT NOT NULL REFERENCES bolt(id),
    version_id      TEXT NOT NULL REFERENCES inspection_version(id),
    released_by     TEXT NOT NULL,
    decision        TEXT NOT NULL CHECK (decision IN ('release','reject')),
    basis           TEXT NOT NULL,                    -- 放行依据说明（最高等级/豁免/退修）
    created_at      TEXT NOT NULL
);

-- 复检超时提醒（进程内任务扫描写入）
CREATE TABLE IF NOT EXISTS reminder (
    id              TEXT PRIMARY KEY,
    reinspection_id TEXT NOT NULL REFERENCES reinspection(id),
    kind            TEXT NOT NULL DEFAULT 'reinspection_timeout',
    message         TEXT NOT NULL,
    created_at      TEXT NOT NULL,
    acknowledged    INTEGER NOT NULL DEFAULT 0
);

CREATE INDEX IF NOT EXISTS idx_defect_segment ON defect(segment_id);
CREATE INDEX IF NOT EXISTS idx_reinspection_bolt ON reinspection(bolt_id, status);
CREATE INDEX IF NOT EXISTS idx_version_bolt ON inspection_version(bolt_id);
"""


def connect(db_path: str | Path) -> sqlite3.Connection:
    db_path = Path(db_path)
    db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(db_path), timeout=10, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA journal_mode = WAL")
    conn.execute("PRAGMA busy_timeout = 10000")
    return conn


def init_db(conn: sqlite3.Connection) -> None:
    conn.executescript(SCHEMA)
    conn.commit()


@contextmanager
def transaction(conn: sqlite3.Connection):
    """显式事务。SQLite 在 BEGIN IMMEDIATE 下串行化写入，用于接收竞争。"""
    try:
        conn.execute("BEGIN IMMEDIATE")
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
