"""供热配额与热量表。

热量表按周期累计热量，读数到顶后回绕，总账要同时给出"表读数"和"实际累计"两个
口径。多个采集任务会并发累加同一块表，累加必须是原子的：非原子的 ``+=`` 在并发
下会丢增量，总账比实际小。

跨期部分由 :mod:`heatnet.quota.carryover` 负责：归属口径按表型定死（贸易表按
计量时刻、考核表按到达时刻），结算边界前后的增量通过断流预提、迟到冲减与前期
调整结转账平，缺量部分失败时挂账滚动，绝不当零。
"""

from __future__ import annotations

from .accumulator import QuotaAccumulator
from .billing import BASE_RATE, OVERAGE_MULTIPLIER, billing_lines, charge_for
from .carryover import CarryBook, CarryError
from .meter import HeatMeter
from .policy import (
    KIND_RUN,
    KIND_TRADE,
    POLICY_ARRIVAL,
    POLICY_MEASURED,
    attributing_moment,
    policy_for,
)
from .quota import QuotaLedger
from .state import CarryEntry, CarrySheet, IntakeRecord, MeterSnapshot, QuotaSnapshot, StationBook

__all__ = [
    "BASE_RATE",
    "CarryBook",
    "CarryEntry",
    "CarryError",
    "CarrySheet",
    "HeatMeter",
    "IntakeRecord",
    "KIND_RUN",
    "KIND_TRADE",
    "MeterSnapshot",
    "OVERAGE_MULTIPLIER",
    "POLICY_ARRIVAL",
    "POLICY_MEASURED",
    "QuotaAccumulator",
    "QuotaLedger",
    "QuotaSnapshot",
    "StationBook",
    "attributing_moment",
    "billing_lines",
    "charge_for",
    "policy_for",
]
