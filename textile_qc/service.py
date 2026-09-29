"""核心业务服务：检验版本链、复检、退修、放行规则、照片权限、超时扫描。

规则汇总：
- 疵点位置必须落在所属检验段区间内；检验段必须落在布卷长度内且同版本不重叠。
- 复检任务只能引用当前版本的原检验段；复检人不得与初检人相同。
- 同一布卷同时只允许一条进行中的复检任务（部分唯一索引 + IMMEDIATE 事务），
  两条产线并发接收时只有一条成功。
- 退修后生成新版本（supersedes 指向旧版本），旧版本置 superseded、旧复检任务取消。
- 已放行/已拒收布卷只读，不能改回待检。
- 放行条件按最高疵点等级分级；照片缺失一票否决，长度不足需经理特许。
"""
from __future__ import annotations

import sqlite3
import threading
import uuid
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path

from . import users
from .db import transaction

GRADE_RANK = {"A": 3, "B": 2, "C": 1}

# 复检期限（小时）
DEFAULT_SLA_HOURS = 24

DEFECT_TYPES = {
    "warp_break":   ("断经", "A"),
    "color_diff":   ("色差", "B"),
    "stain":        ("污渍", "B"),
    "length_short": ("长度不足", "B"),
    "missing_photo": ("照片缺失", "C"),
}


class QCError(Exception):
    """业务规则错误。http_status 供 API 层映射。"""

    def __init__(self, message: str, http_status: int = 400, code: str = "rule_violation"):
        super().__init__(message)
        self.message = message
        self.http_status = http_status
        self.code = code


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _iso(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).isoformat()


def _uid(prefix: str) -> str:
    return f"{prefix}_{uuid.uuid4().hex[:12]}"


class QCService:
    def __init__(self, conn: sqlite3.Connection, photo_root: str | Path,
                 sla_hours: int = DEFAULT_SLA_HOURS, clock=_now):
        self.conn = conn
        self.photo_root = Path(photo_root)
        self.photo_root.mkdir(parents=True, exist_ok=True)
        self.sla_hours = sla_hours
        self.clock = clock
        self._lock = threading.RLock()  # 串行化进程内写事务，配合 DB 唯一索引

    @contextmanager
    def _tx(self):
        """进程内锁 + BEGIN IMMEDIATE：消除接收竞争与写-写冲突。"""
        with self._lock:
            with transaction(self.conn):
                yield

    # ---------------------------------------------------------------- 基础字典

    def seed_dictionaries(self) -> None:
        now = _iso(self.clock())
        for code, (name, grade) in DEFECT_TYPES.items():
            self.conn.execute(
                "INSERT INTO defect_type(id, code, name, default_grade) VALUES(?,?,?,?) "
                "ON CONFLICT(code) DO NOTHING",
                (_uid("dt"), code, name, grade),
            )
        self.conn.commit()

    def list_defect_types(self) -> list[dict]:
        rows = self.conn.execute("SELECT * FROM defect_type ORDER BY code").fetchall()
        return [dict(r) for r in rows]

    # ---------------------------------------------------------------- 布卷台账

    def register_bolt(self, bolt_no: str, loom_code: str, color_code: str,
                      length_m: float, nominal_length_m: float, width_cm: float) -> dict:
        if length_m <= 0 or nominal_length_m <= 0 or width_cm <= 0:
            raise QCError("长度与幅宽必须为正数")
        loom = self.conn.execute("SELECT * FROM loom WHERE code=?", (loom_code,)).fetchone()
        if not loom:
            raise QCError(f"织机不存在: {loom_code}", 404, "not_found")
        cb = self.conn.execute("SELECT * FROM color_batch WHERE code=?", (color_code,)).fetchone()
        if not cb:
            raise QCError(f"颜色批次不存在: {color_code}", 404, "not_found")
        if self.conn.execute("SELECT 1 FROM bolt WHERE bolt_no=?", (bolt_no,)).fetchone():
            raise QCError(f"布卷编号已存在: {bolt_no}", 409, "duplicate")
        now = _iso(self.clock())
        bid = _uid("bolt")
        with self._tx():
            self.conn.execute(
                "INSERT INTO bolt(id, bolt_no, loom_id, color_batch_id, length_m, "
                "nominal_length_m, width_cm, status, created_at, updated_at) "
                "VALUES(?,?,?,?,?,?,?,'pending',?,?)",
                (bid, bolt_no, loom["id"], cb["id"], length_m, nominal_length_m,
                 width_cm, now, now),
            )
        return self.get_bolt(bid)

    def list_bolts(self, status: str | None = None) -> list[dict]:
        sql = (
            "SELECT b.*, l.code AS loom_code, l.name AS loom_name, "
            "c.code AS color_code, c.color_name, c.dye_lot, "
            "(SELECT COUNT(*) FROM defect d JOIN segment s ON s.id=d.segment_id "
            " WHERE s.version_id=b.current_version_id) AS defect_count "
            "FROM bolt b JOIN loom l ON l.id=b.loom_id "
            "JOIN color_batch c ON c.id=b.color_batch_id")
        if status:
            rows = self.conn.execute(sql + " WHERE b.status=? ORDER BY b.bolt_no", (status,)).fetchall()
        else:
            rows = self.conn.execute(sql + " ORDER BY b.bolt_no").fetchall()
        out = []
        for r in rows:
            d = dict(r)
            d["length_short"] = d["length_m"] + 1e-9 < d["nominal_length_m"]
            out.append(d)
        return out

    def get_bolt(self, bolt_id_or_no: str) -> dict:
        r = self.conn.execute(
            "SELECT b.*, l.code AS loom_code, l.name AS loom_name, "
            "c.code AS color_code, c.color_name, c.dye_lot "
            "FROM bolt b JOIN loom l ON l.id=b.loom_id "
            "JOIN color_batch c ON c.id=b.color_batch_id "
            "WHERE b.id=? OR b.bolt_no=?", (bolt_id_or_no, bolt_id_or_no)).fetchone()
        if not r:
            raise QCError(f"布卷不存在: {bolt_id_or_no}", 404, "not_found")
        d = dict(r)
        d["length_short"] = d["length_m"] + 1e-9 < d["nominal_length_m"]
        return d

    def _bolt_row(self, bolt_id: str) -> sqlite3.Row:
        r = self.conn.execute("SELECT * FROM bolt WHERE id=?", (bolt_id,)).fetchone()
        if not r:
            raise QCError(f"布卷不存在: {bolt_id}", 404, "not_found")
        return r

    def _require_unlocked(self, bolt: sqlite3.Row) -> None:
        if bolt["status"] in ("released", "rejected"):
            raise QCError(
                f"布卷 {bolt['bolt_no']} 已{('放行' if bolt['status']=='released' else '拒收')}，"
                "不能直接改为待检或继续编辑", 409, "bolt_locked")

    # ---------------------------------------------------------------- 检验版本

    def open_version(self, bolt_id: str, inspector: str, kind: str = "initial") -> dict:
        """开检验版本。初检 version_no=1；退修后的新版本由 finish_rework 内部创建。"""
        users.get_user(inspector)
        bolt = self._bolt_row(bolt_id)
        self._require_unlocked(bolt)
        if kind != "initial":
            raise QCError("退修后版本只能由退修完工流程生成")
        if bolt["status"] not in ("pending",):
            raise QCError(f"布卷当前状态 {bolt['status']}，不能新建初检版本", 409, "bad_state")
        now = _iso(self.clock())
        vid = _uid("ver")
        with self._tx():
            self.conn.execute(
                "INSERT INTO inspection_version(id, bolt_id, version_no, kind, inspector, "
                "status, created_at) VALUES(?,?,1,'initial',?,'open',?)",
                (vid, bolt_id, inspector, now))
            self.conn.execute(
                "UPDATE bolt SET status='inspecting', current_version_id=?, updated_at=? WHERE id=?",
                (vid, now, bolt_id))
        return self.get_version(vid)

    def get_version(self, version_id: str) -> dict:
        r = self.conn.execute(
            "SELECT v.*, b.bolt_no, b.length_m, b.nominal_length_m, b.width_cm "
            "FROM inspection_version v JOIN bolt b ON b.id=v.bolt_id WHERE v.id=?",
            (version_id,)).fetchone()
        if not r:
            raise QCError("检验版本不存在", 404, "not_found")
        return dict(r)

    def list_versions(self, bolt_id: str) -> list[dict]:
        rows = self.conn.execute(
            "SELECT * FROM inspection_version WHERE bolt_id=? ORDER BY version_no",
            (bolt_id,)).fetchall()
        return [dict(r) for r in rows]

    def add_segment(self, version_id: str, start_m: float, end_m: float,
                    note: str = "", actor: str = "system") -> dict:
        v = self.get_version(version_id)
        if v["status"] != "open":
            raise QCError("检验版本已封板，不能再添加检验段", 409, "bad_state")
        if start_m < 0 or end_m <= start_m:
            raise QCError("检验段区间非法：要求 0 ≤ start < end")
        if end_m > v["length_m"] + 1e-9:
            raise QCError(
                f"检验段终点 {end_m}m 超出布卷实际长度 {v['length_m']}m",
                422, "out_of_range")
        with self._tx():
            existing = self.conn.execute(
                "SELECT start_m, end_m FROM segment WHERE version_id=?", (version_id,)).fetchall()
            for seg in existing:
                if start_m < seg["end_m"] - 1e-9 and end_m > seg["start_m"] + 1e-9:
                    raise QCError(
                        f"检验段区间重叠：[{start_m},{end_m}) 与 "
                        f"[{seg['start_m']},{seg['end_m']}) 相交", 409, "overlap")
            idx = self.conn.execute(
                "SELECT COALESCE(MAX(seg_index)+1,0) FROM segment WHERE version_id=?",
                (version_id,)).fetchone()[0]
            sid = _uid("seg")
            self.conn.execute(
                "INSERT INTO segment(id, version_id, seg_index, start_m, end_m, note) "
                "VALUES(?,?,?,?,?,?)", (sid, version_id, idx, start_m, end_m, note))
        return self.get_segment(sid)

    def get_segment(self, segment_id: str) -> dict:
        r = self.conn.execute("SELECT * FROM segment WHERE id=?", (segment_id,)).fetchone()
        if not r:
            raise QCError("检验段不存在", 404, "not_found")
        return dict(r)

    def list_segments(self, version_id: str) -> list[dict]:
        rows = self.conn.execute(
            "SELECT * FROM segment WHERE version_id=? ORDER BY seg_index",
            (version_id,)).fetchall()
        return [dict(r) for r in rows]

    def add_defect(self, segment_id: str, type_code: str, position_m: float,
                   recorded_by: str, grade: str | None = None,
                   photo_data: bytes | None = None, photo_ext: str = ".png",
                   require_photo: bool = True) -> dict:
        """录入疵点。位置必须落在检验段内；无照片时自动挂一条“照片缺失”关联疵点。"""
        users.get_user(recorded_by)
        if not users.has_role(recorded_by, "inspector"):
            raise QCError("只有初检工可以录入疵点", 403, "forbidden")
        seg = self.get_segment(segment_id)
        v = self.get_version(seg["version_id"])
        if v["status"] != "open":
            raise QCError("检验版本已封板，不能再录入疵点", 409, "bad_state")
        dt = self.conn.execute("SELECT * FROM defect_type WHERE code=?", (type_code,)).fetchone()
        if not dt:
            raise QCError(f"疵点类型不存在: {type_code}", 404, "not_found")
        grade = grade or dt["default_grade"]
        if grade not in GRADE_RANK:
            raise QCError("疵点等级必须为 A/B/C")
        if not (seg["start_m"] - 1e-9 <= position_m < seg["end_m"] - 1e-9):
            raise QCError(
                f"疵点位置 {position_m}m 不在检验段 "
                f"[{seg['start_m']},{seg['end_m']}) 内", 422, "out_of_range")
        photo_req = 0 if type_code in ("length_short",) else int(require_photo)
        now = _iso(self.clock())
        with self._tx():
            did = _uid("def")
            photo_path = None
            if photo_data is not None:
                photo_path = self._write_photo_file(v["bolt_no"], did, photo_data, photo_ext)
            self.conn.execute(
                "INSERT INTO defect(id, segment_id, type_id, position_m, grade, photo_path, "
                "photo_required, recorded_by, created_at, related_defect_id) "
                "VALUES(?,?,?,?,?,?,?,?,?,NULL)",
                (did, segment_id, dt["id"], position_m, grade, photo_path,
                 photo_req, recorded_by, now))
            missing_id = None
            if photo_path is None and photo_req:
                mt = self.conn.execute(
                    "SELECT * FROM defect_type WHERE code='missing_photo'").fetchone()
                missing_id = _uid("def")
                self.conn.execute(
                    "INSERT INTO defect(id, segment_id, type_id, position_m, grade, photo_path, "
                    "photo_required, recorded_by, created_at, related_defect_id) "
                    "VALUES(?,?,?,?,?,NULL,0,?,?,?)",
                    (missing_id, segment_id, mt["id"], position_m, "C", recorded_by, now, did))
        return self.get_defect(did)

    def get_defect(self, defect_id: str) -> dict:
        r = self.conn.execute(
            "SELECT d.*, t.code AS type_code, t.name AS type_name, s.start_m, s.end_m, "
            "v.bolt_id, v.inspector AS version_inspector, v.status AS version_status "
            "FROM defect d JOIN defect_type t ON t.id=d.type_id "
            "JOIN segment s ON s.id=d.segment_id "
            "JOIN inspection_version v ON v.id=s.version_id WHERE d.id=?",
            (defect_id,)).fetchone()
        if not r:
            raise QCError("疵点不存在", 404, "not_found")
        return dict(r)

    def list_defects(self, version_id: str) -> list[dict]:
        rows = self.conn.execute(
            "SELECT d.*, t.code AS type_code, t.name AS type_name "
            "FROM defect d JOIN defect_type t ON t.id=d.type_id "
            "JOIN segment s ON s.id=d.segment_id "
            "WHERE s.version_id=? ORDER BY d.position_m, d.id", (version_id,)).fetchall()
        return [dict(r) for r in rows]

    def attach_photo(self, defect_id: str, user: str, photo_data: bytes,
                     photo_ext: str = ".png") -> dict:
        """补照片：仅初检工/经理；补传后清除关联的“照片缺失”记录。已放行不可补。"""
        self._require_photo_role(user, users.PHOTO_UPLOAD_ROLES)
        d = self.get_defect(defect_id)
        bolt = self._bolt_row(d["bolt_id"])
        self._require_unlocked(bolt)
        if d["type_code"] == "length_short":
            raise QCError("长度不足为布卷级判定，不接收照片")
        with self._tx():
            path = self._write_photo_file(bolt["bolt_no"], defect_id, photo_data, photo_ext)
            self.conn.execute("UPDATE defect SET photo_path=? WHERE id=?", (path, defect_id))
            self.conn.execute(
                "DELETE FROM defect WHERE related_defect_id=? AND type_id="
                "(SELECT id FROM defect_type WHERE code='missing_photo')", (defect_id,))
        return self.get_defect(defect_id)

    def complete_version(self, version_id: str) -> dict:
        """封板检验版本。长度不足自动登记 B 级长度不足疵点。"""
        v = self.get_version(version_id)
        if v["status"] != "open":
            raise QCError("检验版本已封板", 409, "bad_state")
        segs = self.list_segments(version_id)
        if not segs:
            raise QCError("至少需要一个检验段才能封板")
        now = _iso(self.clock())
        with self._tx():
            short = v["length_m"] + 1e-9 < v["nominal_length_m"]
            has_short = self.conn.execute(
                "SELECT 1 FROM defect d JOIN defect_type t ON t.id=d.type_id "
                "JOIN segment s ON s.id=d.segment_id "
                "WHERE s.version_id=? AND t.code='length_short'", (version_id,)).fetchone()
            if short and not has_short:
                dt = self.conn.execute(
                    "SELECT * FROM defect_type WHERE code='length_short'").fetchone()
                self.conn.execute(
                    "INSERT INTO defect(id, segment_id, type_id, position_m, grade, photo_path, "
                    "photo_required, recorded_by, created_at, related_defect_id) "
                    "VALUES(?,?,?,0,'B',NULL,0,?,?,NULL)",
                    (_uid("def"), segs[0]["id"], dt["id"], v["inspector"], now))
            target = "reinspect_after_rework" if v["kind"] == "post_rework" else "inspected"
            self.conn.execute(
                "UPDATE inspection_version SET status='reinspected' WHERE id=?", (version_id,))
            self.conn.execute(
                "UPDATE bolt SET status=?, updated_at=? WHERE id=?",
                (target, now, v["bolt_id"]))
        return self.get_version(version_id)

    # ---------------------------------------------------------------- 复检任务

    def create_reinspection(self, bolt_id: str, segment_id: str, line: str,
                            defect_id: str | None = None,
                            sla_hours: int | None = None) -> dict:
        """产线接收布卷 → 生成复检任务。line 为产线标识（如 line1）。
        同一布卷并发接收只允许一条成功。"""
        if not line:
            raise QCError("产线标识不能为空")
        bolt = self._bolt_row(bolt_id)
        self._require_unlocked(bolt)
        if bolt["status"] not in ("inspected", "reinspect_after_rework"):
            raise QCError(f"布卷状态 {bolt['status']}，不在复检队列中", 409, "bad_state")
        seg = self.get_segment(segment_id)
        if seg["version_id"] != bolt["current_version_id"]:
            raise QCError("复检只能引用当前检验版本的原检验段", 422, "bad_reference")
        if defect_id:
            d = self.get_defect(defect_id)
            if d["segment_id"] != segment_id:
                raise QCError("疵点不属于该检验段", 422, "bad_reference")
        due = self.clock() + timedelta(hours=sla_hours or self.sla_hours)
        now = _iso(self.clock())
        rid = _uid("ri")
        with self._tx():  # BEGIN IMMEDIATE：并发接收在此串行
            try:
                self.conn.execute(
                    "INSERT INTO reinspection(id, bolt_id, version_id, segment_id, defect_id, "
                    "assigned_to, status, due_at, created_at) "
                    "VALUES(?,?,?,?,?,?,'queued',?,?)",
                    (rid, bolt_id, bolt["current_version_id"], segment_id, defect_id,
                     line, _iso(due), now))
            except sqlite3.IntegrityError as e:
                raise QCError(
                    f"布卷 {bolt['bolt_no']} 已被另一条产线接收，只允许一条复检任务",
                    409, "receive_conflict") from e
        return self.get_reinspection(rid)

    def claim_reinspection(self, task_id: str, reinspector: str) -> dict:
        """复检工接收任务。复检人不得与初检人相同；超时任务不可接收。"""
        if not users.has_role(reinspector, "reinspector"):
            raise QCError("只有复检工可以接收复检任务", 403, "forbidden")
        with self._tx():
            task = self.conn.execute(
                "SELECT r.*, v.inspector FROM reinspection r "
                "JOIN inspection_version v ON v.id=r.version_id WHERE r.id=?",
                (task_id,)).fetchone()
            if not task:
                raise QCError("复检任务不存在", 404, "not_found")
            if reinspector == task["inspector"]:
                raise QCError("复检人与初检人不能相同", 403, "inspector_conflict")
            if task["status"] == "overdue":
                raise QCError("复检任务已超时，需主任重新派发后才能接收", 409, "overdue")
            if task["status"] != "queued":
                raise QCError(f"任务当前状态 {task['status']}，不能接收", 409, "bad_state")
            cur = self.conn.execute(
                "UPDATE reinspection SET status='accepted', claimed_by=?, claimed_at=? "
                "WHERE id=? AND status='queued'",
                (reinspector, _iso(self.clock()), task_id))
            if cur.rowcount != 1:
                raise QCError("任务已被其他人接收", 409, "claim_conflict")
            self.conn.execute(
                "UPDATE bolt SET status='reinspecting', updated_at=? WHERE id=?",
                (_iso(self.clock()), task["bolt_id"]))
        return self.get_reinspection(task_id)

    def complete_reinspection(self, task_id: str, reinspector: str,
                              result: str, comment: str = "") -> dict:
        if result not in ("confirmed", "cleared", "escalated"):
            raise QCError("复检结论必须为 confirmed/cleared/escalated")
        late = False
        with self._tx():
            task = self.conn.execute(
                "SELECT * FROM reinspection WHERE id=?", (task_id,)).fetchone()
            if not task:
                raise QCError("复检任务不存在", 404, "not_found")
            if task["claimed_by"] != reinspector:
                raise QCError("只有接收人可以完成该复检任务", 403, "forbidden")
            if task["status"] == "overdue":
                raise QCError("任务已超时，请先申请重新派发", 409, "overdue")
            if task["status"] != "accepted":
                raise QCError(f"任务当前状态 {task['status']}，不能完成", 409, "bad_state")
            if _iso(self.clock()) > task["due_at"]:
                # 提交时已超时：状态与提醒先落盘提交，再在事务外告知调用方
                self.conn.execute(
                    "UPDATE reinspection SET status='overdue' WHERE id=?", (task_id,))
                self._reminder(task_id, "复检任务在提交时已超过期限，已自动转超时")
                late = True
            else:
                self.conn.execute(
                    "UPDATE reinspection SET status='completed', result=?, comment=?, "
                    "completed_at=? WHERE id=?",
                    (result, comment, _iso(self.clock()), task_id))
                back = ("reinspect_after_rework"
                        if self.get_version(task["version_id"])["kind"] == "post_rework"
                        else "inspected")
                self.conn.execute("UPDATE bolt SET status=?, updated_at=? WHERE id=?",
                                  (back, _iso(self.clock()), task["bolt_id"]))
        if late:
            raise QCError("任务已超过复检期限，自动转为超时", 409, "overdue")
        return self.get_reinspection(task_id)

    def requeue_reinspection(self, task_id: str, manager: str,
                             sla_hours: int | None = None) -> dict:
        """超时任务由主任重新派发。"""
        if not users.has_role(manager, "manager"):
            raise QCError("只有主任可以重新派发超时任务", 403, "forbidden")
        due = _iso(self.clock() + timedelta(hours=sla_hours or self.sla_hours))
        with self._tx():
            cur = self.conn.execute(
                "UPDATE reinspection SET status='queued', claimed_by=NULL, claimed_at=NULL, "
                "due_at=? WHERE id=? AND status='overdue'", (due, task_id))
            if cur.rowcount != 1:
                raise QCError("只有超时状态的任务可以重新派发", 409, "bad_state")
            self.conn.execute(
                "UPDATE reminder SET acknowledged=1 WHERE reinspection_id=?", (task_id,))
        return self.get_reinspection(task_id)

    def get_reinspection(self, task_id: str) -> dict:
        r = self.conn.execute(
            "SELECT r.*, b.bolt_no, s.start_m, s.end_m, s.seg_index, "
            "v.inspector AS initial_inspector, v.version_no "
            "FROM reinspection r JOIN bolt b ON b.id=r.bolt_id "
            "JOIN segment s ON s.id=r.segment_id "
            "JOIN inspection_version v ON v.id=r.version_id WHERE r.id=?",
            (task_id,)).fetchone()
        if not r:
            raise QCError("复检任务不存在", 404, "not_found")
        return dict(r)

    def list_reinspections(self, status: str | None = None) -> list[dict]:
        sql = ("SELECT r.*, b.bolt_no, s.start_m, s.end_m, s.seg_index, "
               "v.inspector AS initial_inspector, v.version_no "
               "FROM reinspection r JOIN bolt b ON b.id=r.bolt_id "
               "JOIN segment s ON s.id=r.segment_id "
               "JOIN inspection_version v ON v.id=r.version_id")
        if status:
            rows = self.conn.execute(
                sql + " WHERE r.status=? ORDER BY r.due_at", (status,)).fetchall()
        else:
            rows = self.conn.execute(sql + " ORDER BY r.due_at").fetchall()
        now = _iso(self.clock())
        out = []
        for r in rows:
            d = dict(r)
            d["is_overdue"] = d["status"] in ("queued", "accepted") and d["due_at"] < now
            out.append(d)
        return out

    def scan_overdue(self) -> int:
        """进程内定时任务调用：把到期未完成的复检任务置为超时并写提醒。返回新超时数。"""
        now = _iso(self.clock())
        count = 0
        with self._tx():
            rows = self.conn.execute(
                "SELECT id FROM reinspection WHERE status IN ('queued','accepted') AND due_at<?",
                (now,)).fetchall()
            for r in rows:
                self.conn.execute(
                    "UPDATE reinspection SET status='overdue' WHERE id=?", (r["id"],))
                self._reminder(r["id"], "复检任务超过规定期限未完成，已超时待处理")
                count += 1
        return count

    def list_reminders(self, only_unacked: bool = True) -> list[dict]:
        sql = ("SELECT rm.*, b.bolt_no FROM reminder rm "
               "JOIN reinspection r ON r.id=rm.reinspection_id "
               "JOIN bolt b ON b.id=r.bolt_id")
        if only_unacked:
            rows = self.conn.execute(
                sql + " WHERE rm.acknowledged=0 ORDER BY rm.created_at DESC").fetchall()
        else:
            rows = self.conn.execute(sql + " ORDER BY rm.created_at DESC").fetchall()
        return [dict(r) for r in rows]

    def _reminder(self, task_id: str, message: str) -> None:
        exists = self.conn.execute(
            "SELECT 1 FROM reminder WHERE reinspection_id=? AND acknowledged=0",
            (task_id,)).fetchone()
        if not exists:
            self.conn.execute(
                "INSERT INTO reminder(id, reinspection_id, message, created_at) VALUES(?,?,?,?)",
                (_uid("rm"), task_id, message, _iso(self.clock())))

    # ---------------------------------------------------------------- 退修工单

    def create_rework(self, bolt_id: str, manager: str, reason: str) -> dict:
        if not users.has_role(manager, "manager"):
            raise QCError("只有主任可以开出退修工单", 403, "forbidden")
        bolt = self._bolt_row(bolt_id)
        self._require_unlocked(bolt)
        if bolt["status"] not in ("inspected", "reinspect_after_rework", "reinspecting"):
            raise QCError(f"布卷状态 {bolt['status']}，不能开退修工单", 409, "bad_state")
        defects = self.list_defects(bolt["current_version_id"])
        grade_a = [d for d in defects if d["grade"] == "A" and d["type_code"] != "missing_photo"]
        confirmed_b = []
        for d in defects:
            if d["grade"] != "B" or d["type_code"] in ("length_short",):
                continue
            r = self.conn.execute(
                "SELECT result FROM reinspection WHERE defect_id=? "
                "ORDER BY created_at DESC LIMIT 1", (d["id"],)).fetchone()
            if r and r["result"] in ("confirmed", "escalated"):
                confirmed_b.append(d)
        if not grade_a and not confirmed_b:
            raise QCError("仅当存在 A 级疵点、或 B 级疵点被复检确认/升级时才允许退修")
        now = _iso(self.clock())
        rid = _uid("rw")
        with self._tx():
            self.conn.execute(
                "INSERT INTO rework_order(id, bolt_id, version_id, reason, status, created_at) "
                "VALUES(?,?,?,?,'in_progress',?)",
                (rid, bolt_id, bolt["current_version_id"], reason, now))
            self.conn.execute(
                "UPDATE bolt SET status='reworking', updated_at=? WHERE id=?", (now, bolt_id))
            self.conn.execute(
                "UPDATE reinspection SET status='cancelled' "
                "WHERE bolt_id=? AND status IN ('queued','accepted','overdue')", (bolt_id,))
        return self.get_rework(rid)

    def finish_rework(self, order_id: str, manager: str, inspector: str) -> dict:
        """退修完工：旧版本置 superseded，生成 post_rework 新版本等待重新检验录入。"""
        if not users.has_role(manager, "manager"):
            raise QCError("只有主任可以登记退修完工", 403, "forbidden")
        users.get_user(inspector)
        order = self.get_rework(order_id)
        if order["status"] != "in_progress":
            raise QCError("退修工单不在进行中", 409, "bad_state")
        now = _iso(self.clock())
        with self._tx():
            next_no = self.conn.execute(
                "SELECT COALESCE(MAX(version_no),0)+1 FROM inspection_version WHERE bolt_id=?",
                (order["bolt_id"],)).fetchone()[0]
            vid = _uid("ver")
            self.conn.execute(
                "INSERT INTO inspection_version(id, bolt_id, version_no, kind, inspector, "
                "supersedes_id, rework_order_id, status, created_at) "
                "VALUES(?,?,?,'post_rework',?,?,?,'open',?)",
                (vid, order["bolt_id"], next_no, inspector, order["version_id"],
                 order_id, now))
            self.conn.execute(
                "UPDATE inspection_version SET status='superseded' WHERE id=?",
                (order["version_id"],))
            self.conn.execute(
                "UPDATE rework_order SET status='done', finished_at=? WHERE id=?", (now, order_id))
            self.conn.execute(
                "UPDATE bolt SET status='reinspect_after_rework', current_version_id=?, "
                "updated_at=? WHERE id=?", (vid, now, order["bolt_id"]))
        return self.get_rework(order_id)

    def get_rework(self, order_id: str) -> dict:
        r = self.conn.execute(
            "SELECT rw.*, b.bolt_no FROM rework_order rw JOIN bolt b ON b.id=rw.bolt_id "
            "WHERE rw.id=?", (order_id,)).fetchone()
        if not r:
            raise QCError("退修工单不存在", 404, "not_found")
        return dict(r)

    def list_reworks(self, status: str | None = None) -> list[dict]:
        sql = ("SELECT rw.*, b.bolt_no, v.version_no FROM rework_order rw "
               "JOIN bolt b ON b.id=rw.bolt_id "
               "JOIN inspection_version v ON v.id=rw.version_id")
        if status:
            rows = self.conn.execute(sql + " WHERE rw.status=? ORDER BY rw.created_at",
                                     (status,)).fetchall()
        else:
            rows = self.conn.execute(sql + " ORDER BY rw.created_at").fetchall()
        return [dict(r) for r in rows]

    # ---------------------------------------------------------------- 放行评估

    def evaluate_release(self, bolt_id: str) -> list[dict]:
        """返回逐项放行条件。passed=True 表示该条件满足。"""
        bolt = self._bolt_row(bolt_id)
        vid = bolt["current_version_id"]
        v = self.conn.execute("SELECT * FROM inspection_version WHERE id=?", (vid,)).fetchone()
        defects = self.list_defects(vid) if vid else []
        by_code: dict[str, list[dict]] = {}
        for d in defects:
            by_code.setdefault(d["type_code"], []).append(d)

        gates: list[dict] = []
        # 1) 检验已封板
        sealed = v is not None and v["status"] == "reinspected"
        gates.append({"key": "sealed", "label": "检验已封板", "passed": bool(sealed),
                      "detail": "当前检验版本必须完成封板" if not sealed else "已封板"})

        # 2) 照片缺失一票否决
        missing = [d for d in defects
                   if d["type_code"] == "missing_photo"
                   or (d["photo_required"] and not d["photo_path"])]
        gates.append({
            "key": "photo", "label": "疵点照片齐全",
            "passed": not missing,
            "detail": ("照片缺失：%d 处疵点待补照片" % len(missing)) if missing else "照片齐全",
            "defect_ids": [d["id"] for d in missing],
        })

        # 3) 长度不足：经理特许
        short = bolt["length_m"] + 1e-9 < bolt["nominal_length_m"] or bool(
            by_code.get("length_short"))
        gates.append({
            "key": "length_short", "label": "长度达标或经理特许",
            "passed": not short,
            "needs_concession": short,
            "detail": (f"实际 {bolt['length_m']}m 小于标称 {bolt['nominal_length_m']}m，"
                       "需经理特许放行") if short else "长度达标",
        })

        # 4) A 级 → 必须退修后新版本（新版本中不得再出现 A 级）
        grade_a = [d for d in defects if d["grade"] == "A" and d["type_code"] != "missing_photo"]
        reworked = v is not None and v["kind"] == "post_rework"
        gates.append({
            "key": "grade_A", "label": "A 级疵点已退修",
            "passed": not grade_a,
            "detail": ("存在 %d 处 A 级疵点（如断经），必须退修后生成新版本" % len(grade_a))
                      if grade_a else ("已通过退修新版本复检" if reworked else "无 A 级疵点"),
            "defect_ids": [d["id"] for d in grade_a],
        })

        # 5) B 级疵点逐点复检，结论须为 cleared 且复检人≠初检人
        #    C 级为轻微疵点，记录后可直接放行（无需复检）——分级放行差异在此体现。
        b_defects = [d for d in defects
                     if d["grade"] == "B" and d["type_code"] != "length_short"]
        pending: list[dict] = []
        must_rework: list[dict] = []
        same_person: list[dict] = []
        for d in b_defects:
            r = self.conn.execute(
                "SELECT * FROM reinspection WHERE defect_id=? ORDER BY created_at DESC LIMIT 1",
                (d["id"],)).fetchone()
            if not r or r["status"] != "completed":
                pending.append(d)
            elif r["claimed_by"] == v["inspector"]:
                same_person.append(d)
            elif r["result"] in ("confirmed", "escalated"):
                must_rework.append(d)
            # result == 'cleared' → 通过
        gates.append({
            "key": "reinspection_b", "label": "B 级疵点复检通过",
            "passed": not pending and not must_rework and not same_person,
            "detail": ("待复检 %d 处；复检确认/升级须退修 %d 处；复检人违规 %d 处"
                       % (len(pending), len(must_rework), len(same_person)))
                      if (pending or must_rework or same_person)
                      else f"{len(b_defects)} 处 B 级疵点复检通过",
            "pending_ids": [d["id"] for d in pending],
            "rework_ids": [d["id"] for d in must_rework],
            "same_person_ids": [d["id"] for d in same_person],
        })

        # 6) C 级疵点（轻微）信息提示项，不阻断放行
        c_defects = [d for d in defects
                     if d["grade"] == "C" and d["type_code"] != "missing_photo"]
        gates.append({
            "key": "grade_c_info", "label": "C 级轻微疵点",
            "passed": True,
            "detail": (f"{len(c_defects)} 处轻微疵点，记录后可放行"
                       if c_defects else "无 C 级疵点"),
        })
        return gates

    def release(self, bolt_id: str, user: str, concession: bool = False,
                decision: str = "release", basis_note: str = "") -> dict:
        users.get_user(user)
        bolt = self._bolt_row(bolt_id)
        self._require_unlocked(bolt)
        if bolt["status"] not in ("inspected", "reinspect_after_rework"):
            raise QCError(f"布卷状态 {bolt['status']}，不能放行", 409, "bad_state")
        gates = self.evaluate_release(bolt_id)
        blockers = [g for g in gates if not g["passed"]]

        # 长度不足是唯一可被经理特许豁免的项
        con_gate = next(g for g in gates if g["key"] == "length_short")
        if con_gate["needs_concession"]:
            if not concession:
                raise QCError(con_gate["detail"] + "（需经理特许）", 403, "concession_required")
            if not users.has_role(user, "manager"):
                raise QCError("长度不足特许只能由经理签发", 403, "forbidden")
            blockers = [g for g in blockers if g["key"] != "length_short"]

        if decision == "release":
            if not (users.has_role(user, "inspector") or users.has_role(user, "manager")):
                raise QCError("只有初检工或经理可以放行", 403, "forbidden")
            if blockers:
                raise QCError("放行条件未满足：" + "；".join(g["detail"] for g in blockers),
                              422, "release_blocked")
        else:  # reject
            if not users.has_role(user, "manager"):
                raise QCError("拒收只能由经理签发", 403, "forbidden")

        real = [d for d in self.list_defects(bolt["current_version_id"])
                if d["type_code"] != "missing_photo"]
        max_grade = max((d["grade"] for d in real),
                        key=lambda g: GRADE_RANK[g], default="C")
        basis = basis_note or self._basis_text(decision, max_grade, gates, concession)
        now = _iso(self.clock())
        with self._tx():
            rid = _uid("rel")
            self.conn.execute(
                "INSERT INTO release_record(id, bolt_id, version_id, released_by, decision, "
                "basis, created_at) VALUES(?,?,?,?,?,?,?)",
                (rid, bolt_id, bolt["current_version_id"], user, decision, basis, now))
            self.conn.execute(
                "UPDATE inspection_version SET status=? WHERE id=?",
                ("released" if decision == "release" else "rejected",
                 bolt["current_version_id"]))
            self.conn.execute(
                "UPDATE bolt SET status=?, released_version_id=current_version_id, "
                "updated_at=? WHERE id=?", ("released" if decision == "release" else "rejected",
                                            now, bolt_id))
        return self.get_release(rid)

    @staticmethod
    def _basis_text(decision: str, max_grade: str, gates: list[dict], concession: bool) -> str:
        if decision == "reject":
            return "放行条件不满足，经理签发拒收"
        if concession:
            return f"最高疵点等级 {max_grade}；长度不足由经理特许；其余条件均满足"
        if max_grade == "C":
            return "最高疵点等级 C（轻微），记录后直接放行"
        if max_grade == "B":
            return "最高疵点等级 B，逐点复检通过后放行"
        return "最高疵点等级 A，退修新版本复检通过后放行"

    def get_release(self, release_id: str) -> dict:
        r = self.conn.execute(
            "SELECT rr.*, b.bolt_no FROM release_record rr JOIN bolt b ON b.id=rr.bolt_id "
            "WHERE rr.id=?", (release_id,)).fetchone()
        if not r:
            raise QCError("放行记录不存在", 404, "not_found")
        return dict(r)

    def list_releases(self) -> list[dict]:
        rows = self.conn.execute(
            "SELECT rr.*, b.bolt_no FROM release_record rr JOIN bolt b ON b.id=rr.bolt_id "
            "ORDER BY rr.created_at DESC").fetchall()
        return [dict(r) for r in rows]

    # ---------------------------------------------------------------- 疵点地图

    def defect_map(self, bolt_id: str) -> dict:
        bolt = self.get_bolt(bolt_id)
        versions = []
        for v in self.list_versions(bolt_id):
            segs = []
            for s in self.list_segments(v["id"]):
                seg = dict(s)
                seg["defects"] = [d for d in self.list_defects(v["id"])
                                  if d["segment_id"] == s["id"]]
                segs.append(seg)
            versions.append({"version": v, "segments": segs})
        return {"bolt": bolt, "versions": versions}

    # ---------------------------------------------------------------- 照片

    def _require_photo_role(self, user: str, allowed: set[str]) -> None:
        if user not in users.USERS:
            raise QCError(f"用户不存在: {user}", 403, "unknown_user")
        if not any(users.has_role(user, r) for r in allowed):
            raise QCError("没有照片操作权限", 403, "forbidden")

    def _write_photo_file(self, bolt_no: str, defect_id: str,
                          data: bytes, ext: str) -> str:
        ext = ext if ext.startswith(".") else f".{ext}"
        rel_dir = Path(bolt_no)
        abs_dir = self.photo_root / rel_dir
        abs_dir.mkdir(parents=True, exist_ok=True)
        rel_path = rel_dir / f"{defect_id}{ext}"
        (self.photo_root / rel_path).write_bytes(data)
        return str(rel_path)

    def photo_path_for_view(self, defect_id: str, user: str) -> Path:
        """查看照片：三类岗位均可；路径必须落在照片根目录内（防目录穿越）。"""
        self._require_photo_role(user, users.PHOTO_VIEW_ROLES)
        d = self.get_defect(defect_id)
        if not d["photo_path"]:
            raise QCError("该疵点没有照片", 404, "no_photo")
        root = self.photo_root.resolve()
        target = (root / d["photo_path"]).resolve()
        if root not in target.parents and target != root:
            raise QCError("非法照片路径", 403, "forbidden")
        if not target.exists():
            raise QCError("照片文件缺失", 404, "no_photo")
        return target
