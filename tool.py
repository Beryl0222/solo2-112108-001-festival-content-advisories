#!/usr/bin/env python3
"""仓库根目录便捷入口：等价于 python3 -m shortwarn。

同时 re-export 常用 API，方便 ``from tool import analyse_programme``。
"""
import sys
from pathlib import Path

# python -I 会启用安全路径；显式加入仓库根目录以找到本项目包。
PROJECT_ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(PROJECT_ROOT))

from shortwarn.disclosure import render_advisory  # noqa: E402
from shortwarn.engine import analyse_all, analyse_programme, event_clearance  # noqa: E402
from shortwarn.ledger import Ledger  # noqa: E402
from shortwarn.publishing import trace_issuance  # noqa: E402
from shortwarn.workspace import Workspace, load_workspace  # noqa: E402
from shortwarn.cli import main  # noqa: E402

__all__ = [
    "Workspace",
    "load_workspace",
    "Ledger",
    "analyse_programme",
    "analyse_all",
    "event_clearance",
    "render_advisory",
    "trace_issuance",
    "main",
]

if __name__ == "__main__":
    raise SystemExit(main())
