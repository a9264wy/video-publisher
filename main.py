#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""视频号发布模块统一入口。

用法::

    python main.py scan
    python main.py publish --limit 3
    python main.py schedule
    python main.py status
    python main.py report --open

也支持作为模块运行：``python -m videopub``（见 videopub/__main__.py）。
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

# 确保无论从哪个工作目录调用，都能导入本包
ROOT = Path(__file__).resolve().parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

os.chdir(ROOT)  # 相对路径（logs/ state/ reports/）以项目根为基准

from videopub.cli import main  # noqa: E402

if __name__ == "__main__":
    sys.exit(main())
