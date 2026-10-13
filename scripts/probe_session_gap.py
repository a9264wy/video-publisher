#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""对照「磁盘上的登录态」与「页面跑起来之后浏览器真实持有的登录态」。

动机：`state/channels_storage.json` 里只有 2 个 cookie（sessionid / wxuin），
且 localStorage 的 `finder_uin`、`finder_raw` 是空的。而请求里
`x-wechat-uin` 恒为 `0000000000`、`rawKeyBuff` 恒为空 —— 这两处都指向
「登录态存得不完整」。

本脚本判断：页面加载后 cookie / localStorage 会不会**长出更多东西**。
  * 会 → 说明当初 `login()` 存得太早（拿到 sessionid 就退出了），
    重新登录并等页面完全初始化再存，很可能解决问题；
  * 不会 → 那 2 个 cookie 就是这个站的全部，问题不在这。

只读，不产生任何内容。

用法：.venv/Scripts/python.exe scripts/probe_session_gap.py
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from videopub.config import Config  # noqa: E402
from videopub.backends import create_backend  # noqa: E402

OUT = ROOT / "state" / "diag"


def main() -> int:
    cfg = Config.load("config.json")
    store = ROOT / "state" / "channels_storage.json"
    before = json.loads(store.read_text(encoding="utf-8"))
    before_cookies = {c["name"]: c["value"] for c in before.get("cookies", [])}
    before_ls = {kv["name"]: kv.get("value")
                 for o in before.get("origins", []) for kv in o.get("localStorage", [])}

    backend = create_backend("channels_web", cfg)
    rep: dict = {}
    try:
        ctx = backend._launch()
        page = backend._page or ctx.new_page()
        backend.authenticate()
        # 给页面足够时间把该写的东西写完
        for _ in range(6):
            page.wait_for_timeout(5000)

        live = {c["name"]: c["value"] for c in (ctx.cookies() or [])}
        live_ls = page.evaluate("() => Object.fromEntries(Object.entries(localStorage))")

        rep = {
            "before_cookie_names": sorted(before_cookies),
            "live_cookie_names": sorted(live),
            "new_cookies": sorted(set(live) - set(before_cookies)),
            "missing_in_live": sorted(set(before_cookies) - set(live)),
            "before_ls_empty_keys": sorted(k for k, v in before_ls.items() if not v),
            "live_ls_empty_keys": sorted(k for k, v in (live_ls or {}).items() if not v),
            "ls_gained": sorted(k for k, v in (live_ls or {}).items()
                                if v and not before_ls.get(k)),
            "finder_uin_before": repr(before_ls.get("finder_uin")),
            "finder_uin_live": repr((live_ls or {}).get("finder_uin")),
            "finder_raw_before": repr(before_ls.get("finder_raw")),
            "finder_raw_live": repr((live_ls or {}).get("finder_raw")),
            "url": page.url,
        }
    except Exception as exc:  # noqa: BLE001
        import traceback
        rep = {"fatal": f"{type(exc).__name__}: {exc}", "tb": traceback.format_exc()[-1200:]}
    finally:
        try:
            backend.close()
        except Exception:
            pass

    OUT.mkdir(parents=True, exist_ok=True)
    p = OUT / "session_gap.json"
    p.write_text(json.dumps(rep, ensure_ascii=False, indent=2), encoding="utf-8")

    for k, v in rep.items():
        if k != "tb":
            print(f"  {k}: {v}")
    print(f"\n报告：{p}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
