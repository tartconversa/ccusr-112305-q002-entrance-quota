#!/usr/bin/env python3
"""营运台命令行薄封装，等价于 ``python3 -m opsdesk``。

示例：
    python3 scripts/desk.py report
    python3 scripts/desk.py round --asof 3
    python3 scripts/desk.py backtest --json
"""

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from opsdesk.cli import main  # noqa: E402

if __name__ == "__main__":
    raise SystemExit(main())
