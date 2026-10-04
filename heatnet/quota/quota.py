"""供热配额总账。

配额按结算周期把累计热量分到各换热站，周期结束时结清。总账分两层：

* **本期分配**：``站 → 热量``，供结算前预览与历史调用使用；
* **跨期结转**：:class:`~heatnet.quota.carryover.CarryBook` 保存采集流水、
  断流预提、迟到冲减与前期调整，结算时按"先冲平往期、再结本期、再出单"
  的固定时序滚动。

采集与结算共用同一把锁、同一份结转账册，避免"采集端丢一次、总账看不出来"。
归属口径（计量时刻 / 到达时刻）由表型在表登记时定死，到账路径不允许选边。
"""

from __future__ import annotations

from ..config import QuotaLimits
from .accumulator import QuotaAccumulator
from .carryover import CarryBook
from .state import QuotaSnapshot


class QuotaLedger:
    """供热配额总账。"""

    def __init__(self, limits: QuotaLimits, accumulator: QuotaAccumulator) -> None:
        self._limits = limits
        self._accumulator = accumulator
        self._carry = CarryBook()
        self._quota_mwh = limits.meter_wrap_mwh / 40.0

    @property
    def limits(self) -> QuotaLimits:
        return self._limits

    @property
    def accumulator(self) -> QuotaAccumulator:
        return self._accumulator

    @property
    def carry(self) -> CarryBook:
        return self._carry

    @property
    def quota_mwh(self) -> float:
        """返回当前期分站配额（费用折算口径）。"""

        return self._quota_mwh

    def set_quota_mwh(self, quota_mwh: float) -> None:
        """覆盖分站配额。"""

        self._quota_mwh = max(0.0, float(quota_mwh))

    def register_station_meter(self, meter: str, station: str, kind: str) -> None:
        """登记表 → 站归属与表型（装配期定死，跨期归属据此判定）。"""

        self._carry.register_meter(meter, station, kind)

    def reset_carry(self) -> None:
        """清空跨期结转账册并按现有热量表重新登记（交付标定/演示基线用）。"""

        fresh = CarryBook()
        for name in self._accumulator.meters():
            fresh.register_meter(name, *self._carry.meter_info(name)[:2])
        self._carry = fresh

    def allocate(self, station: str, mwh: float) -> float:
        """把一个站的无表补录量记进本期总账（直接进分站账本期列），返回累计值。"""

        value = max(0.0, float(mwh))
        self._carry.credit_arrival(station, value)
        columns = self._carry.period_view(self._carry.period()).get(station, {})
        return columns.get("current", 0.0)

    def stations(self) -> list[str]:
        """返回本期有到账（或补录）记录的站名（排序后）。"""

        return sorted(self._carry.period_view(self._carry.period()))

    def periods(self) -> int:
        """返回累计结算周期数（以结转账册已落定的期次为准）。"""

        return self._carry.periods_settled()

    def total_mwh(self) -> float:
        """返回本期分站账能量合计（到账 + 已申报预提 + 冲减 + 前期调整）。"""

        return sum(
            columns.get("current", 0.0)
            + columns.get("accrued", 0.0)
            + columns.get("reversal", 0.0)
            + columns.get("adjustment", 0.0)
            for columns in self._carry.period_view(self._carry.period()).values()
        )

    def settle(self, boundary_at: float | None = None, quota_mwh: float | None = None) -> dict:
        """结束本期：先冲平往期、再结本期，返回带结转单的结算摘要。"""

        from .billing import charge_for

        if quota_mwh is not None:
            self.set_quota_mwh(quota_mwh)
        cap = self._quota_mwh
        boundary = self._default_boundary() if boundary_at is None else float(boundary_at)

        def rate_for(period: int, _station: str, billed_mwh: float) -> float:
            # 配额按站按期复用同一口径；前期调整用计量期费率，避免被到账期
            # 的超配额倍率污染（基准费率口径下倍率一致，差额仍然落账可查）。
            return charge_for(billed_mwh, cap)

        outcome = self._carry.settle(boundary, rate_for)
        sheet = outcome["sheet"]
        lines = [
            {
                "station": book.station,
                "period": book.period,
                "mwh": round(book.billed_mwh, 4),
                "current_mwh": round(book.current_mwh, 4),
                "accrued_mwh": round(book.accrued_mwh, 4),
                "reversal_mwh": round(book.reversal_mwh, 4),
                "adjustment_mwh": round(book.adjustment_mwh, 4),
                "quota_mwh": round(cap, 4),
                "overage_mwh": round(max(0.0, book.billed_mwh - cap), 4),
                "charge": round(book.charge, 2),
            }
            for book in sheet.station_books
        ]
        total_mwh = sum(book.billed_mwh for book in sheet.station_books)
        payload = {
            "period": outcome["period"],
            "hours": self._limits.period_hours,
            "boundary_at": outcome["boundary_at"],
            "status": outcome["status"],
            "total_mwh": round(total_mwh, 4),
            "stations": {line["station"]: line["mwh"] for line in lines},
            "lines": lines,
            "carry_sheet": sheet.as_dict(),
            "amendments": outcome["amendments"],
            "reconcile": self._carry.reconcile(),
        }
        return payload

    def _default_boundary(self) -> float:
        """未显式给出边界时，按周期小时数从固定起点推算（复现/测试用）。"""

        period_seconds = self._limits.period_hours * 3600.0
        return 1_758_000_000.0 + self._carry.period() * period_seconds

    def snapshot(self) -> QuotaSnapshot:
        """返回配额总账状态快照。"""

        return QuotaSnapshot(
            total_mwh=self.total_mwh(),
            meters=len(self._accumulator.meters()),
            wraps=self._accumulator.wraps(),
            stations=len(self.stations()),
            period_hours=self._limits.period_hours,
            open_carry=len(self._carry.open_entries()),
            carry_pending_mwh=self._carry.open_carry_mwh(),
        )
