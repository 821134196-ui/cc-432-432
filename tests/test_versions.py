"""检验版本关联：退修后生成新版本、supersedes 链、旧任务取消。"""
from tests.base import PNG_1X1, QCBase


class TestVersionChain(QCBase):
    def _warp_bolt(self, no="V-001"):
        self.make_bolt(no)
        return self.inspect(
            no, inspector="wang",
            defects=((0, "warp_break", 30, True, None),
                     (0, "stain", 80, True, None)))

    def test_rework_creates_new_version_chain(self):
        b, v1, _, _ = self._warp_bolt()
        rw = self.svc.create_rework(b["id"], "zhang", "断经退修")
        self.svc.finish_rework(rw["id"], "zhang", "zhao")

        versions = self.svc.list_versions(b["id"])
        self.assertEqual([x["version_no"] for x in versions], [1, 2])
        old, new = versions
        self.assertEqual(old["status"], "superseded")
        self.assertEqual(new["kind"], "post_rework")
        self.assertEqual(new["supersedes_id"], old["id"])
        self.assertEqual(new["rework_order_id"], rw["id"])

        b2 = self.svc.get_bolt(b["id"])
        self.assertEqual(b2["current_version_id"], new["id"])
        self.assertEqual(b2["status"], "reinspect_after_rework")

    def test_new_version_starts_without_segments_or_defects(self):
        b, v1, _, _ = self._warp_bolt()
        rw = self.svc.create_rework(b["id"], "zhang", "退修")
        self.svc.finish_rework(rw["id"], "zhang", "zhao")
        new_id = self.svc.get_bolt(b["id"])["current_version_id"]
        self.assertEqual(self.svc.list_segments(new_id), [])
        self.assertEqual(self.svc.list_defects(new_id), [])

    def test_post_rework_version_cannot_be_opened_directly(self):
        b, v1, _, _ = self._warp_bolt()
        with self.expect_error():
            self.svc.open_version(b["id"], "wang", kind="post_rework")

    def test_rework_cancels_active_reinspections(self):
        b, v, segs, defs = self._warp_bolt("V-002")
        # A 级疵点布卷存在一条已接收、未完成的复检任务，退修时应被取消
        t = self.svc.create_reinspection(b["id"], segs[0]["id"], "line1", defect_id=defs[0]["id"])
        t = self.svc.claim_reinspection(t["id"], "li")
        self.assertEqual(t["status"], "accepted")
        rw = self.svc.create_rework(b["id"], "zhang", "A 级退修")
        self.assertEqual(self.svc.get_reinspection(t["id"])["status"], "cancelled")
        self.assertEqual(self.svc.get_bolt(b["id"])["status"], "reworking")
        self.assertEqual(self.svc.get_rework(rw["id"])["status"], "in_progress")

    def test_rework_not_allowed_for_clean_bolt(self):
        self.make_bolt("V-003")
        b, v, segs, defs = self.inspect(
            "V-003", "wang", defects=((0, "stain", 20, True, None),))
        d = defs[0]
        t = self.svc.create_reinspection(b["id"], segs[0]["id"], "line1", defect_id=d["id"])
        t = self.svc.claim_reinspection(t["id"], "li")
        self.svc.complete_reinspection(t["id"], "li", "cleared", "合格")
        with self.expect_error():
            self.svc.create_rework(b["id"], "zhang", "无理由退修")

    def test_only_manager_opens_or_finishes_rework(self):
        b, v1, _, _ = self._warp_bolt("V-004")
        with self.expect_error("forbidden", 403):
            self.svc.create_rework(b["id"], "li", "越权")
        rw = self.svc.create_rework(b["id"], "zhang", "ok")
        with self.expect_error("forbidden", 403):
            self.svc.finish_rework(rw["id"], "li", "zhao")
