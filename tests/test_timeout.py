"""复检超时：扫描转超时、提醒、主任重派、提交时已超时处理；以及初检人≠复检人。"""
from datetime import datetime, timedelta, timezone

from tests.base import QCBase


class TestTimeout(QCBase):
    def _queued_b_task(self, no="T-001", sla_hours=24):
        self.make_bolt(no)
        b, v, segs, defs = self.inspect(
            no, "wang", defects=((0, "stain", 20, True, None),))
        t = self.svc.create_reinspection(b["id"], segs[0]["id"], "line1",
                                         defect_id=defs[0]["id"], sla_hours=sla_hours)
        return b, t

    def test_scan_marks_overdue_and_raises_reminder(self):
        _, t = self._queued_b_task("O-001", sla_hours=-1)
        n = self.svc.scan_overdue()
        self.assertEqual(n, 1)
        self.assertEqual(self.svc.get_reinspection(t["id"])["status"], "overdue")
        reminders = self.svc.list_reminders()
        self.assertEqual(len(reminders), 1)
        self.assertEqual(reminders[0]["reinspection_id"], t["id"])
        # 重复扫描不产生重复提醒
        self.assertEqual(self.svc.scan_overdue(), 0)

    def test_overdue_task_cannot_be_claimed(self):
        _, t = self._queued_b_task("O-002", sla_hours=-1)
        self.svc.scan_overdue()
        with self.expect_error("overdue", 409):
            self.svc.claim_reinspection(t["id"], "li")

    def test_manager_requeue_then_claim_and_complete(self):
        b, t = self._queued_b_task("O-003", sla_hours=-1)
        self.svc.scan_overdue()
        with self.expect_error("forbidden", 403):
            self.svc.requeue_reinspection(t["id"], "li")   # 复检工无权重派
        t2 = self.svc.requeue_reinspection(t["id"], "zhang", sla_hours=24)
        self.assertEqual(t2["status"], "queued")
        self.assertIsNone(t2["claimed_by"])
        t2 = self.svc.claim_reinspection(t2["id"], "chen")
        t2 = self.svc.complete_reinspection(t2["id"], "chen", "cleared", "合格")
        self.assertEqual(t2["status"], "completed")

    def test_accepted_task_going_overdue_is_blocked_at_complete(self):
        # 接收时未超时，提交时刚好超时：complete 应自动转超时并拒绝
        clock = {"now": datetime(2026, 9, 29, 8, 0, tzinfo=timezone.utc)}
        self.svc.clock = lambda: clock["now"]
        self.make_bolt("O-004")
        b = self.svc.get_bolt("O-004")
        v = self.svc.open_version(b["id"], "wang")
        seg = self.svc.add_segment(v["id"], 0, 120, actor="wang")
        d = self.svc.add_defect(seg["id"], "stain", 20, "wang", photo_data=b"x")
        self.svc.complete_version(v["id"])
        t = self.svc.create_reinspection(b["id"], seg["id"], "line1",
                                         defect_id=d["id"], sla_hours=24)
        t = self.svc.claim_reinspection(t["id"], "li")
        clock["now"] += timedelta(hours=25)
        with self.expect_error("overdue", 409):
            self.svc.complete_reinspection(t["id"], "li", "cleared", "晚了")
        self.assertEqual(self.svc.get_reinspection(t["id"])["status"], "overdue")


class TestInspectorSeparation(QCBase):
    def test_reinspector_same_as_initial_inspector_rejected(self):
        # sun 同时具备初检/复检角色，自己初检自己复检必须被拒
        self.make_bolt("S-001")
        b = self.svc.get_bolt("S-001")
        v = self.svc.open_version(b["id"], "sun")
        seg = self.svc.add_segment(v["id"], 0, 120, actor="sun")
        d = self.svc.add_defect(seg["id"], "stain", 20, "sun", photo_data=b"x")
        self.svc.complete_version(v["id"])
        t = self.svc.create_reinspection(b["id"], seg["id"], "line1", defect_id=d["id"])
        with self.expect_error("inspector_conflict", 403):
            self.svc.claim_reinspection(t["id"], "sun")
        # 另一名复检工可以接收
        t = self.svc.claim_reinspection(t["id"], "chen")
        self.assertEqual(t["claimed_by"], "chen")

    def test_only_reinspector_role_can_claim(self):
        self.make_bolt("S-002")
        b = self.svc.get_bolt("S-002")
        v = self.svc.open_version(b["id"], "wang")
        seg = self.svc.add_segment(v["id"], 0, 120, actor="wang")
        self.svc.add_defect(seg["id"], "stain", 20, "wang", photo_data=b"x")
        self.svc.complete_version(v["id"])
        t = self.svc.create_reinspection(b["id"], seg["id"], "line1")
        with self.expect_error("forbidden", 403):
            self.svc.claim_reinspection(t["id"], "zhang")  # 主任不能复检
        with self.expect_error("forbidden", 403):
            self.svc.claim_reinspection(t["id"], "wang")   # 初检工不能复检
