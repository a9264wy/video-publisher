#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""逐个 `/cgi-bin/mmfinderassistant-bin/*` 请求做「成功 vs 失败」对照。

目标：找出 ``helper_upload_params`` 为何返回 ``{"errCode":300002,"errMsg":"request failed"}``。
方法：把同一批请求的 method / url / 关键请求头 / content-type / post_data 长度与片段 /
      响应体 全部落盘。用**成功的接口**（如 get_auth_info）当基线做 diff。

⚠️ 只投递文件、不点提交 → 不会产生内容。

用法：.venv/Scripts/python.exe scripts/probe_api_diff.py [headless|headful]
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from videopub.config import Config  # noqa: E402
from videopub.scanner import scan_source  # noqa: E402
from videopub.backends import create_backend  # noqa: E402

OUT = ROOT / "state" / "diag"
FAMILY = "mmfinderassistant-bin"
# 只留「有诊断价值」的头，避免噪声
KEEP_HEADERS = ("content-type", "content-length", "x-wechat", "x-wx", "referer",
                "origin", "cookie", "x-requested-with", "accept")


def main() -> int:
    args = [a.lower() for a in sys.argv[1:]]
    headless = "headful" not in args
    cfg = Config.load("config.json")
    items = scan_source(cfg)
    item = items[0]
    OUT.mkdir(parents=True, exist_ok=True)

    backend = create_backend("channels_web", cfg)
    recs: list[dict] = []

    def on_resp(r):
        u = r.url
        if FAMILY not in u and "finder" not in u:
            return
        req = r.request
        try:
            hdrs = {k.lower(): v for k, v in (req.headers or {}).items()}
        except Exception:
            hdrs = {}
        keep = {k: (v[:200] if isinstance(v, str) else v)
                for k, v in hdrs.items() if any(k.startswith(x) for x in KEEP_HEADERS)}
        try:
            pd = req.post_data or ""
        except Exception:
            pd = ""
        rec = {
            "status": r.status,
            "method": req.method,
            "path": u.split("?")[0].replace("https://channels.weixin.qq.com", ""),
            "query": u.split("?", 1)[1][:220] if "?" in u else "",
            "ctype": hdrs.get("content-type", ""),
            "post_len": len(pd),
            "post_head": pd[:300],
            "headers": keep,
            "resp": "",
        }
        try:
            body = r.body()
            rec["resp"] = body[:600].decode("utf-8", "replace")
        except Exception as exc:
            rec["resp"] = f"<{type(exc).__name__}>"
        recs.append(rec)

    try:
        ctx = backend._launch(headless=headless)
        page = backend._page or ctx.new_page()
        page.on("response", on_resp)
        backend.authenticate()
        backend._enter_create_page(page)
        page.wait_for_timeout(3000)
        loc = backend._locate(page, "file_input")
        loc.set_input_files(item.video_path)
        for _ in range(6):   # 30s
            page.wait_for_timeout(5000)
        ok = True
    except Exception as exc:  # noqa: BLE001
        import traceback
        ok = False
        recs.append({"fatal": f"{type(exc).__name__}: {exc}",
                     "tb": traceback.format_exc()[-1500:]})
    finally:
        try:
            backend.close()
        except Exception:
            pass

    p = OUT / ("api_diff_" + ("headless" if headless else "headful") + ".json")
    p.write_text(json.dumps(recs, ensure_ascii=False, indent=2), encoding="utf-8")

    print(f"ok={ok}  采集 {len(recs)} 条")
    seen = set()
    for r in recs:
        if "fatal" in r:
            print(r["fatal"]); continue
        key = (r["path"], r["post_len"])
        if key in seen:
            continue
        seen.add(key)
        resp = r["resp"].replace("\n", " ")[:200]
        print(f"\n[{r['status']}] {r['method']} {r['path']}  qlen={len(r['query'])} "
              f"ctype={r['ctype'][:40]!r} postLen={r['post_len']}")
        print(f"     post_head: {r['post_head'][:160]!r}")
        print(f"     resp     : {resp}")
    # 汇总：哪些 path 出现过 300002
    print("\n=== 结果汇总 ===")
    agg: dict[str, set] = {}
    for r in recs:
        if "fatal" in r:
            continue
        agg.setdefault(r["path"], set()).add(r["resp"][:60])
    for k, v in agg.items():
        print(f"  {k}: {sorted(v)[:2]}")
    print(f"\n报告：{p}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
