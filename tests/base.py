"""测试基件：临时 SQLite + 照片目录 + 快速造数辅助。"""
from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from textile_qc.db import connect
from textile_qc.demo import seed_dictionaries
from textile_qc.service import QCError, QCService

PNG_1X1 = bytes.fromhex(
    "89504e470d0a1a0a0000000d49484452000000010000000108020000009077"
    "53de0000000c4944415408d76360f8cfc0f01f00050500015fc8f1d20000000049454e44ae426082")


class QCBase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        root = Path(self.tmp.name)
        self.conn = connect(root / "qc.db")
        seed_dictionaries(self.conn, QCService(self.conn, root / "photos"))
        self.svc = QCService(self.conn, root / "photos")
        self.photo_root = root / "photos"

    def tearDown(self):
        self.conn.close()
        self.tmp.cleanup()

    def make_bolt(self, bolt_no="T-001", length=120.0, nominal=120.0, width=150.0,
                  loom="JM-101", color="CB-LN01"):
        return self.svc.register_bolt(bolt_no, loom, color, length, nominal, width)

    def inspect(self, bolt_id="T-001", inspector="wang", ranges=((0, 120),),
                defects=()):
        """开检 → 加段 → 录疵点 → 封板。defects: (seg_idx, type, pos, photo, grade)"""
        b = self.svc.get_bolt(bolt_id)
        v = self.svc.open_version(b["id"], inspector)
        segs = [self.svc.add_segment(v["id"], s, e, actor=inspector)
                for s, e in ranges]
        out_defs = []
        for idx, dtype, pos, with_photo, grade in defects:
            out_defs.append(self.svc.add_defect(
                segs[idx]["id"], dtype, pos, inspector, grade=grade,
                photo_data=PNG_1X1 if with_photo else None))
        self.svc.complete_version(v["id"])
        return self.svc.get_bolt(bolt_id), self.svc.get_version(v["id"]), segs, out_defs

    def expect_error(self, code=None, status=None):
        class Ctx:
            def __enter__(self_inner):
                return self_inner

            def __exit__(self_inner, exc_type, exc, tb):
                self.assertIsNotNone(exc_type, "预期应抛出 QCError")
                self.assertIsInstance(exc, QCError)
                if code:
                    self.assertEqual(exc.code, code, f"错误码不符: {exc.code} != {code} ({exc.message})")
                if status:
                    self.assertEqual(exc.http_status, status)
                return True

        return Ctx()
