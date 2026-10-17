#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""验证生产代码 `_set_cover_card` 在当前页面上能否真正设上双封面。

流程与 publish 相同（进页 → 投递视频 → 清位置 → 设封面），
但**不提交、不发布**，最后明确打印每张卡是否成功。

加 ``--full`` 会额外执行 publish 流里的「填描述 + 参加活动」两步，
用于对照：封面打不开是否由这两步残留的浮层/遮罩造成。

用法：
    .venv/Scripts/python.exe scripts/test_cover_flow.py
    .venv/Scripts/python.exe scripts/test_cover_flow.py --full
    .venv/Scripts/python.exe scripts/test_cover_flow.py --full \
        --source-dir "H:/去1qa/.../少儿科普白版-20261017"
"""

from __future__ import annotations

import logging
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

logging.basicConfig(level=logging.INFO, format="%(levelname)s | %(message)s", stream=sys.stdout)

from videopub.config import Config  # noqa: E402
from videopub.scanner import scan_source  # noqa: E402
from videopub.backends import create_backend  # noqa: E402


class _CoverLogCollector(logging.Handler):
    """收集「封面已上传」日志，用于明确判定每张卡是否真的设上。"""

    def __init__(self):
        super().__init__()
        self.records: list[str] = []

    def emit(self, record):
        msg = record.getMessage()
        if "封面已上传" in msg or "未能点开" in msg:
            self.records.append(msg)


DUMP_JS = """
() => {
  const out = { at: null, overlays: [], btn: null, maskInfo: null };
  const ifr = document.querySelector('iframe');
  const doc = ifr && ifr.contentDocument ? ifr.contentDocument : document;
  const fr = ifr && ifr.contentWindow ? ifr.contentWindow : window;
  // 主文档全屏 mask 详情
  document.querySelectorAll('div.mask, .weui-desktop-mask, .ant-modal-wrap, .ant-drawer-mask').forEach(el => {
    const cs = getComputedStyle(el);
    if (cs.display === 'none' || cs.visibility === 'hidden' || +cs.opacity === 0) return;
    const r = el.getBoundingClientRect();
    if (r.width < 100) return;
    const chain = [];
    let p = el;
    for (let i = 0; p && i < 4; i++) {
      chain.push(`${p.tagName}.${(p.className || '').toString().slice(0, 70)}`);
      p = p.parentElement;
    }
    out.maskInfo = { chain, zIndex: cs.zIndex, position: cs.position,
                     rect: [Math.round(r.left), Math.round(r.top), Math.round(r.width), Math.round(r.height)],
                     html: el.outerHTML.slice(0, 300) };
  });
  // 封面编辑按钮（3:4 卡）：主文档 + shadow 遍历找
  const btns = [];
  const findBtns = (root) => {
    root.querySelectorAll('*').forEach(el => {
      if (el.shadowRoot) findBtns(el.shadowRoot);
      const t = (el.innerText || '').trim();
      if (t === '编辑' && el.getBoundingClientRect().width > 0) btns.push(el);
    });
  };
  try { findBtns(document); } catch (e) {}
  const btn = btns[0];
  if (btn) {
    const r = btn.getBoundingClientRect();
    const cx = r.left + r.width / 2, cy = r.top + r.height / 2;
    const chain = [];
    let el = document.elementFromPoint(cx, cy);
    for (let i = 0; el && i < 6; i++) {
      const er = el.getBoundingClientRect();
      chain.push(`${el.tagName}.${(el.className || '').toString().slice(0, 60)} ` +
        `[${Math.round(er.left)},${Math.round(er.top)},${Math.round(er.width)}x${Math.round(er.height)}]`);
      el = el.parentElement;
    }
    const cs = getComputedStyle(btn);
    out.btn = { rect: [Math.round(r.left), Math.round(r.top), Math.round(r.width), Math.round(r.height)],
                pointerEvents: cs.pointerEvents, opacity: cs.opacity, chain };
    out.at = [Math.round(cx), Math.round(cy)];
  }
  // 全文档（含 shadow/iframe）可见浮层
  const scan = (root, label) => {
    root.querySelectorAll('*').forEach(el => {
      const cn = (el.className || '').toString();
      if (!/popover|mask|modal|dialog|toast|drawer/i.test(cn)) return;
      const cs = fr.getComputedStyle(el);
      if (cs.display === 'none' || cs.visibility === 'hidden' || +cs.opacity === 0) return;
      const r = el.getBoundingClientRect();
      if (r.width < 5 || r.height < 5) return;
      out.overlays.push(`${label} ${el.tagName}.${cn.slice(0, 50)} ` +
        `[${Math.round(r.left)},${Math.round(r.top)},${Math.round(r.width)}x${Math.round(r.height)}] ` +
        `"${(el.innerText || '').trim().slice(0, 40)}"`);
    });
  };
  scan(doc, 'iframe');
  try { scan(document, 'main'); } catch (e) {}
  return out;
}
"""


def _dump_overlays(be, page, tag):
    try:
        info = page.evaluate(DUMP_JS)
    except Exception as exc:
        print(f"[dump:{tag}] evaluate 失败: {exc}")
        return
    print(f"[dump:{tag}] elementFromPoint={info['at']}")
    if info.get("maskInfo"):
        m = info["maskInfo"]
        print(f"[dump:{tag}] ★全屏mask z-index={m['zIndex']} position={m['position']} rect={m['rect']}")
        print(f"[dump:{tag}]   父链: {' < '.join(m['chain'])}")
        print(f"[dump:{tag}]   HTML: {m['html'][:200]}")
    if info["btn"]:
        b = info["btn"]
        print(f"[dump:{tag}] 按钮 rect={b['rect']} pointerEvents={b['pointerEvents']} opacity={b['opacity']}")
        for i, c in enumerate(b["chain"]):
            print(f"[dump:{tag}]   命中链[{i}] {c}")
    for o in info["overlays"][:15]:
        print(f"[dump:{tag}] 浮层 {o}")


def main() -> int:
    full = "--full" in sys.argv
    dump = "--dump" in sys.argv
    src = None
    if "--source-dir" in sys.argv:
        src = sys.argv[sys.argv.index("--source-dir") + 1]
    cfg = Config.load(ROOT / "config.json")
    items = scan_source(cfg, source_dir=src)
    if not items:
        print("源目录没有可发布条目")
        return 1
    item = items[0]
    print(f"使用条目：{item.title}（目录 {item.directory}）")
    collector = _CoverLogCollector()
    logging.getLogger("videopub.backend.channels_web").addHandler(collector)
    be = create_backend("channels_web", cfg)
    be._launch()
    page = be._page
    try:
        be._enter_create_page(page)
        be._attach_upload_probe(page)
        be._upload_video(page, item)
        be._dismiss_blocking_dialogs(page)
        page.wait_for_timeout(2000)

        if full:
            print("===== 额外执行：填描述 + 参加活动（复刻 publish 流）=====")
            be._fill_description(page, item)
            if dump:
                _dump_overlays(be, page, "描述后")
            act = cfg.content.activity
            if act:
                be._join_activity(page, act)
            if dump:
                _dump_overlays(be, page, "活动后")
        else:
            print("===== 跳过描述与活动（最小流程）=====")

        be._clear_location(page)
        page.wait_for_timeout(1000)
        if dump:
            _dump_overlays(be, page, "位置后")

        folder = Path(item.cover_path).parent
        results = {}
        for wrap, keys, slot in (
            ("vertical-cover-wrap", ("3x4", "3：4", "3X4"), "3:4"),
            ("horizon-cover-wrap", ("4x3", "4：3", "4X3"), "4:3"),
        ):
            img = be._find_cover_file(folder, keys)
            print(f"\n===== 设置 {slot} 封面（{img.name}）=====")
            if dump:
                _dump_overlays(be, page, f"{slot}-前")
            be._set_cover_card(page, wrap, img)
            if dump:
                _dump_overlays(be, page, f"{slot}-后")
            # 判定：再收一次候选，看该卡是否已换成新素材（弹窗标题不再出现=已关闭）
            results[slot] = img.name
            page.wait_for_timeout(1000)

        page.screenshot(path=str(ROOT / "state" / "diag" / "cover_flow_result.png"), full_page=True)
        print("\n===== 结果 =====")
        if collector.records:
            for r in collector.records:
                print(f"  {r}")
        else:
            print("  （未捕获到封面上传/失败日志）")
        print("  截图：state/diag/cover_flow_result.png")
        return 0
    finally:
        try:
            be.close()
        except Exception:
            pass


if __name__ == "__main__":
    sys.exit(main())