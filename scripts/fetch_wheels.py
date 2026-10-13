#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""离线 wheel 抓取器：绕过 pip 的索引解析，直接把 wheel 下到本地再安装。

背景：本机 pip 在沙箱内无法正常解析 PyPI 索引（报 "from versions: none"），
但直连镜像的 HTTP 下载是通的。因此改为：
  1) 从镜像的 simple 索引页解析出目标 wheel 的真实下载地址；
  2) 逐一下载到 .wheels/；
  3) 用 `pip install --no-index --find-links=.wheels` 完成安装。

用法：
    python scripts/fetch_wheels.py playwright pyee greenlet
"""

from __future__ import annotations

import html as html_mod
import re
import socket
import ssl
import sys
import urllib.parse
import urllib.request
from pathlib import Path

MIRRORS = [
    "https://pypi.tuna.tsinghua.edu.cn/simple/",
    "https://mirrors.aliyun.com/pypi/simple/",
    "https://mirrors.cloud.tencent.com/pypi/simple/",
]
OUT = Path(__file__).resolve().parent.parent / ".wheels"
socket.setdefaulttimeout(60)


def _version_key(v: str) -> tuple:
    parts = re.split(r"[._-]", v)
    out = []
    for p in parts:
        out.append((0, int(p)) if p.isdigit() else (1, p))
    return tuple(out)


def _get(url: str) -> bytes:
    ctx = ssl.create_default_context()
    req = urllib.request.Request(url, headers={"User-Agent": "wheel-fetcher/1.0"})
    with urllib.request.urlopen(req, context=ctx) as resp:
        return resp.read()


def index_url(base: str, pkg: str) -> str:
    return f"{base}{pkg}/"


def pick_wheel(pkg: str, base: str, want: str | None = None) -> tuple[str, str]:
    """返回 (版本, 绝对下载URL)。优先 win_amd64 / py3-none-any / cp313。

    ``want`` 为 ``"13.*"`` 之类的简单前缀/通配要求（用于满足依赖约束）。
    """
    page = _get(index_url(base, pkg)).decode("utf-8", "replace")
    hrefs = [html_mod.unescape(h) for h in re.findall(r'href="([^"]+)"', page)]
    hrefs = [h.split("#")[0] for h in hrefs]
    files = []
    for h in hrefs:
        name = h.rsplit("/", 1)[-1]
        if not name.endswith(".whl"):
            continue
        if pkg.lower().replace("-", "_") not in name.lower().replace("-", "_"):
            continue
        files.append((name, h))
    if not files:
        raise SystemExit(f"[{pkg}] 索引页未找到 wheel")

    def version_of(name: str) -> str:
        return name.split("-")[1]

    if want:
        prefix = want.rstrip("*").rstrip(".")
        files = [(n, h) for n, h in files if version_of(n).startswith(prefix)]
        if not files:
            raise SystemExit(f"[{pkg}] 索引页未找到匹配 {want} 的 wheel")

    def score(name: str) -> tuple:
        ver = version_of(name)
        good_plat = ("win_amd64" in name) or ("py3-none-any" in name)
        cp = "cp313" in name
        return (_version_key(ver), 1 if good_plat else 0, 1 if cp else 0)

    files.sort(key=lambda x: score(x[0]))
    name, href = files[-1]
    url = href if href.startswith("http") else urllib.parse.urljoin(index_url(base, pkg), href)
    return version_of(name), url


def main(pkgs: list[str]) -> int:
    OUT.mkdir(parents=True, exist_ok=True)
    print(f"输出目录: {OUT}")
    for spec in pkgs:
        pkg, _, want = spec.partition("==")
        want = want or None
        last_err = None
        for base in MIRRORS:
            try:
                ver, url = pick_wheel(pkg, base, want)
                target = OUT / url.rsplit("/", 1)[-1]
                if target.exists() and target.stat().st_size > 1024:
                    print(f"  [skip] {target.name}（已存在）")
                    last_err = None
                    break
                print(f"  [get ] {pkg} {ver}  <- {base}")
                data = _get(url)
                target.write_bytes(data)
                print(f"  [ok  ] {target.name}  {len(data) / 1024:.0f} KB")
                last_err = None
                break
            except Exception as exc:  # 换镜像重试
                last_err = f"{base} -> {type(exc).__name__}: {exc}"
                continue
        if last_err:
            print(f"  [fail] {pkg}: {last_err}", file=sys.stderr)
            return 1
    return 0


if __name__ == "__main__":
    args = sys.argv[1:] or ["playwright", "pyee", "greenlet"]
    sys.exit(main(args))
