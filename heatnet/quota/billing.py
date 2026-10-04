"""配额结算行。

结算按站给出本期热量与折算费用。费用口径以分站账的"计费量"为准：

    计费量 = 本期到账 + 断流预提 + 冲减 + 前期调整

因此跨期事项的钱与结转单完全对得上：贸易表断流先按预估量预提费用，恢复后
差额以前期调整回到断流期费率口径；迟到量在到账期先冲减、再以计量期费率补提，
两笔净额为零，不重开往期账单。费率固定为基准费率，超配额部分按倍率计费。
"""

from __future__ import annotations

from .carryover import CarryBook

BASE_RATE = 32.0
OVERAGE_MULTIPLIER = 1.6


def charge_for(mwh: float, quota_mwh: float) -> float:
    """按配额折算费用（配额内基准费率，超出部分按倍率）。

    计费量为负时（极端情况下往期冲减大于本期到账）按零收费，负差仍保留在
    分站账里滚动核对，不用计费把差额"抹平"。
    """

    cap = max(0.0, float(quota_mwh))
    billable = max(0.0, float(mwh))
    within = min(billable, cap)
    overage = max(0.0, billable - cap)
    return within * BASE_RATE + overage * BASE_RATE * OVERAGE_MULTIPLIER


def billing_lines(book: CarryBook, quota_mwh: float, period: int | None = None) -> list[dict]:
    """按站生成某期结算行（含超配额折算与跨期四列分解）。

    期次取当前期时，分站账视图已包含已申报未结算的贸易表断流预提，因此
    结算前预览与结算后出账用同一口径，不会出现"预览少一块、结算多一块"。
    """

    target = book.period() if period is None else period
    columns_by_station = book.period_view(target)
    lines: list[dict] = []
    for station in sorted(columns_by_station):
        columns = columns_by_station[station]
        current = columns.get("current", 0.0)
        accrued = columns.get("accrued", 0.0)
        reversal = columns.get("reversal", 0.0)
        adjustment = columns.get("adjustment", 0.0)
        used = current + accrued + reversal + adjustment
        cap = max(0.0, float(quota_mwh))
        charge = charge_for(used, cap)
        lines.append(
            {
                "station": station,
                "period": target,
                "mwh": round(used, 4),
                "current_mwh": round(current, 4),
                "accrued_mwh": round(accrued, 4),
                "reversal_mwh": round(reversal, 4),
                "adjustment_mwh": round(adjustment, 4),
                "quota_mwh": round(cap, 4),
                "overage_mwh": round(max(0.0, used - cap), 4),
                "charge": round(charge, 2),
            }
        )
    return lines
