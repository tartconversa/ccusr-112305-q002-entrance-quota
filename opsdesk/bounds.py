"""整数上下界与区间运算。

营运资料里的点验值、名额、人数都是整数；推断结论以闭区间 :class:`Bounds`
表示。区间始终包含所有仍可能成立的真实值（可靠性优先），在资料把数值钉死时
退化为单点。
"""

from __future__ import annotations

from dataclasses import dataclass

INF = 10**9


@dataclass(frozen=True)
class Bounds:
    """闭整数区间 [lo, hi]。"""

    lo: int
    hi: int

    def __post_init__(self) -> None:
        if self.lo is None or self.hi is None:
            raise ValueError("上下界不可为 None")
        if self.lo > self.hi:
            raise ValueError(f"空区间：[{self.lo}, {self.hi}]")

    @staticmethod
    def exact(value: int) -> "Bounds":
        return Bounds(int(value), int(value))

    @staticmethod
    def open_range(nonnegative: bool = True) -> "Bounds":
        return Bounds(0 if nonnegative else -INF, INF)

    @property
    def exact_value(self):
        return self.lo if self.lo == self.hi else None

    @property
    def width(self) -> int:
        return self.hi - self.lo

    @property
    def pinned(self) -> bool:
        return self.lo == self.hi

    def contains(self, value: int) -> bool:
        return self.lo <= value <= self.hi

    def intersect(self, other: "Bounds") -> "Bounds":
        lo = max(self.lo, other.lo)
        hi = min(self.hi, other.hi)
        if lo > hi:
            raise InfeasibleError(f"区间冲突：{self} ∩ {other} 为空")
        return Bounds(lo, hi)

    def intersection(self, other: "Bounds") -> "Bounds | None":
        """不抛异常的交集；无交集返回 None。"""
        lo = max(self.lo, other.lo)
        hi = min(self.hi, other.hi)
        return Bounds(lo, hi) if lo <= hi else None

    def add(self, other: "Bounds") -> "Bounds":
        return Bounds(self.lo + other.lo, self.hi + other.hi)

    def sub(self, other: "Bounds") -> "Bounds":
        return Bounds(self.lo - other.hi, self.hi - other.lo)

    def scale(self, coefficient: int) -> "Bounds":
        if coefficient >= 0:
            return Bounds(coefficient * self.lo, coefficient * self.hi)
        return Bounds(coefficient * self.hi, coefficient * self.lo)

    def clip_nonnegative(self) -> "Bounds":
        lo = max(self.lo, 0)
        if lo > self.hi:
            raise InfeasibleError(f"区间在非负约束下为空：{self}")
        return Bounds(lo, self.hi)

    def to_dict(self) -> dict:
        return {"low": self.lo, "high": self.hi, "exact": self.pinned}

    def __str__(self) -> str:  # pragma: no cover - 展示用
        return str(self.lo) if self.pinned else f"[{self.lo},{self.hi}]"


class InfeasibleError(ValueError):
    """多项资料无法同时成立。"""
