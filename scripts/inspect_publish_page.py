#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""只读探查：从视频号助手后台进入「发表动态」页，并 dump 真实 DOM 结构。

⚠️ 本脚本**只读取与截图**，绝不点击「发表」/「保存草稿」，不会在你的账号上
产生任何内容。用途是校准 channels_web 后端的发布选择器。

用法：
    python scripts/inspect_publish_page.py
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from playwright.sync_api import sync_playwright  # noqa: E402

HOME = "https://channels.weixin.qq.com/platform"

DUMP_JS = """() => {
  // 关键：视频号助手把表单放在 **Shadow DOM** 里，原生 querySelectorAll 穿不过去，
  // 必须递归 shadowRoot 才能采到真实控件（这是实测踩到的坑）。
  const all = [];
  const seen = new Set();
  function collect(root, depth) {
    if (!root || depth > 12) return;
    let nodes = [];
    try { nodes = [...root.querySelectorAll('input,textarea,[contenteditable],button,img,canvas')]; }
    catch (e) { return; }
    for (const el of nodes) {
      if (seen.has(el)) continue;
      seen.add(el);
      all.push(el);
    }
    let hosts = [];
    try { hosts = [...root.querySelectorAll('*')]; } catch (e) { return; }
    for (const el of hosts) {
      if (el.shadowRoot) collect(el.shadowRoot, depth + 1);
    }
  }
  collect(document, 0);

  const pick = (el) => {
    const r = el.getBoundingClientRect();
    let cls = '';
    try { cls = String(el.getAttribute('class') || ''); } catch (e) {}
    return {
      tag: el.tagName, cls: cls.slice(0, 100), id: el.id || '',
      type: el.getAttribute('type') || '',
      accept: el.getAttribute('accept') || '',
      placeholder: el.getAttribute('placeholder') || '',
      contenteditable: el.getAttribute('contenteditable') || '',
      text: (el.innerText || el.value || '').trim().replace(/\\n/g, ' ').slice(0, 40),
      x: Math.round(r.x), y: Math.round(r.y),
      w: Math.round(r.width), h: Math.round(r.height),
      visible: r.width > 0 && r.height > 0
    };
  };
  const by = (tag) => all.filter(el => el.tagName === tag).map(pick);
  return {
    url: location.href,
    total_collected: all.length,
    file_inputs: by('INPUT').filter(x => x.type === 'file'),
    inputs: by('INPUT').filter(x => x.type !== 'file').slice(0, 20),
    textareas: by('TEXTAREA'),
    editables: all.filter(el => el.getAttribute('contenteditable'))
                    .map(pick),
    images: by('IMG').filter(x => x.visible).slice(0, 12),
    canvases: by('CANVAS').filter(x => x.visible).slice(0, 8),
    buttons: by('BUTTON').filter(x => x.visible),
    body: document.body.innerText.replace(/\\n+/g, ' | ').slice(0, 500)
  };
}"""


def main() -> int:
    ss = ROOT / "state" / "channels_storage.json"
    if not ss.exists():
        print("缺少登录态，请先执行: python main.py login", file=sys.stderr)
        return 2

    with sync_playwright() as p:
        browser = p.chromium.launch(channel="chrome", headless=True)
        ctx = browser.new_context(storage_state=str(ss),
                                  viewport={"width": 1440, "height": 900}, locale="zh-CN")
        page = ctx.new_page()
        page.goto(HOME, wait_until="domcontentloaded", timeout=60000)
        page.wait_for_timeout(9000)

        # 1) 后台内的路由链接
        links = page.evaluate(
            "() => [...document.querySelectorAll('a')].map(a => ({href:a.getAttribute('href')||'',"
            "text:(a.innerText||'').trim().slice(0,20)})).filter(a=>a.href).slice(0, 10)"
        )
        print("后台链接(前10):", json.dumps(links, ensure_ascii=False))

        # 2) 客户端路由进入发表动态页（避免整页刷新被重定向回首页）
        navigated = False
        for path in ("/platform/post/create", "/platform/post/list"):
            try:
                page.evaluate("(p) => { history.pushState({}, '', p);"
                              " window.dispatchEvent(new PopStateEvent('popstate')); }", path)
                page.wait_for_timeout(4000)
                if "/post/" in page.url:
                    navigated = True
                    break
            except Exception as exc:
                print(f"路由 {path} 失败: {exc}", file=sys.stderr)

        # 表单是懒加载的，等真实控件出现再 dump（否则拿到空 DOM）
        for wait_for in ("text=视频描述", "text=上传时长", "textarea"):
            try:
                page.wait_for_selector(wait_for, timeout=30000)
                print(f"已等到控件: {wait_for}", file=sys.stderr)
                break
            except Exception:
                continue
        page.wait_for_timeout(3000)

        info = page.evaluate(DUMP_JS)
        info["navigated"] = navigated
        page.screenshot(path=str(ROOT / "state" / "inspect_create.png"))

        out = ROOT / "state" / "inspect_create.json"
        out.write_text(json.dumps(info, ensure_ascii=False, indent=2), encoding="utf-8")
        browser.close()

    print(json.dumps(info, ensure_ascii=False, indent=2)[:2500])
    print(f"\n已写入 {out}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
