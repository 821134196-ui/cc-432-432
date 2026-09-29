"""演示数据播种：织机、颜色批次、布卷与完整检验/复检/退修/放行样例。

一条 `python3 -m textile_qc` 即自动建库并写入下列样例：
  A01 初检→复检→直接放行（B 级疵点复检清除）
  A02 A 级断经→退修→新版本复检→放行
  A03 待检布卷
  A04 检验中、存在照片缺失
  A05 长度不足→经理特许放行
  A06 复检超时（带提醒）
  A07 退修进行中
  A08 B 级疵点等待复检
  A09 A 级疵点经理拒收
"""
from __future__ import annotations

import sqlite3
import struct
import zlib
from pathlib import Path

from .db import init_db
from .service import QCService

LOOMS = [
    ("JM-101", "1号喷气织机", "织造一车间"),
    ("JM-102", "2号喷气织机", "织造一车间"),
    ("JM-205", "5号剑杆织机", "织造二车间"),
]
COLORS = [
    ("CB-LN01", "亚麻原色", "DYE-2026-0901"),
    ("CB-HS02", "锈红", "DYE-2026-0903"),
    ("CB-HL03", "灰蓝", "DYE-2026-0905"),
]

# 疵点类型 -> 演示照片颜色
_PHOTO_COLORS = {
    "warp_break": (140, 64, 48),
    "color_diff": (107, 112, 118),
    "stain": (92, 78, 60),
}


def demo_photo(type_code: str, label: str) -> bytes:
    """生成一张纯色 120x90 PNG（无第三方依赖），充当疵点照片。"""
    rgb = _PHOTO_COLORS.get(type_code, (120, 110, 90))
    w, h = 120, 90
    raw = bytearray()
    for y in range(h):
        raw.append(0)  # PNG filter type 0
        for x in range(w):
            shade = 0 if (x // 12 + y // 12) % 5 == 0 else 1
            raw.extend((min(255, rgb[0] + shade * 26),
                        min(255, rgb[1] + shade * 26),
                        min(255, rgb[2] + shade * 26)))

    def chunk(tag: bytes, data: bytes) -> bytes:
        return (struct.pack(">I", len(data)) + tag + data
                + struct.pack(">I", zlib.crc32(tag + data) & 0xFFFFFFFF))

    ihdr = struct.pack(">IIBBBBB", w, h, 8, 2, 0, 0, 0)
    return (b"\x89PNG\r\n\x1a\n"
            + chunk(b"IHDR", ihdr)
            + chunk(b"IDAT", zlib.compress(bytes(raw), 9))
            + chunk(b"IEND", b""))


def seed_dictionaries(conn: sqlite3.Connection, svc: QCService) -> None:
    init_db(conn)
    for code, name, ws in LOOMS:
        conn.execute("INSERT INTO loom(id, code, name, workshop) VALUES(?,?,?,?) "
                     "ON CONFLICT(code) DO NOTHING",
                     (f"loom_{code[-3:]}", code, name, ws))
    for code, cname, lot in COLORS:
        conn.execute("INSERT INTO color_batch(id, code, color_name, dye_lot) "
                     "VALUES(?,?,?,?) ON CONFLICT(code) DO NOTHING",
                     (f"cb_{code[-3:]}", code, cname, lot))
    conn.commit()
    svc.seed_dictionaries()


def _add(svc: QCService, version_id: str, start: float, end: float,
         dtype: str, pos: float, by: str, with_photo: bool = True,
         grade: str | None = None) -> str:
    seg = svc.add_segment(version_id, start, end, actor=by)
    d = svc.add_defect(
        seg["id"], dtype, pos, by, grade=grade,
        photo_data=demo_photo(dtype, dtype) if with_photo else None)
    return d["id"]


def seed_demo(conn: sqlite3.Connection, photo_root: str | Path) -> QCService:
    svc = QCService(conn, photo_root)
    seed_dictionaries(conn, svc)

    if conn.execute("SELECT COUNT(*) FROM bolt").fetchone()[0] > 0:
        return svc  # 已有演示数据，不重复播种

    # ---- A01：初检 → 双段复检清除 → 直接放行 -------------------------------
    b = svc.register_bolt("B20260901-A01", "JM-101", "CB-LN01", 120, 120, 150)
    v = svc.open_version(b["id"], "wang")
    d1 = _add(svc, v["id"], 0, 40, "stain", 25, "wang")
    d2 = _add(svc, v["id"], 40, 80, "color_diff", 65, "wang")
    svc.add_segment(v["id"], 80, 120, actor="wang")
    svc.complete_version(v["id"])
    segs = svc.list_segments(v["id"])
    seg0 = next(s for s in segs if s["start_m"] == 0)
    seg1 = next(s for s in segs if s["start_m"] == 40)
    t1 = svc.create_reinspection(b["id"], seg0["id"], "line1", defect_id=d1)
    t1 = svc.claim_reinspection(t1["id"], "li")
    svc.complete_reinspection(t1["id"], "li", "cleared", "污渍已清洗，复检无痕迹")
    t2 = svc.create_reinspection(b["id"], seg1["id"], "line2", defect_id=d2)
    t2 = svc.claim_reinspection(t2["id"], "chen")
    svc.complete_reinspection(t2["id"], "chen", "cleared", "色差在允收范围")
    svc.release(b["id"], "wang")

    # ---- A02：A 级断经 → 退修 → 新版本复检 → 放行 ---------------------------
    b = svc.register_bolt("B20260901-A02", "JM-102", "CB-HS02", 120, 120, 148)
    v = svc.open_version(b["id"], "wang")
    _add(svc, v["id"], 0, 60, "warp_break", 30, "wang")
    _add(svc, v["id"], 60, 120, "stain", 85, "wang")
    svc.complete_version(v["id"])
    rw = svc.create_rework(b["id"], "zhang", "A 级断经，整段换经重织")
    svc.finish_rework(rw["id"], "zhang", "zhao")
    v2_id = svc.get_bolt(b["id"])["current_version_id"]
    svc.add_segment(v2_id, 0, 60, actor="zhao")
    d2 = _add(svc, v2_id, 60, 120, "stain", 90, "zhao")
    svc.complete_version(v2_id)
    seg = svc.list_segments(v2_id)[1]
    t = svc.create_reinspection(b["id"], seg["id"], "line1", defect_id=d2)
    t = svc.claim_reinspection(t["id"], "chen")
    svc.complete_reinspection(t["id"], "chen", "cleared", "退修后复检合格")
    svc.release(b["id"], "zhao")

    # ---- A03：待检 ----------------------------------------------------------
    svc.register_bolt("B20260901-A03", "JM-205", "CB-HL03", 120, 120, 150)

    # ---- A04：检验中 + 照片缺失（录入疵点时未附照片） ----------------------
    b = svc.register_bolt("B20260901-A04", "JM-101", "CB-HL03", 120, 120, 150)
    v = svc.open_version(b["id"], "zhao")
    seg = svc.add_segment(v["id"], 0, 60, actor="zhao")
    svc.add_defect(seg["id"], "stain", 18, "zhao", photo_data=None)  # 自动挂照片缺失

    # ---- A05：长度不足 → 经理特许放行 --------------------------------------
    b = svc.register_bolt("B20260901-A05", "JM-205", "CB-LN01", 112.5, 120, 150)
    v = svc.open_version(b["id"], "zhao")
    svc.add_segment(v["id"], 0, 112.5, actor="zhao")
    svc.complete_version(v["id"])  # 封板自动登记长度不足
    svc.release(b["id"], "zhang", concession=True)

    # ---- A06：复检超时（进程扫描后产生提醒） --------------------------------
    b = svc.register_bolt("B20260901-A06", "JM-102", "CB-HL03", 120, 120, 148)
    v = svc.open_version(b["id"], "wang")
    _add(svc, v["id"], 0, 60, "stain", 20, "wang")
    svc.add_segment(v["id"], 60, 120, actor="wang")
    svc.complete_version(v["id"])
    seg = svc.list_segments(v["id"])[0]
    overdue_task = svc.create_reinspection(b["id"], seg["id"], "line1", sla_hours=-1)
    svc.scan_overdue()  # 立即置为超时并写提醒
    assert svc.get_reinspection(overdue_task["id"])["status"] == "overdue"

    # ---- A07：退修进行中 ----------------------------------------------------
    b = svc.register_bolt("B20260901-A07", "JM-101", "CB-HS02", 120, 120, 150)
    v = svc.open_version(b["id"], "wang")
    _add(svc, v["id"], 0, 120, "warp_break", 72, "wang")
    svc.complete_version(v["id"])
    svc.create_rework(b["id"], "zhang", "断经密集，安排换经")

    # ---- A08：已封板，B 级疵点等待复检（复检队列） --------------------------
    b = svc.register_bolt("B20260901-A08", "JM-205", "CB-LN01", 120, 120, 150)
    v = svc.open_version(b["id"], "zhao")
    _add(svc, v["id"], 0, 60, "color_diff", 42, "zhao")
    svc.add_segment(v["id"], 60, 120, actor="zhao")
    svc.complete_version(v["id"])
    seg = svc.list_segments(v["id"])[0]
    defects = svc.list_defects(v["id"])
    cd = next(d for d in defects if d["type_code"] == "color_diff")
    svc.create_reinspection(b["id"], seg["id"], "line2", defect_id=cd["id"])

    # ---- A09：A 级断经无法放行，经理拒收 ------------------------------------
    b = svc.register_bolt("B20260901-A09", "JM-102", "CB-HS02", 120, 120, 148)
    v = svc.open_version(b["id"], "zhao")
    _add(svc, v["id"], 0, 120, "warp_break", 105, "zhao")
    svc.complete_version(v["id"])
    svc.release(b["id"], "zhang", decision="reject")

    return svc
