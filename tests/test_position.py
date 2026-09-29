"""疵点位置区间、检验段区间与重叠校验。"""
from textile_qc.service import QCError
from tests.base import PNG_1X1, QCBase


class TestPositionRanges(QCBase):
    def setUp(self):
        super().setUp()
        self.b = self.make_bolt("P-001", length=100)
        self.v = self.svc.open_version(self.b["id"], "wang")

    def test_defect_position_must_fall_in_segment(self):
        seg = self.svc.add_segment(self.v["id"], 10, 40)
        self.svc.add_defect(seg["id"], "stain", 10, "wang", photo_data=PNG_1X1)   # 左边界含
        self.svc.add_defect(seg["id"], "stain", 39.9, "wang", photo_data=PNG_1X1)
        with self.expect_error("out_of_range", 422):
            self.svc.add_defect(seg["id"], "stain", 40, "wang", photo_data=PNG_1X1)  # 右边界不含
        with self.expect_error("out_of_range", 422):
            self.svc.add_defect(seg["id"], "stain", 5, "wang", photo_data=PNG_1X1)

    def test_segment_end_must_not_exceed_bolt_length(self):
        with self.expect_error("out_of_range", 422):
            self.svc.add_segment(self.v["id"], 90, 101)   # 布卷仅 100m
        self.svc.add_segment(self.v["id"], 0, 100)        # 恰好到卷长，允许

    def test_segment_overlap_rejected(self):
        self.svc.add_segment(self.v["id"], 0, 50)
        with self.expect_error("overlap", 409):
            self.svc.add_segment(self.v["id"], 40, 80)    # 与 [0,50) 相交
        self.svc.add_segment(self.v["id"], 50, 80)        # 端点相接允许
        self.svc.add_segment(self.v["id"], 80, 100)

    def test_illegal_segment_bounds(self):
        with self.expect_error():
            self.svc.add_segment(self.v["id"], -1, 50)
        with self.expect_error():
            self.svc.add_segment(self.v["id"], 60, 60)
        with self.expect_error():
            self.svc.add_segment(self.v["id"], 80, 70)

    def test_negative_defect_position_rejected(self):
        seg = self.svc.add_segment(self.v["id"], 0, 50)
        with self.assertRaises(QCError):
            self.svc.add_defect(seg["id"], "stain", -2, "wang", photo_data=PNG_1X1)

    def test_sealed_version_rejects_segments_and_defects(self):
        seg = self.svc.add_segment(self.v["id"], 0, 50)
        self.svc.add_defect(seg["id"], "stain", 20, "wang", photo_data=PNG_1X1)
        self.svc.add_segment(self.v["id"], 50, 100)
        self.svc.complete_version(self.v["id"])
        with self.expect_error("bad_state", 409):
            self.svc.add_segment(self.v["id"], 0, 30)
        with self.expect_error("bad_state", 409):
            self.svc.add_defect(seg["id"], "stain", 25, "wang", photo_data=PNG_1X1)

    def test_only_inspector_records_defects(self):
        seg = self.svc.add_segment(self.v["id"], 0, 50)
        with self.expect_error("forbidden", 403):
            self.svc.add_defect(seg["id"], "stain", 20, "li", photo_data=PNG_1X1)
