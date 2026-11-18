"""采集端命令行。

典型流程：
  python -m collector.cli add /path/work C02 2025 单体 --label 同名实业
  python -m collector.cli run /path/work --base-url http://127.0.0.1:8765
  # 另一个终端：python -m collector.cli pause /path/work
  python -m collector.cli status /path/work
  python -m collector.cli handover /path/work
"""
from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path

from . import handover
from .engine import Engine
from .model import STATE_TEXT, Store


def _pause_file(workdir: Path) -> Path:
    return workdir / "PAUSE"


def cmd_add(args) -> int:
    store = Store.load(args.workdir)
    req = store.add_requirement(args.code, args.year, args.scope, args.label or "")
    print(f"已登记需求 {req.key}，状态 {STATE_TEXT.get(req.state, req.state)}")
    return 0


def cmd_run(args) -> int:
    engine = Engine(args.base_url, args.workdir, headless=not args.headed,
                    timeout_ms=args.timeout_ms)
    if not engine.store.requirements:
        print("工作目录下没有任何需求，请先用 add 登记", file=sys.stderr)
        return 2
    pause_file = _pause_file(Path(args.workdir))
    if pause_file.exists():
        pause_file.unlink()

    # PAUSE 文件轮询：操作员可在任意时刻安全请求暂停，当前需求处理完即停
    import threading

    def _watch_pause() -> None:
        while not engine.pause_requested:
            if pause_file.exists():
                engine.request_pause()
                print("\n收到暂停请求，将在当前需求处理完成后停下…", flush=True)
                return
            time.sleep(0.25)

    watcher = threading.Thread(target=_watch_pause, daemon=True)
    watcher.start()
    try:
        result = engine.run(
            interval_seconds=args.interval,
            max_rounds=args.max_rounds,
        )
    except KeyboardInterrupt:
        engine.request_pause()
        result = engine.run(interval_seconds=args.interval, max_rounds=1)

    handover_dir = handover.write_handover(engine.store)
    summary = result["summary"]
    print(
        f"轮次 {result['rounds']}，暂停={result['paused']}，"
        f"需求 {summary['需求数']}，收齐 {summary['已收齐数']}，"
        f"待操作员处理 {summary['待操作员处理数']}"
    )
    print(f"交接单目录：{handover_dir}")
    return 0


def cmd_pause(args) -> int:
    _pause_file(Path(args.workdir)).touch()
    print("已写入暂停请求，运行中的采集端会在当前需求处理完成后安全停下")
    return 0


def cmd_status(args) -> int:
    store = Store.load(args.workdir)
    if not store.requirements:
        print("（工作目录为空）")
        return 0
    for req in store.requirements.values():
        print(
            f"[{req.key}] 状态={req.state}（{STATE_TEXT.get(req.state, req.state)}）"
            f" 申请号={req.application_id or '—'} 已存册={sorted(req.files) or '无'}"
            f" 缺册={list(req.missing) or '无'}"
        )
        for note in req.notes[-3:]:
            print(f"    · {note}")
    return 0


def cmd_handover(args) -> int:
    store = Store.load(args.workdir)
    path = handover.write_handover(store)
    print(f"交接单已写出：{path}")
    print((path / "summary.md").read_text(encoding="utf-8"))
    return 0


def cmd_export_state(args) -> int:
    store = Store.load(args.workdir)
    print(json.dumps(
        {"requirements": [
            {
                "key": r.key, "state": r.state, "application_id": r.application_id,
                "apply_attempted": r.apply_attempted,
                "files": sorted(r.files), "missing": r.missing,
            } for r in store.requirements.values()
        ]},
        ensure_ascii=False, indent=2,
    ))
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="企业 ESG 门户异步导出采集端")
    sub = parser.add_subparsers(dest="command", required=True)

    p_add = sub.add_parser("add", help="登记一项资料需求")
    p_add.add_argument("workdir")
    p_add.add_argument("code", help="企业代码，如 C02")
    p_add.add_argument("year", help="报告期，如 2025")
    p_add.add_argument("scope", help="披露范围，如 单体 / 集团")
    p_add.add_argument("--label", default="", help="企业名称（仅备注，不用于判断）")
    p_add.set_defaults(func=cmd_add)

    p_run = sub.add_parser("run", help="用真实 Chromium 按状态机运行采集")
    p_run.add_argument("workdir")
    p_run.add_argument("--base-url", default="http://127.0.0.1:8765")
    p_run.add_argument("--interval", type=float, default=1.0, help="两轮对账间隔秒数")
    p_run.add_argument("--max-rounds", type=int, default=None, help="最多对账轮数（演练用）")
    p_run.add_argument("--headed", action="store_true", help="显示浏览器窗口")
    p_run.add_argument("--timeout-ms", type=int, default=10_000)
    p_run.set_defaults(func=cmd_run)

    p_pause = sub.add_parser("pause", help="请求运行中的采集端安全暂停")
    p_pause.add_argument("workdir")
    p_pause.set_defaults(func=cmd_pause)

    p_status = sub.add_parser("status", help="查看本地采集状态")
    p_status.add_argument("workdir")
    p_status.set_defaults(func=cmd_status)

    p_handover = sub.add_parser("handover", help="重新生成交接单")
    p_handover.add_argument("workdir")
    p_handover.set_defaults(func=cmd_handover)

    p_state = sub.add_parser("export-state", help="导出状态机快照 JSON")
    p_state.add_argument("workdir")
    p_state.set_defaults(func=cmd_export_state)
    return parser


def main(argv=None) -> int:
    args = build_parser().parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
