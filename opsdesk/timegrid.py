"""当地经营时段编号。

每个经营日从当地时间 8:00 起按半小时编号：窗口 0 = 08:00–08:30，
窗口 w 覆盖 [8:00 + 30w 分钟, 再半小时)。窗口 w 的开始时点记为状态 N(w)。
"""

from __future__ import annotations

from datetime import datetime, timedelta

SLOT_MINUTES = 30
DAY_START_HOUR = 8
# 资料中出现的观察窗口为 0..8；保留 9 个状态点（w=0..8）与 8 个可由相邻
# 累计读数求增量的穿越窗口（w=1..8），窗口 0 的增量没有前值，需靠开场点验锚定。
N_SLOTS = 9
MAX_WINDOW = 8


def window_clock(window: int) -> tuple[str, str]:
    start = datetime(2000, 1, 1, DAY_START_HOUR) + timedelta(minutes=SLOT_MINUTES * window)
    end = start + timedelta(minutes=SLOT_MINUTES)
    return start.strftime("%H:%M"), end.strftime("%H:%M")


def window_label(window: int) -> str:
    start, end = window_clock(window)
    return f"W{window}({start}-{end})"


def state_label(window: int) -> str:
    start, _ = window_clock(window)
    return f"N{window}@{start}"
