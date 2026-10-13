"""结果查询与报表渲染。

  * :func:`render_status`   —— 控制台状态面板（发布总览 + 待发布队列）
  * :func:`generate_report` —— 生成自包含的 HTML 发布报表，可离线打开/归档

报表不依赖任何外部静态资源（内联 CSS/JS），可直接双击打开。
"""

from __future__ import annotations

import html
import json
from collections import Counter
from datetime import datetime
from pathlib import Path
from typing import Any, Iterable

from .config import Config
from .models import PublishStatus
from .store import StateStore

STATUS_COLOR = {
    "success": "#12a150",
    "dry_run": "#2563eb",
    "failed": "#e5484d",
    "skipped": "#8b8d98",
    "pending": "#b7791f",
    "running": "#0ea5e9",
}


def _bar(counts: dict[str, int]) -> str:
    return "  ".join(f"{k}={v}" for k, v in counts.items() if v)


def render_status(cfg: Config, store: StateStore, *, pending: list | None = None,
                  sched: dict[str, Any] | None = None) -> str:
    counts = store.counts()
    lines = []
    lines.append("=" * 68)
    lines.append("  视频号发布模块 · 状态总览")
    lines.append("=" * 68)
    lines.append(f"  源目录      : {cfg.source_dir}")
    lines.append(f"  后端        : {cfg.publish.backend if not cfg.dry_run else 'dryrun'}"
                 f"{'（演练模式）' if cfg.dry_run else ''}")
    lines.append(f"  状态文件    : {store.state_file}")
    lines.append(f"  历史记录    : {store.history_file}")
    lines.append("-" * 68)
    lines.append(f"  已跟踪条目  : {counts.get('total', 0)}")
    lines.append(f"    成功      : {counts.get('success', 0)}")
    lines.append(f"    演练通过  : {counts.get('dry_run', 0)}")
    lines.append(f"    失败      : {counts.get('failed', 0)}")
    lines.append(f"    跳过      : {counts.get('skipped', 0)}")
    if sched:
        lines.append("-" * 68)
        lines.append(f"  定时发布    : {'开启' if sched.get('enabled') else '关闭'}"
                     f"（每日 {sched.get('time')}，{sched.get('daily_count')} 条）")
        lines.append(f"  下次执行    : {sched.get('next_run')}")
        last = sched.get("last_run")
        if last:
            lines.append(f"  上次执行    : {last.get('at')}  成功 {last.get('success')} / 失败 {last.get('failed')}")
    if pending is not None:
        lines.append("-" * 68)
        lines.append(f"  待发布队列  : {len(pending)} 条")
        for i, it in enumerate(pending[:10], 1):
            tag = "  ".join(it.hashtags()[:4])
            lines.append(f"    {i:>2}. {it.title}   [{it.video_size / 1024 / 1024:.1f}MB]  {tag}")
        if len(pending) > 10:
            lines.append(f"    ... 其余 {len(pending) - 10} 条")
    lines.append("=" * 68)
    return "\n".join(lines)


def _esc(v: Any) -> str:
    return html.escape(str(v if v is not None else ""))


def _rows(records: Iterable[dict[str, Any]]) -> str:
    out = []
    for r in records:
        st = r.get("status", "unknown")
        color = STATUS_COLOR.get(st, "#666")
        ts = (r.get("finished_at") or r.get("started_at") or "")[:19].replace("T", " ")
        dur = r.get("duration_ms") or 0
        code = r.get("error_code") or ""
        err = r.get("error_message") or ""
        if code:
            err = f"[{code}] {err}"
        remote = r.get("remote_id") or ""
        out.append(
            f"<tr data-status='{_esc(st)}'>"
            f"<td class='mono'>{_esc(ts)}</td>"
            f"<td><span class='badge' style='background:{color}'>{_esc(st)}</span></td>"
            f"<td>{_esc(r.get('title'))}</td>"
            f"<td class='mono sm'>{_esc(r.get('item_id'))}</td>"
            f"<td>{_esc(r.get('backend'))}</td>"
            f"<td class='mono sm'>{_esc(remote)}</td>"
            f"<td class='num'>{dur / 1000:.2f}s</td>"
            f"<td class='err'>{_esc(err)}</td>"
            f"</tr>"
        )
    return "\n".join(out)


def generate_report(cfg: Config, store: StateStore, *, limit: int = 200,
                    out_path: str | Path | None = None,
                    schedule_status: dict[str, Any] | None = None) -> Path:
    """生成 HTML 发布报表，返回文件路径。"""
    counts = store.counts()
    history = store.history(limit=limit)
    pending_records = [
        {"item_id": k, **v} for k, v in store.items.items()
        if v.get("status") not in (PublishStatus.SUCCESS.value,)
    ]
    per_day = store.history_summary(days=14)

    ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    target = Path(out_path) if out_path else cfg.report_path / f"发布报表-{datetime.now():%Y%m%d-%H%M%S}.html"
    target.parent.mkdir(parents=True, exist_ok=True)

    cards = [
        ("已跟踪条目", counts.get("total", 0), "#334155"),
        ("成功", counts.get("success", 0), STATUS_COLOR["success"]),
        ("演练通过", counts.get("dry_run", 0), STATUS_COLOR["dry_run"]),
        ("失败", counts.get("failed", 0), STATUS_COLOR["failed"]),
        ("跳过", counts.get("skipped", 0), STATUS_COLOR["skipped"]),
        ("历史尝试", len(history), "#7c3aed"),
    ]
    cards_html = "".join(
        f"<div class='card'><div class='card-v' style='color:{c}'>{v}</div>"
        f"<div class='card-k'>{_esc(k)}</div></div>" for k, v, c in cards
    )

    day_bars = ""
    if per_day:
        maxv = max(sum(v.values()) for v in per_day.values()) or 1
        for day, cnt in per_day.items():
            total = sum(cnt.values())
            w = int(total / maxv * 100)
            segs = ""
            for st, n in cnt.items():
                segs += (f"<span class='seg' style='width:{n / total * 100:.1f}%;"
                         f"background:{STATUS_COLOR.get(st, '#888')}' title='{st}: {n}'></span>")
            day_bars += (f"<div class='dayrow'><span class='daylabel'>{_esc(day)}</span>"
                         f"<span class='daybar' style='width:{w}%'>{segs}</span>"
                         f"<span class='daynum'>{total}</span></div>")
    else:
        day_bars = "<div class='empty'>暂无历史数据</div>"

    sched_html = ""
    if schedule_status:
        sched_html = (
            "<div class='panel'><h3>定时发布</h3><table class='kv'>"
            f"<tr><td>状态</td><td>{'开启' if schedule_status.get('enabled') else '关闭'}</td></tr>"
            f"<tr><td>每日时间</td><td>{_esc(schedule_status.get('time'))}</td></tr>"
            f"<tr><td>每日条数</td><td>{_esc(schedule_status.get('daily_count'))}</td></tr>"
            f"<tr><td>下次执行</td><td class='mono'>{_esc(schedule_status.get('next_run'))}</td></tr>"
            f"<tr><td>上次执行</td><td class='mono'>{_esc(json.dumps(schedule_status.get('last_run'), ensure_ascii=False))}</td></tr>"
            "</table></div>"
        )

    doc = f"""<!DOCTYPE html>
<html lang="zh-CN"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>视频号发布报表 · {ts}</title>
<style>
  :root {{ --bg:#f6f7f9; --fg:#1f2430; --muted:#6b7280; --line:#e5e7eb; --card:#fff; }}
  * {{ box-sizing: border-box; }}
  body {{ margin:0; padding:28px; background:var(--bg); color:var(--fg);
         font: 14px/1.6 "Microsoft YaHei","PingFang SC",system-ui,-apple-system,sans-serif; }}
  h1 {{ font-size:22px; margin:0 0 4px; }}
  .sub {{ color:var(--muted); font-size:13px; margin-bottom:20px; }}
  .cards {{ display:grid; grid-template-columns:repeat(auto-fit,minmax(130px,1fr)); gap:12px; margin-bottom:20px; }}
  .card {{ background:var(--card); border:1px solid var(--line); border-radius:12px; padding:14px 16px; }}
  .card-v {{ font-size:26px; font-weight:700; line-height:1.2; }}
  .card-k {{ color:var(--muted); font-size:12px; margin-top:2px; }}
  .panel {{ background:var(--card); border:1px solid var(--line); border-radius:12px; padding:16px 18px; margin-bottom:20px; }}
  .panel h3 {{ margin:0 0 12px; font-size:15px; }}
  table {{ width:100%; border-collapse:collapse; }}
  th,td {{ text-align:left; padding:8px 10px; border-bottom:1px solid var(--line); vertical-align:top; }}
  th {{ font-weight:600; color:var(--muted); font-size:12px; text-transform:uppercase; letter-spacing:.03em; }}
  td.num {{ text-align:right; font-variant-numeric:tabular-nums; white-space:nowrap; }}
  .mono {{ font-family:ui-monospace,Consolas,monospace; }}
  .sm {{ font-size:12px; color:var(--muted); }}
  .err {{ color:#b91c1c; font-size:12px; max-width:320px; }}
  .badge {{ display:inline-block; color:#fff; border-radius:999px; padding:1px 9px; font-size:12px; }}
  .dayrow {{ display:flex; align-items:center; gap:10px; margin:6px 0; }}
  .daylabel {{ width:96px; color:var(--muted); font-size:12px; font-family:ui-monospace,monospace; }}
  .daybar {{ display:flex; height:16px; border-radius:8px; overflow:hidden; background:#eef0f3; min-width:8px; }}
  .seg {{ display:block; height:100%; }}
  .daynum {{ font-size:12px; color:var(--muted); }}
  .empty {{ color:var(--muted); }}
  .kv td:first-child {{ width:120px; color:var(--muted); }}
  .toolbar {{ margin-bottom:14px; }}
  .toolbar input,.toolbar select {{ padding:6px 10px; border:1px solid var(--line); border-radius:8px; background:#fff; }}
  footer {{ color:var(--muted); font-size:12px; margin-top:24px; }}
</style></head><body>
<h1>视频号发布报表</h1>
<div class="sub">生成时间 {ts} · 源目录 <span class="mono">{_esc(cfg.source_dir)}</span>
 · 后端 <b>{_esc('dryrun(演练)' if cfg.dry_run else cfg.publish.backend)}</b></div>
<div class="cards">{cards_html}</div>
{sched_html}
<div class="panel"><h3>近 14 天发布活动</h3>{day_bars}</div>
<div class="panel">
  <h3>发布历史（最近 {len(history)} 条）</h3>
  <div class="toolbar">
    <select id="fs">
      <option value="">全部状态</option>
      <option value="success">success</option>
      <option value="dry_run">dry_run</option>
      <option value="failed">failed</option>
      <option value="skipped">skipped</option>
    </select>
    <input id="q" placeholder="搜索标题 / 条目ID…" style="width:220px">
  </div>
  <table id="tbl">
    <thead><tr><th>时间</th><th>状态</th><th>标题</th><th>条目ID</th><th>后端</th>
    <th>远端ID</th><th class="num">耗时</th><th>错误</th></tr></thead>
    <tbody>{_rows(history)}</tbody>
  </table>
</div>
<footer>本报表由 videopub 自动生成，可离线打开。</footer>
<script>
  const fs=document.getElementById('fs'), q=document.getElementById('q');
  const rows=[...document.querySelectorAll('#tbl tbody tr')];
  function apply(){{
    const s=fs.value, k=q.value.trim().toLowerCase();
    rows.forEach(r=>{{
      const okS=!s||r.dataset.status===s;
      const okQ=!k||r.innerText.toLowerCase().includes(k);
      r.style.display=(okS&&okQ)?'':'none';
    }});
  }}
  fs.addEventListener('change',apply); q.addEventListener('input',apply);
</script>
</body></html>"""
    target.write_text(doc, encoding="utf-8")
    return target
