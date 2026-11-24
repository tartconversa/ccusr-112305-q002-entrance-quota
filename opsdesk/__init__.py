"""雪场营运台：分区人数推断、口间流量对账、容量放号与终端承诺核算。

只依赖 Python 标准库。常用入口：

- :class:`opsdesk.desk.OperationsDesk`：往复使用的营运台门面
- :func:`opsdesk.inference.infer_day`：某日某 as-of 时点的区间推断
- :func:`opsdesk.backtest.run_backtest`：独立收尾点验历史回测
- :class:`opsdesk.terminals.TerminalLedger`：版本化限额/终端承诺台账
"""

from .bounds import Bounds
from .io import Workspace, load_workspace, read_table
from .inference import DayInference, infer_day

__all__ = [
    "Bounds",
    "Workspace",
    "load_workspace",
    "read_table",
    "DayInference",
    "infer_day",
    "OperationsDesk",
    "TerminalLedger",
    "GroupBook",
    "CapacityBook",
    "run_backtest",
]

__version__ = "1.0.0"


def __getattr__(name):
    # 较重的门面/子系统按需导入，避免循环依赖与无谓启动开销。
    if name == "OperationsDesk":
        from .desk import OperationsDesk
        return OperationsDesk
    if name == "TerminalLedger":
        from .terminals import TerminalLedger
        return TerminalLedger
    if name == "GroupBook":
        from .groups import GroupBook
        return GroupBook
    if name == "CapacityBook":
        from .capacity import CapacityBook
        return CapacityBook
    if name == "run_backtest":
        from .backtest import run_backtest
        return run_backtest
    raise AttributeError(f"module 'opsdesk' has no attribute {name!r}")
