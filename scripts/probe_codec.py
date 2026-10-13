#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""诊断脚本：Chrome 到底能不能解码这些视频？

线索：控制台出现 ``@@@[编辑Dialog]使用video硬解获取帧: 0`` —— 平台的
rhino/FFmpeg-WASM SDK 从我们的视频里抽帧，得到 **0 帧**，
于是封面预览永远「生成中」、上传进度停在 0%。

本脚本把文件塞进页面里的 file input，再用 ``URL.createObjectURL`` 建 blob，
丢进 ``<video>`` 里读元数据，并顺带查 codec 支持表。纯只读、不提交。

用法：
    python scripts/probe_codec.py [item_id]
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

CHECK = r"""(async (input) => {
  const out = {foundInput: !!input, fileCount: 0, file: null, video: null,
               codecs: {}, mse: {}};
  if (!input || !input.files || !input.files.length) return out;
  const f = input.files[0];
  out.fileCount = input.files.length;
  out.file = {name: f.name, size: f.size, type: f.type,
              lastModified: f.lastModified};
  const v = document.createElement('video');
  v.muted = true; v.playsInline = true; v.preload = 'metadata';
  out.codecs.canPlay_avc1 = v.canPlayType('video/mp4; codecs="avc1.42E01E"');
  out.codecs.canPlay_h264_generic = v.canPlayType('video/mp4');
  out.codecs.canPlay_hevc = v.canPlayType('video/mp4; codecs="hvc1.1.6.L93.B0"');
  try { out.mse.avc1 = MediaSource.isTypeSupported('video/mp4; codecs="avc1.42E01E"'); }
  catch (e) { out.mse.avc1 = 'err ' + e; }
  out.hasWebCodecs = typeof VideoDecoder !== 'undefined';
  out.hasSAB = typeof SharedArrayBuffer !== 'undefined';
  out.crossOriginIsolated = self.crossOriginIsolated;

  const url = URL.createObjectURL(f);
  v.src = url;
  const res = await new Promise(resolve => {
    let done = false;
    const finish = (how) => { if (!done) { done = true; resolve(how); } };
    v.onloadedmetadata = () => finish('loadedmetadata');
    v.onerror = () => finish('error');
    setTimeout(() => finish('timeout'), 8000);
  });
  out.video = {
    how: res,
    duration: v.duration, videoWidth: v.videoWidth, videoHeight: v.videoHeight,
    readyState: v.readyState, networkState: v.networkState,
    errorCode: v.error ? v.error.code : null,
    errorMessage: v.error ? String(v.error.message).slice(0, 200) : null
  };
  // 尝试真正解码一帧
  try {
    v.currentTime = 0.5;
    const drew = await new Promise(resolve => {
      let done = false;
      const fin = (ok) => { if (!done) { done = true; resolve(ok); } };
      v.onseeked = () => {
        try {
          const c = document.createElement('canvas');
          c.width = v.videoWidth || 16; c.height = v.videoHeight || 16;
          const ctx = c.getContext('2d');
          ctx.drawImage(v, 0, 0, c.width, c.height);
          const d = ctx.getImageData(0, 0, 1, 1).data;
          fin({ok: true, pixel: [d[0], d[1], d[2], d[3]]});
        } catch (e) { fin({ok: false, err: String(e).slice(0, 200)}); }
      };
      v.onerror = () => fin({ok: false, err: 'onerror'});
      setTimeout(() => fin({ok: false, err: 'seek timeout'}), 8000);
    });
    out.drawFrame = drew;
  } catch (e) { out.drawFrame = {ok: false, err: String(e).slice(0, 200)}; }
  URL.revokeObjectURL(url);
  return out;
})()"""


def main() -> int:
    item_id = sys.argv[1] if len(sys.argv) > 1 else "01"
    cfg = Config.load("config.json")
    items = [it for it in scan_source(cfg) if it.item_id.startswith(item_id)]
    item = items[0]
    print(f"条目：{item.item_id}")
    print(f"文件：{item.video_path}")

    backend = create_backend("channels_web", cfg)
    try:
        ctx = backend._launch(headless=True)
        page = backend._page or ctx.new_page()
        backend.authenticate()
        backend._enter_create_page(page)
        page.wait_for_timeout(3000)

        loc = backend._locate(page, "file_input")
        loc.set_input_files(item.video_path)
        page.wait_for_timeout(2000)

        # 直接把 file input 元素传给 locator.evaluate ——
        # 它在 shadow root 里，document.querySelector 是找不到的
        result = loc.evaluate(CHECK)
        result["frame_url"] = getattr(page, "url", "")
        print(json.dumps(result, ensure_ascii=False, indent=2))
        (ROOT / "state" / "codec_check.json").write_text(
            json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
        print(f"\n已写出：{ROOT / 'state' / 'codec_check.json'}")

        v = result.get("video") or {}
        if v.get("how") == "loadedmetadata" and v.get("videoWidth"):
            print(f"\n结论：Chrome 能解码（{v['videoWidth']}x{v['videoHeight']}, "
                  f"{v['duration']:.1f}s）→ 问题不在文件/编解码")
        else:
            print(f"\n结论：Chrome **无法解码**该视频 → {v}")
        return 0
    finally:
        backend.close()


if __name__ == "__main__":
    sys.exit(main())
