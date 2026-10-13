#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""诊断脚本：播放器实测 —— 这个浏览器能不能解码这段视频？

做法绕开 file input 的一切坑：把视频文件在 Python 侧读成 base64，
送进页面里造 Blob → `<video>` → 读元数据 → 真正 draw 一帧。
同时打印 codec 支持表与 WebCodecs / SharedArrayBuffer / crossOriginIsolated。

这一步用来回答：「0 帧」到底是**浏览器的解码能力问题**，还是页面 SDK 的问题。

⚠️ 只读。

用法：
    python scripts/probe_decode.py [item_id]
"""

from __future__ import annotations

import base64
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from videopub.config import Config  # noqa: E402
from videopub.scanner import scan_source  # noqa: E402
from videopub.backends import create_backend  # noqa: E402

JS = r"""(async () => {
  const b64 = window.__b64 || '';
  const out = {
    ua: navigator.userAgent,
    expectedLen: window.__expect,
    gotLen: b64.length,
    crossOriginIsolated: self.crossOriginIsolated,
    hasSAB: typeof SharedArrayBuffer !== 'undefined',
    hasVideoDecoder: typeof VideoDecoder !== 'undefined',
    hasVideoEncoder: typeof VideoEncoder !== 'undefined',
    hardwareConcurrency: navigator.hardwareConcurrency,
    codecs: {}, mse: {}
  };
  const v0 = document.createElement('video');
  out.codecs.avc1_baseline = v0.canPlayType('video/mp4; codecs="avc1.42E01E"');
  out.codecs.avc1_high = v0.canPlayType('video/mp4; codecs="avc1.640028"');
  out.codecs.mp4_generic = v0.canPlayType('video/mp4');
  out.codecs.hevc = v0.canPlayType('video/mp4; codecs="hvc1.1.6.L93.B0"');
  try { out.mse.avc1 = MediaSource.isTypeSupported('video/mp4; codecs="avc1.42E01E"'); }
  catch (e) { out.mse.avc1 = 'err'; }
  if (b64.length !== window.__expect) {
    out.fatal = 'base64 长度不符（传输被截断）';
    return out;
  }

  const bin = atob(b64);
  const len = bin.length;
  const u8 = new Uint8Array(len);
  for (let i = 0; i < len; i++) u8[i] = bin.charCodeAt(i);
  const blob = new Blob([u8], {type: 'video/mp4'});
  out.blobSize = blob.size;
  const url = URL.createObjectURL(blob);

  const v = document.createElement('video');
  v.muted = true; v.playsInline = true; v.preload = 'auto';
  v.setAttribute('muted', ''); v.setAttribute('playsinline', '');
  // 必须挂到文档里：游离的 <video> 在 Chrome 里可能根本不会启动解码管线
  v.style.cssText = 'position:fixed;left:0;top:0;width:320px;height:180px;opacity:0.01;pointer-events:none;';
  document.body.appendChild(v);
  v.src = url;

  const meta = await new Promise(resolve => {
    let done = false;
    const fin = (how) => { if (!done) { done = true; resolve(how); } };
    v.onloadedmetadata = () => fin('loadedmetadata');
    v.onloadeddata = () => fin('loadeddata');
    v.onerror = () => fin('error');
    setTimeout(() => fin('timeout'), 15000);
  });
  out.meta = {how: meta, duration: v.duration, w: v.videoWidth, h: v.videoHeight,
              readyState: v.readyState, networkState: v.networkState,
              errCode: v.error ? v.error.code : null,
              errMsg: v.error ? String(v.error.message).slice(0, 200) : null};

  // 尝试播放，逼解码管线动起来
  try { await v.play(); out.played = true; }
  catch (e) { out.played = false; out.playErr = String(e).slice(0, 150); }

  // 等 readyState >= 2（有当前帧数据），超时即视为「解不出来」
  const waitFrame = await new Promise(resolve => {
    let done = false;
    const fin = (how) => { if (!done) { done = true; resolve(how); } };
    const tick = () => {
      if (v.readyState >= 2 && v.videoWidth > 0) return fin('readyState>=2');
      if (done) return;
      setTimeout(tick, 200);
    };
    tick();
    setTimeout(() => fin('timeout readyState=' + v.readyState), 15000);
  });
  out.waitFrame = waitFrame;

  // 真正解码一帧（能 draw 出来才叫能解码）
  out.decode = (() => {
    try {
      const c = document.createElement('canvas');
      c.width = v.videoWidth || 32; c.height = v.videoHeight || 32;
      const ctx = c.getContext('2d');
      ctx.drawImage(v, 0, 0, c.width, c.height);
      const d = ctx.getImageData(Math.floor(c.width / 2), Math.floor(c.height / 2), 1, 1).data;
      return {ok: true, pixel: [d[0], d[1], d[2], d[3]]};
    } catch (e) { return {ok: false, err: String(e).slice(0, 250)}; }
  })();
  out.finalReadyState = v.readyState;
  out.currentTime = v.currentTime;
  try { v.pause(); } catch (e) {}
  v.remove();
  URL.revokeObjectURL(url);
  return out;
})()"""


def main() -> int:
    item_id = sys.argv[1] if len(sys.argv) > 1 else "01"
    cfg = Config.load("config.json")
    items = [it for it in scan_source(cfg) if it.item_id.startswith(item_id)]
    item = items[0]
    data = Path(item.video_path).read_bytes()
    b64 = base64.b64encode(data).decode("ascii")
    print(f"条目：{item.item_id}  {len(data)/1024/1024:.2f}MB  base64 {len(b64)/1024/1024:.2f}MB")

    backend = create_backend("channels_web", cfg)
    try:
        ctx = backend._launch(headless=True)
        page = backend._page or ctx.new_page()
        backend.authenticate()
        backend._enter_create_page(page)
        page.wait_for_timeout(2000)

        # 分块传输：整块 4MB 字符串过一次 CDP 会被截断（atob 报 InvalidCharacterError）
        page.main_frame.evaluate("() => { window.__b64 = ''; }")
        CHUNK = 200_000
        for i in range(0, len(b64), CHUNK):
            page.main_frame.evaluate(
                "(c) => { window.__b64 += c; }", b64[i:i + CHUNK])
        print(f"已分块送入 {len(b64)} 字符（{(len(b64)+CHUNK-1)//CHUNK} 块）")

        page.main_frame.evaluate("(n) => { window.__expect = n; }", len(b64))
        res = page.main_frame.evaluate(JS)
        print("\n" + json.dumps(res, ensure_ascii=False, indent=2))
        (ROOT / "state" / "decode_check.json").write_text(
            json.dumps(res, ensure_ascii=False, indent=2), encoding="utf-8")
        print(f"\n已写出：{ROOT / 'state' / 'decode_check.json'}")

        dec = res.get("decode") or {}
        meta = res.get("meta") or {}
        if dec.get("ok"):
            print(f"\n结论：浏览器**可以解码**（{meta.get('w')}x{meta.get('h')}, "
                  f"{meta.get('duration')}s, 取到像素 {dec.get('pixel')}）")
        else:
            print(f"\n结论：浏览器**无法解码** → meta={meta} decode={dec}")
        return 0
    finally:
        backend.close()


if __name__ == "__main__":
    sys.exit(main())
