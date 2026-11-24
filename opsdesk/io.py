"""读取 data/ 下的 JSON 营运资料。

保持与 ``scripts/inspect_sources.py`` 相同的表格式约定：每个来源是一个
JSON 对象数组。路径可注入，便于测试与复用不同经营日的资料副本。
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_DATA_DIR = REPO_ROOT / "data"

TABLE_NAMES = (
    "gates",
    "counts",
    "route_map",
    "closures",
    "occupancy_limits",
    "ticket_tranches",
    "terminal_log",
    "group_tickets",
)


def read_table(name: str, data_dir: str | Path | None = None) -> list[dict[str, Any]]:
    """读取 ``<data_dir>/<name>.json`` 为对象数组。"""
    directory = Path(data_dir) if data_dir is not None else DEFAULT_DATA_DIR
    path = directory / f"{name}.json"
    with path.open(encoding="utf-8") as stream:
        records = json.load(stream)
    if not isinstance(records, list) or not all(isinstance(r, dict) for r in records):
        raise ValueError(f"{path} 应为 JSON 对象数组")
    return records


@dataclass
class Workspace:
    """整套营运资料的内存视图。"""

    data_dir: Path
    gates: list[dict]
    counts: list[dict]
    route_map: list[dict]
    closures: list[dict]
    occupancy_limits: list[dict]
    ticket_tranches: list[dict]
    terminal_log: list[dict]
    group_tickets: list[dict]
    _tables: dict[str, list[dict]] = field(default_factory=dict, repr=False)

    def table(self, name: str) -> list[dict]:
        return self._tables[name]

    @property
    def days(self) -> list[str]:
        return sorted({r["day"] for r in self.gates})

    def require_day(self, day: str) -> str:
        days = self.days
        if day not in days:
            raise KeyError(f"无此经营日 {day}；可选：{', '.join(days)}")
        return day


def load_workspace(data_dir: str | Path | None = None) -> Workspace:
    directory = Path(data_dir) if data_dir is not None else DEFAULT_DATA_DIR
    tables = {name: read_table(name, directory) for name in TABLE_NAMES}
    return Workspace(data_dir=directory, _tables=tables, **tables)  # type: ignore[arg-type]
