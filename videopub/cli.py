"""命令行入口。

子命令一览::

    python main.py scan                     # 只扫描，列出待发布内容（不发布）
    python main.py publish [--limit 3]      # 发布一批（默认演练后端）
    python main.py publish --limit 3 --live # 使用真实后端发布
    python main.py publish --item 01-风是从哪来的   # 发布指定条目
    python main.py login                    # 视频号助手扫码登录（channels_web 后端）
    python main.py schedule                 # 常驻定时发布（每日早上自动发 3 条）
    python main.py schedule --once          # 只判断此刻是否该发（交给系统计划任务）
    python main.py status                   # 状态总览
    python main.py history [--limit 50]     # 发布历史查询
    python main.py report [--open]          # 生成 HTML 发布报表
    python main.py doctor                   # 环境与后端自检
    python main.py preflight                # 发布前预检（平台是否肯下发上传参数）
    python main.py backends                 # 列出可用后端
"""

from __future__ import annotations

import argparse
import json
import sys
import webbrowser
from pathlib import Path
from typing import Any

from . import __version__
from .backends import list_backends
from . import timing
from .config import Config
from .errors import ConfigError, PublisherError
from .logger import get_logger, log_event, setup_logging, shutdown_logging
from .publisher import VideoPublisher
from .report import generate_report, render_status
from .scanner import pending_items, scan_source
from .store import StateStore

log = get_logger("cli")


# --------------------------------------------------------------------------- #
# 通用
# --------------------------------------------------------------------------- #
def _load_cfg(args) -> Config:
    cfg = Config.load(getattr(args, "config", None))
    if getattr(args, "source_dir", None):
        cfg.source_dir = args.source_dir
    if getattr(args, "backend", None):
        cfg.publish.backend = args.backend
    if getattr(args, "live", False):
        cfg.dry_run = False
    if getattr(args, "count", None):
        cfg.schedule.daily_count = args.count
    _apply_mode_overrides(cfg, args)
    cfg.ensure_dirs()
    setup_logging(cfg.log_path, level=10 if getattr(args, "verbose", False) else 20)
    return cfg


def _apply_mode_overrides(cfg: Config, args) -> None:
    """把 ``--mode`` / ``--scheduled-at`` 覆盖到配置上。

    语义细节：只给 ``--scheduled-at`` 不给 ``--mode`` 时，自动切到 scheduled —— 
    用户表达「定时到某时刻」的意图已经足够明确，不该再要求他补一个参数。
    """
    mode = getattr(args, "mode", None)
    when = (getattr(args, "scheduled_at", None) or "").strip()
    if when:
        try:
            timing.parse_datetime(when)
        except ConfigError as exc:
            print(f"--scheduled-at 格式错误：{exc.message}", file=sys.stderr)
            raise SystemExit(2) from exc
        cfg.publish.scheduled_at = when
        mode = mode or "scheduled"
    if mode:
        cfg.publish.publish_mode = mode

    p = cfg.publish
    if p.publish_mode == "scheduled" and not (
        p.scheduled_at or p.scheduled_daily_time or p.scheduled_delay_minutes > 0
    ):
        print("publish_mode=scheduled 需要指定定时时间："
              "用 --scheduled-at \"YYYY-MM-DD HH:MM\"，"
              "或在 config.json 里设 publish.scheduled_daily_time（如 \"08:00\"）",
              file=sys.stderr)
        raise SystemExit(2)


def _print(obj: Any) -> None:
    print(json.dumps(obj, ensure_ascii=False, indent=2, default=str))


# --------------------------------------------------------------------------- #
# 子命令实现
# --------------------------------------------------------------------------- #
def cmd_scan(args: argparse.Namespace) -> int:
    cfg = _load_cfg(args)
    items = scan_source(cfg)
    print(f"\n源目录: {cfg.source_dir}")
    print(f"共扫描到 {len(items)} 个条目：\n")
    for i, it in enumerate(items, 1):
        warn = it.meta.get("warnings") or []
        print(f"{i:>2}. {it.title}")
        print(f"    ID      : {it.item_id}")
        print(f"    视频    : {Path(it.video_path).name if it.video_path else '（缺失）'}"
              f"  {it.video_size / 1024 / 1024:.1f}MB")
        print(f"    封面    : {Path(it.cover_path).name if it.cover_path else '（缺失）'}")
        print(f"    话题    : {' '.join(it.hashtags()) or '（无）'}")
        print(f"    描述    : {(it.description[:60] + '…') if len(it.description) > 60 else (it.description or '（空）')}")
        if warn:
            print(f"    ⚠ 告警  : {' | '.join(warn)}")
        print()
    if args.json:
        _print([it.to_dict() for it in items])
    return 0


def _report_mode(cfg: Config, options: dict[str, Any]) -> None:
    """把本次将使用的发布模式与定时目标打印出来，避免「以为定了其实没定」。"""
    mode = cfg.publish.publish_mode
    when = cfg.publish.resolve_scheduled_at(override=str(options.get("scheduled_at") or ""))
    label = {"draft": "存草稿", "immediate": "立即发布", "scheduled": "平台定时发表"}.get(mode, mode)
    line = f"发布模式：{label}（backend={cfg.publish.backend}，dry_run={cfg.dry_run}）"
    if mode == "scheduled":
        line += f"  目标时间：{when.strftime('%Y-%m-%d %H:%M') if when else '（未解析出！）'}"
    print(line)


def cmd_publish(args: argparse.Namespace) -> int:
    cfg = _load_cfg(args)
    store = StateStore(cfg.state_path)
    options: dict[str, Any] = {}
    _report_mode(cfg, options)

    with VideoPublisher(cfg, store=store, dry_run=cfg.dry_run) as pub:
        if args.item:
            all_items = scan_source(cfg)
            picked = [it for it in all_items if it.item_id == args.item or it.item_id.startswith(args.item)]
            if not picked:
                print(f"未找到条目：{args.item}", file=sys.stderr)
                return 2
            report_items = picked
            # 注意：显式指定 --item 时也必须把 --force 传下去，
            # 否则 run() 会回退到配置里的 skip_published=True，把条目直接跳过
            # （曾经 --item 01 --force 什么都不发，还返回「跳过」）。
            report = pub.run(items=report_items, limit=args.limit, trigger="cli",
                             skip_if_published=not args.force, options=options)
        else:
            report = pub.run(limit=args.limit, trigger="cli", skip_if_published=not args.force,
                             options=options)

    print()
    print(f"批次 {report.batch_id}（后端 {report.backend_name}，触发 {report.trigger}）")
    print(f"总计 {report.total} | 成功 {len(report.succeeded)} | 失败 {len(report.failed)} | 跳过 {len(report.skipped)}"
          f" | 耗时 {report.duration_ms / 1000:.1f}s")
    for r in report.results:
        mark = "✓" if r.status.ok else ("•" if r.status.value == "skipped" else "✗")
        line = f"  {mark} {r.title}  [{r.status.label}]"
        if r.remote_id:
            line += f"  → {r.remote_id}"
        if r.error_message:
            line += f"  ({r.error_code}: {r.error_message})"
        print(line)

    if args.json:
        _print(report.to_dict())
    if args.report:
        p = generate_report(cfg, store)
        print(f"\n报表已生成：{p}")
    return 0 if not report.failed else 1


def cmd_schedule(args: argparse.Namespace) -> int:
    from .scheduler import Scheduler

    cfg = _load_cfg(args)
    if args.count:
        cfg.schedule.daily_count = args.count
    if args.time:
        try:
            hh, mm = (int(x) for x in args.time.split(":"))
            if not (0 <= hh <= 23 and 0 <= mm <= 59):
                raise ValueError
            cfg.schedule.time = f"{hh:02d}:{mm:02d}"
        except ValueError:
            print(f"时间格式应为 HH:MM：{args.time}", file=sys.stderr)
            return 2

    sched = Scheduler(cfg)
    _report_mode(cfg, {"scheduled_at": getattr(args, "scheduled_at", None) or ""})
    if args.once:
        if args.force or sched.is_due():
            report = sched.run_once(trigger="schedule", limit=args.limit, force=args.force)
            print(f"本次执行：成功 {len(report.succeeded)} / 失败 {len(report.failed)}")
            return 0 if not report.failed else 1
        print("当前未到发布时刻（或今日已执行）。使用 --force 可强制执行。")
        _print(sched.status())
        return 0

    status = sched.status()
    print(f"定时发布：每日 {status['time']} 发布 {status['daily_count']} 条")
    if status.get("platform_target"):
        print(f"平台侧发布时刻：{status['platform_target']}（服务器执行，本机可关机）")
    print(f"下次执行：{status['next_run']}")
    if status.get("last_run"):
        print(f"上次执行：{status['last_run']}")
    print("按 Ctrl+C 停止。\n")
    sched.daemon(limit=args.limit)
    return 0


def cmd_status(args: argparse.Namespace) -> int:
    cfg = _load_cfg(args)
    store = StateStore(cfg.state_path)
    pending: list | None = None
    if not args.no_pending:
        try:
            pending = pending_items(cfg, store)
        except PublisherError as exc:
            print(f"（待发布队列获取失败：{exc.message}）")
    sched_status = None
    try:
        from .scheduler import Scheduler

        sched_status = Scheduler(cfg, store=store, publisher=VideoPublisher(cfg, store=store)).status()
    except Exception:
        pass
    print(render_status(cfg, store, pending=pending, sched=sched_status))
    if args.json:
        _print({"store": store.to_dict(), "schedule": sched_status,
                "pending": [it.to_dict() for it in (pending or [])]})
    return 0


def cmd_history(args: argparse.Namespace) -> int:
    cfg = _load_cfg(args)
    store = StateStore(cfg.state_path)
    records = store.history(limit=args.limit, status=args.status, backend=args.backend_filter,
                            item_id=args.item)
    if args.json:
        _print(records)
        return 0
    if not records:
        print("暂无发布历史。")
        return 0
    print(f"{'时间':<20}{'状态':<10}{'标题':<28}{'后端':<14}{'耗时':<9}错误")
    print("-" * 96)
    for r in records:
        ts = (r.get("finished_at") or r.get("started_at") or "")[:19].replace("T", " ")
        title = (r.get("title") or "")[:24]
        dur = f"{(r.get('duration_ms') or 0) / 1000:.1f}s"
        err = (r.get("error_message") or "")[:28]
        print(f"{ts:<20}{r.get('status', ''):<10}{title:<28}{r.get('backend', ''):<14}{dur:<9}{err}")
    print("-" * 96)
    print(f"共 {len(records)} 条。汇总：{_bar(store.counts())}")
    return 0


def _bar(counts: dict[str, Any]) -> str:
    return "  ".join(f"{k}={v}" for k, v in counts.items() if k != "total" and v)


def cmd_report(args: argparse.Namespace) -> int:
    cfg = _load_cfg(args)
    store = StateStore(cfg.state_path)
    sched_status = None
    try:
        from .scheduler import Scheduler

        sched_status = Scheduler(cfg, store=store, publisher=VideoPublisher(cfg, store=store)).status()
    except Exception:
        pass
    path = generate_report(cfg, store, limit=args.limit, out_path=args.out, schedule_status=sched_status)
    print(f"报表已生成：{path}")
    if args.open:
        webbrowser.open(path.as_uri())
    return 0


def cmd_preflight(args: argparse.Namespace) -> int:
    """发布前预检：1 秒内回答「平台现在肯不肯收视频」。

    背景：视频号助手在上传前先调 ``helper_upload_params`` 取上传参数。
    该接口返回非 0 时，页面会永远卡在「0% / 取消上传」，且点「发表」会被
    「请上传视频」挡回 —— 属于账号/服务端侧限制，**任何浏览器调参都救不了**。
    所以在花钱跑一遍浏览器自动化之前，先在这里问清楚。
    """
    from .preflight import check_upload_ready, format_report

    cfg = _load_cfg(args)
    rep = check_upload_ready(cfg, timeout=getattr(args, "timeout", 20) or 20,
                             direct=getattr(args, "direct", False))
    if getattr(args, "json", False):
        _print(rep)
    else:
        print(format_report(rep))
    return 0 if rep.get("ready") else 1


def cmd_doctor(args: argparse.Namespace) -> int:
    cfg = _load_cfg(args)
    out: dict[str, Any] = {"version": __version__, "python": sys.version.split()[0], "config": cfg.to_dict()}
    problems: list[str] = []

    # 1) 配置与目录
    try:
        cfg.ensure_dirs()
        out["dirs_ok"] = True
    except OSError as exc:
        out["dirs_ok"] = False
        problems.append(f"目录创建失败：{exc}")

    # 2) 源目录
    try:
        items = scan_source(cfg)
        out["source"] = {"ok": True, "count": len(items)}
    except PublisherError as exc:
        out["source"] = {"ok": False, "error": exc.to_dict()}
        problems.append(f"源目录不可用：{exc.message}")

    # 3) 后端自检
    pub = VideoPublisher(cfg, store=StateStore(cfg.state_path), dry_run=cfg.dry_run)
    try:
        out["backend"] = pub.healthcheck()
        h = out["backend"].get("health", {})
        if isinstance(h, dict) and not h.get("ok", True):
            problems.append(f"后端自检未通过：{h.get('error')}")
    except Exception as exc:
        out["backend"] = {"error": repr(exc)}
        problems.append(f"后端初始化失败：{exc!r}")
    finally:
        pub.close()

    # 4) 可写性
    try:
        probe = cfg.state_path.parent / ".write_probe"
        probe.write_text("ok", encoding="utf-8")
        probe.unlink()
        out["writable"] = True
    except OSError as exc:
        out["writable"] = False
        problems.append(f"状态目录不可写：{exc}")

    out["problems"] = problems
    out["ok"] = not problems
    _print(out)
    if problems:
        print("\n发现问题：")
        for p in problems:
            print(f"  ✗ {p}")
        return 1
    print("\n自检通过 ✓")
    return 0


def cmd_login(args: argparse.Namespace) -> int:
    from .backends import create_backend

    cfg = _load_cfg(args)
    cfg.dry_run = False
    creds = cfg.credentials.setdefault("channels_web", {})
    if getattr(args, "channel", None):
        creds["channel"] = args.channel
    if getattr(args, "qr", None):
        creds["qr_path"] = args.qr

    backend = create_backend("channels_web", cfg)
    try:
        info = backend.login(timeout_seconds=args.timeout, force=getattr(args, "force", False))
    except PublisherError as exc:
        print(f"登录失败：{exc.message}", file=sys.stderr)
        print(f"提示：{exc.detail.get('hint', '')}", file=sys.stderr)
        return 1
    finally:
        close = getattr(backend, "close", None)
        if callable(close):
            close()

    if info.get("already_logged_in"):
        acct = info.get("account") or {}
        print(f"当前登录态仍然有效，无需重新扫码。"
              f"（账号：{acct.get('nickname', '')} {acct.get('channels_id', '')}）")
        print("如需换号，请加 --force 强制重新登录。")
        return 0

    if not info.get("ok", True):
        print(f"登录未完成：{info.get('error', 'unknown')}", file=sys.stderr)
        if info.get("qr"):
            print(f"二维码图片：{info['qr']}", file=sys.stderr)
        print(f"提示：{info.get('hint', '')}", file=sys.stderr)
        return 1

    print("登录成功，登录态已保存：")
    _print(info)
    return 0


def cmd_backends(args: argparse.Namespace) -> int:
    rows = list_backends()
    print(f"{'名称':<16}说明")
    print("-" * 80)
    for r in rows:
        print(f"{r['name']:<16}{r['desc']}")
    return 0


# --------------------------------------------------------------------------- #
# 参数解析
# --------------------------------------------------------------------------- #
def add_common_options(sp: argparse.ArgumentParser) -> argparse.ArgumentParser:
    """给子命令挂上全局选项的副本。

    这样 ``main.py scan --source-dir X`` 与 ``main.py --source-dir X scan``
    两种写法都成立（早期版本只支持后者，用户极易踩坑）。

    实现说明：这里**不用** ``parents=[parent]`` —— 同一个 parent 对象被多个
    子命令复用时，共享的 action 对象会让 argparse 抛
    ``conflicting option string``。改为逐个子命令独立添加。
    子命令副本统一用 ``default=SUPPRESS``，未显式提供时不会覆盖顶层解析结果。
    """
    sp.add_argument("--config", default=argparse.SUPPRESS, help="配置文件路径（默认 ./config.json）")
    sp.add_argument("--source-dir", dest="source_dir", default=argparse.SUPPRESS, help="覆盖源目录")
    sp.add_argument("--backend", choices=["dryrun", "openapi", "channels_web"],
                    default=argparse.SUPPRESS, help="覆盖后端")
    sp.add_argument("--live", action="store_true", default=argparse.SUPPRESS,
                    help="关闭演练模式，使用真实后端")
    sp.add_argument("-v", "--verbose", action="store_true", default=argparse.SUPPRESS,
                    help="输出调试日志")
    return sp


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="videopub",
        description="微信视频号内容发布模块（支持定时发布、日志与结果查询）",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    p.add_argument("--version", action="version", version=f"videopub {__version__}")
    p.add_argument("--config", help="配置文件路径（默认 ./config.json）")
    p.add_argument("--source-dir", dest="source_dir", help="覆盖源目录")
    p.add_argument("--backend", choices=["dryrun", "openapi", "channels_web"], help="覆盖后端")
    p.add_argument("--live", action="store_true", help="关闭演练模式，使用真实后端")
    p.add_argument("-v", "--verbose", action="store_true", help="输出调试日志")

    sub = p.add_subparsers(dest="cmd", required=True)

    sp = add_common_options(sub.add_parser("scan", help="扫描并列出待发布内容"))
    sp.add_argument("--json", action="store_true")

    pp = add_common_options(sub.add_parser("publish", help="发布一批内容"))
    pp.add_argument("--limit", type=int, default=None, help="本次发布条数（默认全部待发布）")
    pp.add_argument("--item", help="只发布指定条目（ID 前缀匹配）")
    pp.add_argument("--force", action="store_true", help="忽略『已发布』标记，强制重发")
    pp.add_argument("--report", action="store_true", help="发布后生成 HTML 报表")
    pp.add_argument("--mode", choices=["draft", "immediate", "scheduled"], default=None,
                    help="覆盖 publish_mode：draft 存草稿 / immediate 立即发布 / scheduled 平台定时")
    pp.add_argument("--scheduled-at", dest="scheduled_at", default=None,
                    help='平台定时时间，形如 "2026-10-14 08:00"（需配合 --mode scheduled）')
    pp.add_argument("--json", action="store_true")

    sch = add_common_options(sub.add_parser("schedule", help="定时发布（常驻或 --once）"))
    sch.add_argument("--once", action="store_true", help="只判断此刻是否该发一次")
    sch.add_argument("--force", action="store_true", help="配合 --once，强制执行")
    sch.add_argument("--limit", type=int, default=None, help="覆盖本次条数")
    sch.add_argument("--count", type=int, default=None, help="覆盖每日条数")
    sch.add_argument("--time", default=None, help="覆盖每日时刻 HH:MM")
    sch.add_argument("--mode", choices=["draft", "immediate", "scheduled"], default=None,
                     help="覆盖 publish_mode")
    sch.add_argument("--scheduled-at", dest="scheduled_at", default=None,
                     help='平台定时时间，形如 "2026-10-14 08:00"')

    st = add_common_options(sub.add_parser("status", help="状态总览"))
    st.add_argument("--json", action="store_true")
    st.add_argument("--no-pending", action="store_true", help="不计算待发布队列（加快）")

    hi = add_common_options(sub.add_parser("history", help="发布历史查询"))
    hi.add_argument("--limit", type=int, default=50)
    hi.add_argument("--status", choices=["success", "failed", "skipped", "dry_run", "pending"])
    hi.add_argument("--filter-backend", dest="backend_filter", help="按后端名过滤历史")
    hi.add_argument("--item")
    hi.add_argument("--json", action="store_true")

    rp = add_common_options(sub.add_parser("report", help="生成 HTML 发布报表"))
    rp.add_argument("--limit", type=int, default=200)
    rp.add_argument("--out", help="输出路径")
    rp.add_argument("--open", action="store_true", help="生成后自动打开")

    add_common_options(sub.add_parser("doctor", help="环境与后端自检"))

    pf = add_common_options(sub.add_parser(
        "preflight", help="发布前预检：平台现在是否肯下发上传参数（不开浏览器，约 1 秒）"))
    pf.add_argument("--timeout", type=float, default=20.0, help="单次请求超时秒数（默认 20）")
    pf.add_argument("--direct", action="store_true", help="绕过所有代理直连（怀疑代理干扰时用）")

    lg = add_common_options(sub.add_parser("login", help="视频号助手扫码登录（channels_web 后端）"))
    lg.add_argument("--timeout", type=int, default=600, help="等待扫码的秒数（默认 600）")
    lg.add_argument("--channel", choices=["chrome", "msedge", "chromium"], default=None,
                    help="浏览器内核：chrome/msedge 复用系统浏览器，chromium 用 playwright 自带内核")
    lg.add_argument("--qr", default=None, help="二维码截图输出路径")
    lg.add_argument("--force", action="store_true", help="忽略已有登录态，强制重新扫码（换号用）")

    add_common_options(sub.add_parser("backends", help="列出可用后端"))
    return p


DISPATCH = {
    "scan": cmd_scan,
    "publish": cmd_publish,
    "schedule": cmd_schedule,
    "status": cmd_status,
    "history": cmd_history,
    "report": cmd_report,
    "doctor": cmd_doctor,
    "preflight": cmd_preflight,
    "login": cmd_login,
    "backends": cmd_backends,
}


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    # 默认值回填（子命令未显式提供时）
    for attr, default in (("json", False), ("limit", None), ("verbose", False),
                          ("live", False), ("force", False), ("report", False)):
        if not hasattr(args, attr):
            setattr(args, attr, default)
    try:
        try:
            return DISPATCH[args.cmd](args)
        except KeyboardInterrupt:
            print("\n已中断。", file=sys.stderr)
            return 130
        except PublisherError as exc:
            log.error("执行失败：%s", exc)
            print(f"\n错误 [{exc.code}] {exc.message}", file=sys.stderr)
            if exc.detail:
                print(f"详情：{json.dumps(exc.detail, ensure_ascii=False, default=str)}", file=sys.stderr)
            return 1
        except Exception as exc:  # 兜底，避免抛栈给用户
            log.exception("未预期错误")
            print(f"\n未预期错误：{exc!r}", file=sys.stderr)
            return 1
    finally:
        # 释放日志文件句柄，避免进程结束后日志目录仍被占用
        shutdown_logging()
