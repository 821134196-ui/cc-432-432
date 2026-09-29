"""分级放行条件：A 退修 / B 复检 / C 直接放行 / 照片缺失否决 / 长度不足特许。"""
from tests.base import PNG_1X1, QCBase


def gate_map(gates):
    return {g["key"]: g for g in gates}


class TestReleaseConditions(QCBase):
    def test_clean_bolt_release(self):
        self.make_bolt("R-001")
        self.inspect("R-001")  # 无疵点
        rec = self.svc.release(self.svc.get_bolt("R-001")["id"], "wang")
        self.assertEqual(rec["decision"], "release")
        self.assertEqual(self.svc.get_bolt("R-001")["status"], "released")

    def test_grade_c_direct_release(self):
        self.make_bolt("R-002")
        b, v, segs, defs = self.inspect(
            "R-002", "wang", defects=((0, "stain", 10, True, "C"),))
        # C 级污渍：无需复检，记录后可直接放行
        gates = gate_map(self.svc.evaluate_release(b["id"]))
        self.assertTrue(gates["grade_c_info"]["passed"])
        rec = self.svc.release(b["id"], "wang")
        self.assertIn("C", rec["basis"])

    def test_grade_b_requires_cleared_reinspection(self):
        self.make_bolt("R-003")
        b, v, segs, defs = self.inspect(
            "R-003", "wang", defects=((0, "stain", 20, True, None),))
        with self.expect_error("release_blocked", 422):
            self.svc.release(b["id"], "wang")  # B 级未复检
        t = self.svc.create_reinspection(b["id"], segs[0]["id"], "line1", defect_id=defs[0]["id"])
        t = self.svc.claim_reinspection(t["id"], "li")
        self.svc.complete_reinspection(t["id"], "li", "cleared", "合格")
        rec = self.svc.release(b["id"], "wang")
        self.assertEqual(rec["decision"], "release")

    def test_grade_b_confirmed_forces_rework(self):
        self.make_bolt("R-004")
        b, v, segs, defs = self.inspect(
            "R-004", "wang", defects=((0, "stain", 20, True, None),))
        t = self.svc.create_reinspection(b["id"], segs[0]["id"], "line1", defect_id=defs[0]["id"])
        t = self.svc.claim_reinspection(t["id"], "li")
        self.svc.complete_reinspection(t["id"], "li", "confirmed", "污渍确认")
        with self.expect_error("release_blocked", 422):
            self.svc.release(b["id"], "wang")
        # confirmed 允许开退修
        rw = self.svc.create_rework(b["id"], "zhang", "复检确认后退修")
        self.assertEqual(rw["status"], "in_progress")

    def test_grade_a_must_rework_to_new_version(self):
        self.make_bolt("R-005")
        b, v, segs, defs = self.inspect(
            "R-005", "wang", defects=((0, "warp_break", 30, True, None),))
        with self.expect_error("release_blocked", 422):
            self.svc.release(b["id"], "wang")
        rw = self.svc.create_rework(b["id"], "zhang", "断经退修")
        self.svc.finish_rework(rw["id"], "zhang", "zhao")
        new_id = self.svc.get_bolt(b["id"])["current_version_id"]
        # 新版本未录入检验、未封板，放行条件不满足
        with self.expect_error("release_blocked", 422):
            self.svc.release(b["id"], "wang")
        self.svc.add_segment(new_id, 0, 120, actor="zhao")
        self.svc.complete_version(new_id)
        rec = self.svc.release(b["id"], "zhao")
        self.assertEqual(rec["decision"], "release")

    def test_missing_photo_blocks_release_until_supplied(self):
        self.make_bolt("R-006")
        b, v, segs, defs = self.inspect(
            "R-006", "wang", defects=((0, "stain", 20, False, "C"),))
        # 无照片 → 自动挂了一条 missing_photo，放行被一票否决
        defects = self.svc.list_defects(v["id"])
        missing = [d for d in defects if d["type_code"] == "missing_photo"]
        self.assertEqual(len(missing), 1)
        gates = gate_map(self.svc.evaluate_release(b["id"]))
        self.assertFalse(gates["photo"]["passed"])
        with self.expect_error("release_blocked", 422):
            self.svc.release(b["id"], "wang")
        # 补照片后缺失记录自动清除
        self.svc.attach_photo(defs[0]["id"], "wang", PNG_1X1)
        defects = self.svc.list_defects(v["id"])
        self.assertFalse(any(d["type_code"] == "missing_photo" for d in defects))
        rec = self.svc.release(b["id"], "wang")
        self.assertEqual(rec["decision"], "release")

    def test_length_short_requires_manager_concession(self):
        self.make_bolt("R-007", length=110, nominal=120)
        b, v, segs, defs = self.inspect("R-007", "wang", ranges=((0, 110),))
        # 封板自动登记长度不足
        self.assertTrue(any(d["type_code"] == "length_short"
                            for d in self.svc.list_defects(v["id"])))
        with self.expect_error("concession_required", 403):
            self.svc.release(b["id"], "wang")  # 未特许
        with self.expect_error("forbidden", 403):
            self.svc.release(b["id"], "li", concession=True)  # 复检工不能特许
        rec = self.svc.release(b["id"], "zhang", concession=True)  # 经理特许
        self.assertEqual(rec["decision"], "release")
        self.assertIn("特许", rec["basis"])

    def test_released_bolt_cannot_go_back_to_pending(self):
        self.make_bolt("R-008")
        self.inspect("R-008")
        self.svc.release(self.svc.get_bolt("R-008")["id"], "wang")
        b = self.svc.get_bolt("R-008")
        with self.expect_error("bolt_locked", 409):
            self.svc.open_version(b["id"], "wang")
        with self.expect_error("bolt_locked", 409):
            self.svc.release(b["id"], "wang")

    def test_reject_only_by_manager(self):
        self.make_bolt("R-009")
        b, v, segs, defs = self.inspect(
            "R-009", "wang", defects=((0, "warp_break", 30, True, None),))
        with self.expect_error("forbidden", 403):
            self.svc.release(b["id"], "wang", decision="reject")
        rec = self.svc.release(b["id"], "zhang", decision="reject")
        self.assertEqual(rec["decision"], "reject")
        self.assertEqual(self.svc.get_bolt("R-009")["status"], "rejected")

    def test_reinspector_cannot_release(self):
        self.make_bolt("R-010")
        self.inspect("R-010")
        with self.expect_error("forbidden", 403):
            self.svc.release(self.svc.get_bolt("R-010")["id"], "li")
