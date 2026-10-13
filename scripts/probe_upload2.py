#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""一次性把所有可疑点全量落盘的诊断脚本（上传卡 0% 专用）。

与 probe_upload.py 的区别：那个只看「进度动不动」，本脚本回答「为什么不动」。

采集清单：
  1. 自动化指纹：navigator.webdriver / userAgent / crossOriginIsolated / SharedArrayBuffer
  2. 全 frame 的 file input 清单：所在 frame、命中选择器、可见性、accept、bounding box
  3. **投递前** 目标 input 的 files.length
  4. **投递后立刻** 从**同一个 frame** 回读 files.length / files[0].name / size
     —— 这是判「文件到底进没进去」的唯一硬证据
  5. 控制台全量消息（含 error / warning）
  6. 网络请求日志：所有 URL 里出现 up/upload/cdn/qcloud/ugc 的请求 + 响应状态
  7. 进度轮询（pct / 取消上传 / 重试 / 文案尾部）
  8. 截图

⚠️ 只投递文件、不点任何提交按钮 → 不会在账号上产生内容。

用法：
    .venv/Scripts/python.exe scripts/probe_upload2.py [headless|headful] [stealth|nostealth]
"""

from __future__ import annotations

import json
import re
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from videopub.config import Config  # noqa: E402
from videopub.scanner import scan_source  # noqa: E402
from videopub.backends import create_backend  # noqa: E402

OUT_DIR = ROOT / "state" / "diag"

FINGERPRINT_JS = r"""() => ({
  webdriver: navigator.webdriver,
  ua: navigator.userAgent,
  platform: navigator.platform,
  languages: (navigator.languages || []).join(','),
  plugins: (navigator.plugins ? navigator.plugins.length : -1),
  crossOriginIsolated: (typeof crossOriginIsolated !== 'undefined') ? crossOriginIsolated : null,
  hasSAB: (typeof SharedArrayBuffer !== 'undefined'),
  hardwareConcurrency: navigator.hardwareConcurrency,
  deviceMemory: navigator.deviceMemory,
  chrome: !!window.chrome,
  url: location.href,
  hasMediaSource: (typeof MediaSource !== 'undefined'),
  avc1: (typeof MediaSource !== 'undefined') ? MediaSource.isTypeSupported('video/mp4; codecs="avc1.42E01E"') : null,
  hasVideoDecoder: (typeof VideoDecoder !== 'undefined'),
  hasFileSystemAccess: (typeof window.showOpenFilePicker === 'function'),
  permissions: null,
})"""

# 枚举本 frame 里所有 file input
INPUTS_JS = r"""() => {
  const out = [];
  const all = document.querySelectorAll('input[type=file]');
  let i = 0;
  for (const el of all) {
    const r = el.getBoundingClientRect();
    const st = getComputedStyle(el);
    out.push({
      idx: i++,
      accept: el.getAttribute('accept') || '',
      multiple: !!el.multiple,
      filesLen: el.files ? el.files.length : -1,
      fileName: (el.files && el.files[0]) ? el.files[0].name : '',
      fileSize: (el.files && el.files[0]) ? el.files[0].size : -1,
      visible: (r.width > 0 && r.height > 0 && st.display !== 'none' && st.visibility !== 'hidden'),
      rect: [Math.round(r.x), Math.round(r.y), Math.round(r.width), Math.round(r.height)],
      disabled: !!el.disabled,
      outer: el.outerHTML.slice(0, 200),
      inShadow: (el.getRootNode() !== document),
    });
  }
  return out;
}"""

# 从某个 frame 内部再读一次 input.files（用 index 定位，避免 locator 往返）
READ_FILES_JS = r"""() => {
  const all = document.querySelectorAll('input[type=file]');
  return Array.from(all).map(el => ({
    accept: el.getAttribute('accept') || '',
    filesLen: el.files ? el.files.length : -1,
    names: el.files ? Array.from(el.files).map(f => f.name + ':' + f.size) : [],
  }));
}"""

STATUS_JS = r"""() => {
  const out = {text: '', hasCancel: false, hasRetry: false, hasForm: false, pct: null, media: 0};
  const stack = [document];
  const seen = new Set();
  let blob = [];
  while (stack.length) {
    const root = stack.pop();
    let all = [];
    try { all = root.querySelectorAll('*'); } catch (e) { continue; }
    for (const el of all) {
      if (el.shadowRoot && !seen.has(el)) { seen.add(el); stack.push(el.shadowRoot); }
    }
    let t = '';
    try { t = (root.textContent || ''); } catch (e) {}
    if (t) blob.push(t);
  }
  out.text = blob.join(' ').replace(/\s+/g, ' ');
  out.hasCancel = /取消上传/.test(out.text);
  out.hasRetry = /重新上传|上传失败/.test(out.text);
  out.hasForm = /视频描述/.test(out.text);
  out.media = document.querySelectorAll('img,video,canvas').length;
  const m = out.text.match(/(\d{1,3})\s*%/g);
  out.pct = m ? m.join(',') : null;
  out.len = out.text.length;
  out.tail = out.text.slice(0, 300);
  return out;
}"""

UPLOAD_HINT = re.compile(
    r"(up|upload|cdn|qcloud|myqcloud|ugc|wxqcloud|vod|cos\.|dns|channels)",
    re.I,
)


def main() -> int:
    args = [a.lower() for a in sys.argv[1:]]
    headless = "headful" not in args
    force_stealth = "stealth" in args
    no_stealth = "nostealth" in args

    cfg = Config.load("config.json")
    creds = cfg.credentials.get("channels_web", {}) or {}
    if force_stealth:
        creds["stealth"] = True
    if no_stealth:
        creds["stealth"] = False
    cfg.credentials["channels_web"] = creds

    items = scan_source(cfg)
    item = items[0]
    OUT_DIR.mkdir(parents=True, exist_ok=True)

    backend = create_backend("channels_web", cfg)
    report: dict = {
        "mode": "headless" if headless else "headful",
        "stealth": bool(creds.get("stealth", True)),
        "no_proxy": bool(creds.get("no_proxy", False)),
        "channel": creds.get("channel", ""),
        "item": item.item_id,
        "video": str(item.video_path),
        "video_size": item.video_size,
        "console": [],
        "requests": [],
        "responses": [],
        "failed": [],
    }

    try:
        ctx = backend._launch(headless=headless)
        page = backend._page or ctx.new_page()

        page.on("console", lambda m: report["console"].append(
            {"type": m.type, "text": (m.text or "")[:600]}))
        page.on("pageerror", lambda e: report["console"].append(
            {"type": "pageerror", "text": str(e)[:600]}))

        def _on_req(req):
            if UPLOAD_HINT.search(req.url):
                report["requests"].append({"method": req.method, "url": req.url[:400]})

        def _on_resp(resp):
            if UPLOAD_HINT.search(resp.url):
                report["responses"].append({"status": resp.status, "url": resp.url[:400]})

        def _on_fail(req):
            report["failed"].append({"url": req.url[:400], "err": str(req.failure)[:200]})

        page.on("request", _on_req)
        page.on("response", _on_resp)
        page.on("requestfailed", _on_fail)

        backend.authenticate()
        backend._enter_create_page(page)
        page.wait_for_timeout(4000)

        # ---- 1. 指纹（逐 frame） ----
        report["fingerprints"] = []
        for i, f in enumerate(backend._frames(page)):
            try:
                fp = f.evaluate(FINGERPRINT_JS)
            except Exception as exc:
                fp = {"error": str(exc)[:200]}
            fp["frame_index"] = i
            fp["frame_url"] = getattr(f, "url", "")
            report["fingerprints"].append(fp)

        # ---- 2. 全 frame file input 清单 ----
        report["inputs"] = []
        for i, f in enumerate(backend._frames(page)):
            try:
                ins = f.evaluate(INPUTS_JS)
            except Exception as exc:
                ins = [{"error": str(exc)[:200]}]
            for x in ins:
                x["frame_index"] = i
                x["frame_url"] = getattr(f, "url", "")
            report["inputs"].extend(ins)

        # ---- 3/4. 选目标 input 并投递 ----
        loc = backend._locate(page, "file_input")
        report["chosen_locator"] = str(loc) if loc is not None else None
        # 找出 locator 落在哪个 frame
        chosen_frame_idx = None
        for i, f in enumerate(backend._frames(page)):
            try:
                l2 = f.locator("input[type=file][accept*='video']")
                if l2.count() > 0:
                    chosen_frame_idx = i
                    break
                l2b = f.locator("input[type=file]")
                if l2b.count() > 0:
                    chosen_frame_idx = i
                    break
            except Exception:
                continue
        report["chosen_frame_index"] = chosen_frame_idx

        files = [f for f in backend._frames(page)]
        if chosen_frame_idx is not None:
            try:
                report["before_set"] = files[chosen_frame_idx].evaluate(READ_FILES_JS)
            except Exception as exc:
                report["before_set"] = {"error": str(exc)[:200]}

        t0 = time.time()
        loc.set_input_files(item.video_path)
        report["set_ok"] = True
        report["set_elapsed"] = round(time.time() - t0, 2)
        page.wait_for_timeout(1500)

        if chosen_frame_idx is not None:
            for k in range(3):
                try:
                    report[f"after_set_{k}"] = files[chosen_frame_idx].evaluate(READ_FILES_JS)
                except Exception as exc:
                    report[f"after_set_{k}"] = {"error": str(exc)[:200]}
                page.wait_for_timeout(1500)

        # ---- 7. 进度轮询 ----
        poll = []
        for i in range(18):  # 90s
            page.wait_for_timeout(5000)
            el = round(time.time() - t0, 1)
            best = None
            for f in backend._frames(page):
                try:
                    info = f.evaluate(STATUS_JS)
                except Exception:
                    continue
                if info.get("hasForm") or info.get("hasCancel"):
                    best = info
                    break
            if best is None:
                poll.append({"t": el, "err": "no_frame"})
                continue
            poll.append({"t": el, "pct": best.get("pct"), "cancel": best["hasCancel"],
                         "retry": best["hasRetry"], "media": best["media"],
                         "len": best.get("len"), "tail": best.get("tail")})
        report["poll"] = poll

        try:
            page.screenshot(path=str(OUT_DIR / f"probe2_{report['mode']}_"
                                          f"{'stealth' if report['stealth'] else 'plain'}.png"),
                            full_page=True)
        except Exception:
            pass

        report["ok"] = True
    except Exception as exc:  # noqa: BLE001
        import traceback
        report["ok"] = False
        report["fatal"] = f"{type(exc).__name__}: {exc}"
        report["traceback"] = traceback.format_exc()[-3000:]
    finally:
        try:
            backend.close()
        except Exception:
            pass

    tag = f"probe2_{report['mode']}_{'stealth' if report['stealth'] else 'plain'}"
    path = OUT_DIR / f"{tag}.json"
    path.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")

    # 终端摘要
    print(f"模式={report['mode']}  stealth={report['stealth']}  ok={report.get('ok')}")
    fps = report.get("fingerprints") or []
    for fp in fps[:3]:
        if "error" in fp:
            continue
        print(f"  frame[{fp['frame_index']}] webdriver={fp.get('webdriver')} "
              f"SAB={fp.get('hasSAB')} COI={fp.get('crossOriginIsolated')} "
              f"VideoDecoder={fp.get('hasVideoDecoder')} url={str(fp.get('frame_url'))[:70]}")
    print(f"  file inputs 共 {len(report.get('inputs') or [])} 个")
    for x in (report.get("inputs") or [])[:6]:
        print(f"    frame{x.get('frame_index')} idx{x.get('idx')} accept={x.get('accept','')[:30]!r} "
              f"visible={x.get('visible')} files={x.get('filesLen')} shadow={x.get('inShadow')}")
    print(f"  chosen_frame={report.get('chosen_frame_index')} set_ok={report.get('set_ok')} "
          f"elapsed={report.get('set_elapsed')}s")
    print(f"  before_set = {json.dumps(report.get('before_set'), ensure_ascii=False)[:300]}")
    for k in ("after_set_0", "after_set_1", "after_set_2"):
        print(f"  {k} = {json.dumps(report.get(k), ensure_ascii=False)[:300]}")
    print(f"  网络请求命中上传特征：{len(report.get('requests') or [])}")
    for r in (report.get("requests") or [])[:8]:
        print(f"    {r['method']} {r['url'][:110]}")
    print(f"  响应：{len(report.get('responses') or [])}")
    for r in (report.get("responses") or [])[:8]:
        print(f"    {r['status']} {r['url'][:110]}")
    print(f"  失败请求：{len(report.get('failed') or [])}")
    for r in (report.get("failed") or [])[:8]:
        print(f"    {r['err'][:60]} {r['url'][:110]}")
    errs = [c for c in (report.get("console") or []) if c["type"] in ("error", "pageerror", "warning")]
    print(f"  控制台 error/warning：{len(errs)}")
    for c in errs[:12]:
        print(f"    [{c['type']}] {c['text'][:180]}")
    print("  进度：")
    for p in (report.get("poll") or [])[:20]:
        print(f"    {json.dumps(p, ensure_ascii=False)[:220]}")
    if not report.get("ok"):
        print(f"  FATAL: {report.get('fatal')}")
    print(f"\n完整报告：{path}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
