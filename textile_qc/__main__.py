"""单条本地启动命令：

    python3 -m textile_qc [--port 8000] [--reset]

自动创建 SQLite 数据库、疵点照片目录、初检/复检/退修/放行演示样例，并启动网页。
"""
from __future__ import annotations

import argparse
import shutil
import sys
from pathlib import Path

from .server import run


def main(argv: list[str] | None = None) -> int:
    try:
        sys.stdout.reconfigure(line_buffering=True)
    except Exception:
        pass
    ap = argparse.ArgumentParser(description="布卷疵点复检与退修系统")
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--port", type=int, default=8000)
    ap.add_argument("--db", default="data/qc.db")
    ap.add_argument("--photos", default="data/photos")
    ap.add_argument("--reset", action="store_true", help="删除旧库后重新播种演示数据")
    args = ap.parse_args(argv)

    if args.reset:
        for p in (args.db, args.db + "-wal", args.db + "-shm"):
            Path(p).unlink(missing_ok=True)
        shutil.rmtree(args.photos, ignore_errors=True)
        print("已清空旧数据。")

    Path(args.db).parent.mkdir(parents=True, exist_ok=True)
    state, httpd = run(args.host, args.port, args.db, args.photos, seed=True)
    bolts = state.svc.list_bolts()
    print("=" * 64)
    print(" 纺织厂布卷疵点复检与退修系统 已启动")
    print(f"  台账地址 : http://{args.host}:{args.port}/")
    print(f"  数据库   : {args.db}")
    print(f"  照片目录 : {args.photos}")
    print(f"  演示布卷 : {len(bolts)} 卷（含初检、复检、退修、放行样例）")
    print("  后台任务 : 每 30 秒扫描复检超时并生成提醒")
    print("=" * 64)
    print(" 按 Ctrl+C 停止")
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        print("\n正在停止…")
    finally:
        state.shutdown()
        httpd.server_close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
