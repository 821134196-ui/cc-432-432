"""疵点照片权限与文件访问控制。"""
from tests.base import PNG_1X1, QCBase


class TestPhotoPermissions(QCBase):
    def _one_defect(self, no="F-001", with_photo=True):
        self.make_bolt(no)
        b, v, segs, defs = self.inspect(
            no, "wang", defects=((0, "stain", 20, with_photo, None),))
        return b, v, defs[0]

    def test_all_roles_can_view(self):
        _, _, d = self._one_defect()
        for user in ("wang", "li", "zhang"):
            p = self.svc.photo_path_for_view(d["id"], user)
            self.assertTrue(p.exists())
            self.assertEqual(p.read_bytes(), PNG_1X1)

    def test_unknown_user_cannot_view(self):
        _, _, d = self._one_defect()
        with self.expect_error("unknown_user", 403):
            self.svc.photo_path_for_view(d["id"], "nobody")

    def test_only_inspector_or_manager_can_attach(self):
        _, _, d = self._one_defect(with_photo=False)
        with self.expect_error("forbidden", 403):
            self.svc.attach_photo(d["id"], "li", PNG_1X1)   # 复检工不能补传
        self.svc.attach_photo(d["id"], "wang", PNG_1X1)     # 初检工可以
        self.assertIsNotNone(self.svc.get_defect(d["id"])["photo_path"])

    def test_manager_can_attach_too(self):
        _, _, d = self._one_defect(with_photo=False)
        self.svc.attach_photo(d["id"], "zhang", PNG_1X1)
        self.assertIsNotNone(self.svc.get_defect(d["id"])["photo_path"])

    def test_no_photo_returns_404(self):
        # 长度不足为布卷级判定，不持有照片；直接查一个自动生成的缺失记录
        self.make_bolt("F-002", length=100, nominal=120)
        b, v, segs, defs = self.inspect("F-002", "zhao", ranges=((0, 100),))
        short = next(d for d in self.svc.list_defects(v["id"])
                     if d["type_code"] == "length_short")
        with self.expect_error("no_photo", 404):
            self.svc.photo_path_for_view(short["id"], "wang")

    def test_photo_blocked_after_release(self):
        self.make_bolt("F-003")
        b, v, segs, defs = self.inspect(
            "F-003", "wang", defects=((0, "stain", 20, True, "C"),))
        d = defs[0]
        self.svc.release(b["id"], "wang")
        with self.expect_error("bolt_locked", 409):
            self.svc.attach_photo(d["id"], "wang", PNG_1X1)  # 已放行不可改
