"""接收竞争：同一布卷两条产线并发接收，只允许一条成功。"""
import threading

from tests.base import QCBase


class TestReceiveContention(QCBase):
    def _inspected_bolt_with_two_segments(self, no="C-001"):
        self.make_bolt(no)
        b, v, segs, defs = self.inspect(
            no, "wang", ranges=((0, 60), (60, 120)),
            defects=((0, "stain", 20, True, None),
                     (1, "color_diff", 90, True, None)))
        return b, segs

    def test_sequential_second_receive_conflicts(self):
        b, segs = self._inspected_bolt_with_two_segments()
        self.svc.create_reinspection(b["id"], segs[0]["id"], "line1")
        # 即使引用另一个检验段，同一布卷也不能有第二条活跃任务
        with self.expect_error("receive_conflict", 409):
            self.svc.create_reinspection(b["id"], segs[1]["id"], "line2")

    def test_concurrent_receive_only_one_succeeds(self):
        results = []
        barrier = threading.Barrier(6)

        def receive(bid, seg_id, line):
            barrier.wait()
            try:
                t = self.svc.create_reinspection(bid, seg_id, line)
                results.append(("ok", line, t["id"]))
            except Exception as e:  # noqa: BLE001
                results.append(("fail", line, type(e).__name__))

        # 三个布卷 × 两条产线并发抢收
        prepared = []
        for i in range(3):
            no = f"C-{i+1:03d}"
            bb, ss = self._inspected_bolt_with_two_segments(no)
            prepared.append((bb, ss))

        threads = []
        for bb, ss in prepared:
            for line, seg in (("lineA", ss[0]["id"]), ("lineB", ss[1]["id"])):
                threads.append(threading.Thread(
                    target=receive, args=(bb["id"], seg, line)))
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        oks = [r for r in results if r[0] == "ok"]
        self.assertEqual(len(oks), 3, f"每个布卷恰好一条成功，实际成功 {len(oks)}: {results}")
        for bb, ss in prepared:
            active = [t for t in self.svc.list_reinspections()
                      if t["bolt_id"] == bb["id"] and t["status"] == "queued"]
            self.assertEqual(len(active), 1)
            self.assertIn(active[0]["assigned_to"], ("lineA", "lineB"))

    def test_receive_after_completion_allowed(self):
        b, segs = self._inspected_bolt_with_two_segments("C-010")
        t = self.svc.create_reinspection(b["id"], segs[0]["id"], "line1",
                                         defect_id=self.svc.list_defects(
                                             b["current_version_id"])[0]["id"])
        t = self.svc.claim_reinspection(t["id"], "li")
        self.svc.complete_reinspection(t["id"], "li", "cleared", "ok")
        # 旧任务已完成，另一疵点可以再接收
        d2 = self.svc.list_defects(b["current_version_id"])[1]
        t2 = self.svc.create_reinspection(b["id"], segs[1]["id"], "line2", defect_id=d2["id"])
        self.assertEqual(t2["status"], "queued")

    def test_reinspection_must_reference_current_version_segment(self):
        # 退修生成新版本后，旧版本检验段不能再被复检引用
        b2, _, _, _ = self._warp_then_rework_path("C-012")
        old_seg = self.svc.list_segments(
            self.svc.list_versions(b2["id"])[0]["id"])[0]
        with self.expect_error("bad_reference", 422):
            self.svc.create_reinspection(b2["id"], old_seg["id"], "lineX")

    def _warp_then_rework_path(self, no):
        self.make_bolt(no)
        b, v, segs, defs = self.inspect(
            no, "sun", ranges=((0, 120),),
            defects=((0, "warp_break", 40, True, None),))
        rw = self.svc.create_rework(b["id"], "zhang", "A")
        self.svc.finish_rework(rw["id"], "zhang", "zhao")
        return self.svc.get_bolt(no), None, segs, defs
