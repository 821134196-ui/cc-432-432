"""HTTP 服务：JSON API + 静态网页，全部基于 Python 标准库。

进程内后台线程每 30 秒扫描一次复检超时并写提醒。
"""
from __future__ import annotations

import json
import threading
import urllib.parse
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from .db import connect
from .demo import seed_demo
from .service import QCError, QCService
from .users import USERS

WEB_DIR = Path(__file__).parent / "web"

STATUS_TEXT = {
    "pending": "待检", "inspecting": "检验中", "inspected": "待复检",
    "reinspecting": "复检中", "reworking": "退修中",
    "reinspect_after_rework": "退修后待复检", "released": "已放行",
    "rejected": "已拒收",
}


class AppState:
    def __init__(self, db_path: str, photo_root: str, seed: bool = True,
                 scan_interval: float = 30.0):
        self.conn = connect(db_path)
        self.photo_root = Path(photo_root)
        if seed:
            seed_demo(self.conn, self.photo_root)
        self.svc = QCService(self.conn, self.photo_root)
        self.scan_interval = scan_interval
        self._stop = threading.Event()
        self._scanner = threading.Thread(target=self._scan_loop, daemon=True,
                                         name="qc-timeout-scanner")
        self._scanner.start()

    def _scan_loop(self) -> None:
        while not self._stop.wait(self.scan_interval):
            try:
                n = self.svc.scan_overdue()
                if n:
                    print(f"[复检提醒] {n} 条任务超时")
            except Exception as e:  # 扫描线程不能因异常退出
                print(f"[复检提醒] 扫描失败: {e}")

    def shutdown(self) -> None:
        self._stop.set()


def make_handler(state: AppState):
    svc = state.svc

    class Handler(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"

        def log_message(self, fmt, *args):
            pass  # 静默默认访问日志

        # ------------------------------------------------ 工具方法
        def _json(self, payload, status: int = 200):
            body = json.dumps(payload, ensure_ascii=False, default=str).encode()
            self.send_response(status)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def _read_json(self) -> dict:
            length = int(self.headers.get("Content-Length", 0))
            if not length:
                return {}
            raw = self.rfile.read(length)
            try:
                return json.loads(raw.decode())
            except json.JSONDecodeError:
                raise QCError("请求体不是合法 JSON", 400, "bad_json")

        def _user(self, q: dict, body: dict | None = None) -> str:
            user = q.get("user") or (body or {}).get("user") or "wang"
            if user not in USERS:
                raise QCError(f"用户不存在: {user}", 403, "unknown_user")
            return user

        def _handle_error(self, e: QCError):
            self._json({"error": e.code, "message": e.message}, e.http_status)

        # ------------------------------------------------ 静态文件
        def _serve_static(self, rel: str):
            if not rel:
                rel = "index.html"
            target = (WEB_DIR / rel).resolve()
            if WEB_DIR.resolve() not in target.parents and target != WEB_DIR.resolve():
                self.send_error(403)
                return
            if not target.is_file():
                self.send_error(404)
                return
            ctype = {
                ".html": "text/html; charset=utf-8",
                ".css": "text/css; charset=utf-8",
                ".js": "application/javascript; charset=utf-8",
                ".png": "image/png", ".svg": "image/svg+xml",
            }.get(target.suffix, "application/octet-stream")
            data = target.read_bytes()
            self.send_response(200)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        # ------------------------------------------------ GET
        def do_GET(self):
            parsed = urllib.parse.urlparse(self.path)
            path = parsed.path
            q = {k: v[0] for k, v in urllib.parse.parse_qs(parsed.query).items()}
            try:
                if path == "/api/health":
                    self._json({"ok": True})
                elif path == "/api/meta":
                    self._json({"users": USERS, "status_text": STATUS_TEXT,
                                "defect_types": svc.list_defect_types()})
                elif path == "/api/bolts":
                    self._json(svc.list_bolts(q.get("status")))
                elif path.startswith("/api/bolts/"):
                    self._bolt_detail(path[len("/api/bolts/"):])
                elif path == "/api/reinspections":
                    self._json(svc.list_reinspections(q.get("status")))
                elif path == "/api/reworks":
                    self._json(svc.list_reworks(q.get("status")))
                elif path == "/api/releases":
                    self._json(svc.list_releases())
                elif path == "/api/reminders":
                    self._json(svc.list_reminders())
                elif path.startswith("/api/photos/"):
                    self._photo(path[len("/api/photos/"):], q)
                elif path.startswith("/api/"):
                    self.send_error(404)
                else:
                    self._serve_static(path.lstrip("/"))
            except QCError as e:
                self._handle_error(e)
            except Exception as e:  # noqa: BLE001
                self._json({"error": "internal", "message": str(e)}, 500)

        def _bolt_detail(self, tail: str):
            parts = tail.split("/")
            bolt = svc.get_bolt(parts[0])
            if len(parts) == 1:
                self._json(bolt)
            elif parts[1] == "map":
                self._json(svc.defect_map(bolt["id"]))
            elif parts[1] == "gates":
                self._json({"bolt": bolt, "gates": svc.evaluate_release(bolt["id"])})
            elif parts[1] == "versions":
                ver_id = parts[2]
                if ver_id == "current":
                    ver_id = bolt["current_version_id"]
                payload = {"version": svc.get_version(ver_id),
                           "segments": svc.list_segments(ver_id),
                           "defects": svc.list_defects(ver_id)}
                self._json(payload)
            else:
                self.send_error(404)

        def _photo(self, defect_id: str, q: dict):
            target = svc.photo_path_for_view(defect_id, self._user(q))
            data = target.read_bytes()
            self.send_response(200)
            self.send_header("Content-Type", "image/png")
            self.send_header("Content-Length", str(len(data)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(data)

        # ------------------------------------------------ POST
        def do_POST(self):
            parsed = urllib.parse.urlparse(self.path)
            path = parsed.path
            parts = path.strip("/").split("/")
            q = {k: v[0] for k, v in urllib.parse.parse_qs(parsed.query).items()}
            try:
                body = self._read_json()
                user = self._user(q, body)

                if path == "/api/bolts":
                    self._json(svc.register_bolt(
                        body["bolt_no"], body["loom_code"], body["color_code"],
                        float(body["length_m"]), float(body["nominal_length_m"]),
                        float(body["width_cm"])), 201)
                elif path == "/api/inspection/open":
                    b = svc.get_bolt(body["bolt_no"])
                    self._json(svc.open_version(b["id"], user), 201)
                elif path == "/api/segments":
                    self._json(svc.add_segment(
                        body["version_id"], float(body["start_m"]),
                        float(body["end_m"]), body.get("note", ""), actor=user), 201)
                elif path == "/api/defects":
                    self._json(self._create_defect(body, user), 201)
                elif path == "/api/inspection/complete":
                    self._json(svc.complete_version(body["version_id"]))
                elif path == "/api/reinspections":
                    b = svc.get_bolt(body["bolt_no"])
                    self._json(svc.create_reinspection(
                        b["id"], body["segment_id"], body.get("line", "line1"),
                        defect_id=body.get("defect_id"),
                        sla_hours=body.get("sla_hours")), 201)
                elif (len(parts) == 4 and parts[1] == "reinspections"
                      and parts[3] in ("claim", "complete", "requeue")):
                    tid, action = parts[2], parts[3]
                    if action == "claim":
                        self._json(svc.claim_reinspection(tid, user))
                    elif action == "complete":
                        self._json(svc.complete_reinspection(
                            tid, user, body["result"], body.get("comment", "")))
                    else:
                        self._json(svc.requeue_reinspection(tid, user))
                elif path == "/api/reworks":
                    b = svc.get_bolt(body["bolt_no"])
                    self._json(svc.create_rework(b["id"], user, body.get("reason", "")), 201)
                elif len(parts) == 4 and parts[1] == "reworks" and parts[3] == "finish":
                    self._json(svc.finish_rework(parts[2], user, body["inspector"]))
                elif path == "/api/release":
                    b = svc.get_bolt(body["bolt_no"])
                    self._json(svc.release(
                        b["id"], user, concession=bool(body.get("concession")),
                        decision=body.get("decision", "release"),
                        basis_note=body.get("basis_note", "")), 201)
                else:
                    self.send_error(404)
            except QCError as e:
                self._handle_error(e)
            except KeyError as e:
                self._json({"error": "missing_field", "message": f"缺少字段: {e}"}, 400)
            except Exception as e:  # noqa: BLE001
                self._json({"error": "internal", "message": str(e)}, 500)

        def _create_defect(self, body: dict, user: str) -> dict:
            photo = None
            if body.get("photo"):  # base64 编码的演示照片
                import base64
                photo = base64.b64decode(body["photo"])
            return svc.add_defect(
                body["segment_id"], body["type_code"], float(body["position_m"]),
                user, grade=body.get("grade"), photo_data=photo,
                require_photo=bool(body.get("require_photo", True)))

        # 照片补传（multipart 略，演示用 base64 JSON）
        def do_PUT(self):
            parsed = urllib.parse.urlparse(self.path)
            try:
                if parsed.path.startswith("/api/photos/"):
                    import base64
                    body = self._read_json()
                    user = self._user({}, body)
                    data = base64.b64decode(body["photo"])
                    self._json(svc.attach_photo(
                        parsed.path[len("/api/photos/"):], user, data))
                else:
                    self.send_error(404)
            except QCError as e:
                self._handle_error(e)

    return Handler


def run(host: str = "127.0.0.1", port: int = 8000, db_path: str = "data/qc.db",
        photo_root: str = "data/photos", seed: bool = True) -> tuple[AppState, ThreadingHTTPServer]:
    state = AppState(db_path, photo_root, seed=seed)
    httpd = ThreadingHTTPServer((host, port), make_handler(state))
    return state, httpd
