#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""点开「活动」下拉，枚举可参与的活动项。

只读 + 一次下拉展开：不投递文件、不提交。
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from videopub.config import Config  # noqa: E402
from videopub.backends import create_backend  # noqa: E402

OUT = ROOT / "state" / "diag" / "activity_options.json"


def main() -> int:
    cfg = Config.load(ROOT / "config.json")
    be = create_backend("channels_web", cfg)
    be._launch()
    page = be._page

    api_hits: list[str] = []

    def on_request(req):
        u = req.url
        if "/cgi-bin/" in u and "report" not in u and "perf" not in u:
            api_hits.append(f"{req.method} {u}")
            try:
                if req.post_data:
                    api_hits.append("    BODY: " + req.post_data[:500])
            except Exception:  # noqa: BLE001
                pass

    def on_response(resp):
        if "post_search_event" in resp.url:
            try:
                api_hits.append("SEARCH_RESP: " + resp.text()[:2500])
            except Exception as e:  # noqa: BLE001
                api_hits.append(f"SEARCH_RESP_ERR: {e}")

    try:
        page.on("request", on_request)
        page.on("response", on_response)
        be._enter_create_page(page)
        page.wait_for_timeout(2000)

        # 点开活动下拉（点可见的 activity-display-wrap / not-involve）
        clicked = be._click_first_visible(page, [
            "div.activity-display-wrap", "div.activity-display", "div.not-involve",
        ])
        print(f"点开活动下拉：{clicked}")
        page.wait_for_timeout(1000)

        # 在「搜索活动」输入框里键入关键词，触发活动列表加载
        sinp = be._locate_any(page, [
            ".activity-filter-wrap input.weui-desktop-form__input",
            ".activity-filter-wrap input[type='text']",
        ])
        if sinp is not None:
            sinp.click()
            kw = sys.argv[1] if len(sys.argv) > 1 else "少儿科普"
            sinp.type(kw, delay=80)
            print(f"已在搜索框输入：{kw}")
        else:
            print("⚠️ 未找到活动搜索框")

        # 轮询等待活动项异步加载（最多 12s）
        items = []
        loc = None
        for _ in range(24):
            page.wait_for_timeout(500)
            loc = be._locators_any(page, [".activity-item", "div.activity-item"])
            if loc is not None and loc.count() > 1:
                break
        if loc is not None:
            n = min(loc.count(), 40)
            for i in range(n):
                it = loc.nth(i)
                try:
                    txt = (it.inner_text() or "").strip().replace("\n", " | ")
                except Exception as e:  # noqa: BLE001
                    txt = f"<读取失败 {e}>"
                try:
                    vis = it.is_visible()
                except Exception:  # noqa: BLE001
                    vis = None
                items.append({"index": i, "visible": vis, "text": txt})
                print(f"  [{i}] 可见={vis}  {txt}")
        else:
            print("未找到 .activity-item")

        # 落盘下拉容器 HTML（便于离线分析结构）
        html_dump = None
        try:
            html_dump = page.evaluate("""() => {
                const out = [];
                const grab = (root, tag) => {
                    for (const el of root.querySelectorAll('.common-option-list-wrap, .activity-filter-wrap')) {
                        out.push({frame: tag, html: el.outerHTML.slice(0, 6000)});
                    }
                };
                grab(document, 'main');
                for (const f of document.querySelectorAll('iframe')) {
                    try { grab(f.contentDocument, 'iframe:' + f.src.slice(0, 60)); } catch (e) {}
                }
                for (const h of document.querySelectorAll('*')) {
                    if (h.shadowRoot) { try { grab(h.shadowRoot, 'shadow:' + h.tagName.toLowerCase()); } catch (e) {} }
                }
                return out;
            }""")
        except Exception as e:  # noqa: BLE001
            html_dump = [{"error": str(e)}]

        page.screenshot(path=str(ROOT / "state" / "diag" / "activity_options.png"))
        OUT.write_text(json.dumps(
            {"items": items, "api_hits": api_hits, "html": html_dump},
            ensure_ascii=False, indent=1), encoding="utf-8")
        print(f"已保存 {len(items)} 项 -> {OUT}")
        if api_hits:
            print("activity 相关请求：")
            for u in api_hits[:15]:
                print("  ", u[:140])
    finally:
        try:
            be.close()
        except Exception:  # noqa: BLE001
            pass
    return 0


if __name__ == "__main__":
    sys.exit(main())
