"""版本化的售票与到场回传台账（供新一轮接报增量登记）。

资料里的 terminal_log 已含按 ``(tranche, version)`` 扣减的出票事件；实际营运中
到场/扫码回传会晚到。本类给营运台一个可往复使用的内存登记口：

- 售票、到场都必须落到具体签发版本，不能只记限额名称；
- 已售票未到场 = 仍需预留的预计到场人数；
- 已扫码但尚未走完通道的人计入 ``in_channel``，按口径仍在出发区，不能当离场；
- 新一轮回传直接 :meth:`register` 后重新 :meth:`summary` 即可还原最新承诺。
"""

from __future__ import annotations

from collections import defaultdict


class CallbackBook:
    def __init__(self) -> None:
        self.sold: dict[tuple[str, int], int] = defaultdict(int)
        self.arrived: dict[tuple[str, int], int] = defaultdict(int)
        self.in_channel: dict[tuple[str, int], int] = defaultdict(int)
        self.records: list[dict] = []

    def register(
        self,
        kind: str,
        tranche: str,
        version: int,
        amount: int,
        *,
        in_channel: int = 0,
        ref: str | None = None,
    ) -> dict:
        key = (tranche, int(version))
        event = {"kind": kind, "tranche": tranche, "version": int(version),
                 "amount": int(amount), "in_channel": int(in_channel), "ref": ref}
        if kind in ("售票", "出票", "sale"):
            self.sold[key] += int(amount)
        elif kind in ("到场", "扫码", "arrival"):
            self.arrived[key] += int(amount)
            # in_channel 表示其中已扫码但未走完通道的人数，只做状态留痕，
            # 不重复加总（amount 是本次到场/扫码总量）。
            self.in_channel[key] = max(self.in_channel[key], int(in_channel))
        elif kind in ("离场", "exit"):
            # 走完通道/离场后释放通道内占用
            self.in_channel[key] = max(0, self.in_channel[key] - int(amount))
        else:
            raise ValueError(f"未知回传类型 {kind}")
        self.records.append(event)
        return event

    def summary(self) -> list[dict]:
        keys = sorted(set(self.sold) | set(self.arrived))
        out = []
        for key in keys:
            sold = self.sold[key]
            arrived = self.arrived[key]
            in_channel = self.in_channel[key]
            out.append({
                "tranche": key[0], "version": key[1], "key": f"{key[0]}/v{key[1]}",
                "sold": sold,
                "arrived_or_scanned": arrived,
                "not_yet_arrived": max(0, sold - arrived),
                "scanned_in_channel_counted_at_origin": in_channel,
                "note": "未到场已售票仍预留容量；通道内者仍计出发区，不视为离场",
            })
        return out

    @classmethod
    def from_terminal_log(cls, events: list[dict], day: str | None = None) -> "CallbackBook":
        """把 terminal_log 中已有的出票事件导入为版本化售票。"""
        book = cls()
        for e in events:
            if e["kind"] == "出票":
                book.register("出票", e["tranche"], int(e["version"]), int(e["amount"]),
                              ref=e["event"])
        return book
