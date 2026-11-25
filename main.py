#!/usr/bin/env python3
"""常规入口别名；等价于 python3 tool.py。"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from tool import main

if __name__ == "__main__":
    raise SystemExit(main())
