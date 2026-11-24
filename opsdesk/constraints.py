"""极简线性区间约束传播器。

资料给出的都是区间或等式约束（闸机累计差、点验上下界、区域平衡式）。
在变量区间上反复做 boundary consistency 收紧直到不动点：

    lo <= sum(coef_i * x_i) <= hi

得到的每个区间都*包含*所有联合可行解，因此作为“推定上界”不会低估；
资料把未知量钉死时区间退化为单点。不依赖 scipy / z3。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Hashable

from .bounds import Bounds, INF


def ceil_div(a: int, b: int) -> int:
    """b>0 时的向上取整除法。"""
    return -((-a) // b)


@dataclass
class Constraint:
    terms: dict[Hashable, int]
    lo: int
    hi: int
    note: str = ""

    def __post_init__(self):
        if self.lo > self.hi:
            raise ValueError(f"约束下界大于上界：{self.note}")


@dataclass
class PropagationEngine:
    bounds: dict[Hashable, Bounds] = field(default_factory=dict)
    constraints: list[Constraint] = field(default_factory=list)
    conflicts: list[dict] = field(default_factory=list)
    iterations: int = 0

    def var(self, key: Hashable, initial: Bounds | None = None) -> Bounds:
        if key not in self.bounds:
            self.bounds[key] = initial or Bounds(0, INF)
        return self.bounds[key]

    def set(self, key: Hashable, bounds: Bounds) -> None:
        self.bounds[key] = bounds

    def tighten(self, key: Hashable, bounds: Bounds, source: str = "") -> bool:
        current = self.var(key)
        merged = current.intersection(bounds)
        if merged is None:
            self.conflicts.append(
                {"variable": list(key) if isinstance(key, tuple) else str(key),
                 "current": current.to_dict(), "incoming": bounds.to_dict(), "source": source}
            )
            return False
        if merged != current:
            self.bounds[key] = merged
            return True
        return False

    def add(self, constraint: Constraint) -> None:
        self.constraints.append(constraint)

    def _tighten_from_constraint(self, con: Constraint) -> bool:
        changed = False
        for target, coef in con.terms.items():
            other_lo = 0
            other_hi = 0
            for key, c in con.terms.items():
                if key == target:
                    continue
                b = self.bounds[key]
                if c >= 0:
                    other_lo += c * b.lo
                    other_hi += c * b.hi
                else:
                    other_lo += c * b.hi
                    other_hi += c * b.lo
            # coef*x_target 应落在 [con.lo-other_hi, con.hi-other_lo]
            y_lo = con.lo - other_hi
            y_hi = con.hi - other_lo
            if coef > 0:
                new_lo = ceil_div(y_lo, coef)
                new_hi = y_hi // coef
            else:
                a = -coef
                # y = -a*x ∈ [y_lo, y_hi]  =>  x ∈ [ceil(-y_hi/a), floor(-y_lo/a)]
                new_lo = ceil_div(-y_hi, a)
                new_hi = (-y_lo) // a
            incoming = Bounds(new_lo, new_hi)
            if self.tighten(target, incoming, con.note):
                changed = True
        return changed

    def propagate(self, max_rounds: int = 200) -> None:
        for i in range(1, max_rounds + 1):
            self.iterations = i
            changed = False
            for con in self.constraints:
                if self._tighten_from_constraint(con):
                    changed = True
            if not changed:
                return
        # 达到最大轮数仍在变化在本项目规模下不应发生；保留宽界仍是可靠的。
