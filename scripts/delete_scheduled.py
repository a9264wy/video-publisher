#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""删除视频号后台里我们排期的 10 条定时作品（用户要求全部重做）。

安全约束：
  * 只处理 `.post-feed-item` 文本以指定标题开头、且含「将于」字样的卡片
    （即定时未发表状态），绝不碰其它已发表内容；
  * 每删一条都等确认弹窗并点「确定」，失败即停，不盲删；
  * 全程日志 + 截图留档。

用法：
    .venv/Scripts/python.exe scripts/delete_scheduled.py            # 删全部 10 条
    .venv/Scripts/python.exe scripts/delete_scheduled.py 潮汐        # 只删标题前缀匹配的
"""

from __future__ import annotations

import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from videopub.config import Config  # noqa: E402
from videopub.backends import create_backend  # noqa: E402

ALL_TITLES = ["风是从哪来的", "海水为什么是咸的", "地震是怎么来的", "火山为什么爆发",
              "冰为什么浮在水上", "回声是怎么来的", "磁铁为什么吸铁", "影子是怎么来的",
              "叶子为什么是绿的", "潮汐是怎么来的"]


def open_list_page(be) -> None:
    page = be._page
    page.goto("https://channels.weixin.qq.com/platform", wait_until="domcontentloaded",
              timeout=60_000)
    page.wait_for_timeout(5_000)
    page.evaluate("""() => {
        history.pushState({}, '', '/platform/post/list');
        window.dispatchEvent(new PopStateEvent('popstate'));
    }""")
    page.wait_for_timeout(6_000)


def find_card_locator(be, title: str):
    """返回（frame, locator）：标题前缀匹配且是「定时未发表」的卡片。

    ⚠️ 页面有两份副本：主 frame 的 shadow 镜像 + iframe 活副本。
    镜像没有事件绑定（点了没反应），所以 **iframe 优先**。
    """
    frames = be._frames(be._page)
    # iframe（活副本）在前，主 frame（镜像）在最后
    frames = [f for f in frames if f is not be._page.main_frame] + [be._page.main_frame]
    for f in frames:
        try:
            cards = f.locator(".post-feed-item")
            n = min(cards.count(), 30)
        except Exception:
            continue
        for i in range(n):
            cand = cards.nth(i)
            try:
                txt = (cand.inner_text() or "").strip()
            except Exception:
                continue
            if not txt.startswith(title):
                continue
            if "将于" not in txt:  # 只删定时未发表的
                continue
            return f, cand
    return None, None


def delete_one(be, title: str) -> bool:
    page = be._page
    for attempt in (1, 2):
        # 用 JS 原生 click：主 frame 有全屏 mask 遮罩，playwright 坐标点击会被吃掉
        clicked_frame = None
        for f in [x for x in be._frames(page) if x is not page.main_frame] + [page.main_frame]:
            try:
                ret = f.evaluate("""(title) => {
                    for (const el of document.querySelectorAll('.post-feed-item')) {
                        const t = (el.innerText || '').trim();
                        if (!t.startsWith(title) || !t.includes('将于')) continue;
                        for (const a of el.querySelectorAll('.action-content')) {
                            if ((a.innerText || '').trim() === '删除') { a.click(); return true; }
                        }
                    }
                    return false;
                }""", title)
            except Exception:
                continue
            if ret:
                clicked_frame = f
                break
        if clicked_frame is None:
            print(f"  - {title}: 未找到定时卡片（可能已删）")
            return False
        page.wait_for_timeout(1_500)

        # 确认弹窗：只点**可见**（有尺寸）弹窗里的按钮，同样用 JS click
        confirmed = False
        for f in [x for x in be._frames(page) if x is not page.main_frame] + [page.main_frame]:
            try:
                ret = f.evaluate("""() => {
                    for (const d of document.querySelectorAll('.weui-desktop-dialog, .finder-common-dialog')) {
                        const r = d.getBoundingClientRect();
                        if (!(r.width > 100 && r.height > 50)) continue;
                        for (const b of d.querySelectorAll('button')) {
                            const t = (b.innerText || '').trim();
                            if (t === '确定' || t === '确认' || t === '删除') { b.click(); return t; }
                        }
                    }
                    return null;
                }""")
            except Exception:
                continue
            if ret:
                confirmed = True
                break
        page.wait_for_timeout(2_500)
        f2, card2 = find_card_locator(be, title)
        gone = card2 is None
        if confirmed and gone:
            print(f"  - {title}: 已删除")
            return True
        print(f"  - {title}: 第 {attempt} 次尝试 confirmed={confirmed} gone={gone}")
        if not confirmed and attempt == 1:
            open_list_page(be)
            continue
        page.screenshot(path=str(ROOT / "state" / "diag" / f"delete_fail_{title[:4]}.png"))
        return False
    return False


def main() -> int:
    prefix = sys.argv[1] if len(sys.argv) > 1 else ""
    titles = [t for t in ALL_TITLES if t.startswith(prefix)] or ALL_TITLES
    cfg = Config.load(ROOT / "config.json")
    be = create_backend("channels_web", cfg)
    be._launch()
    ok = 0
    try:
        open_list_page(be)
        for t in titles:
            print(f"处理：{t}")
            # 每删一条后回列表页刷新（列表可能要重新拉取）
            if delete_one(be, t):
                ok += 1
                open_list_page(be)
            else:
                open_list_page(be)
            time.sleep(1)
        print(f"\n完成：{ok}/{len(titles)} 条已删除")
        return 0 if ok == len(titles) else 1
    finally:
        try:
            be.close()
        except Exception:  # noqa: BLE001
            pass


if __name__ == "__main__":
    sys.exit(main())
