"""跨期结转：归属规则、预提冲平与结转单。

结算到点就把本期账结掉、分站清零，但结算边界前后到达的增量不能漏在缝里。
每条增量同时携带"计量时刻"与"到达时刻"，归属规则按表型定死：

- ``heat`` 热量表：按计量时刻归当期。计量时刻落在已关闭账期的增量先挂
  预提，不进本期活账；下一期结算时冲平，作为归属期的补记行计入那次结算。
- ``flow`` 流量表：按到达时刻顺延。无论计量时刻早晚，一律计入到达时
  打开的账期，不产生预提。

结算期间采集断流（历史期有读数、本期一条都没到）属于部分失败：缺的那部分
挂成挂账项留在账上，不能当成零；补采到达时按计量时刻的归属期冲销挂账。
每次结算产出一张结转单，记录本期冲平明细、断流恢复、新挂账与结转到下期的
未了事项；结转单同时供控制台展示与审计落盘，两处读的是同一份数据。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

from ..clock import Clock
from ..errors import InterlockError, PlantError

if TYPE_CHECKING:
    from .quota import QuotaLedger

# 归属规则按表型定死：heat 按计量时刻归当期，flow 按到达时刻顺延。
ATTRIBUTION_BY_KIND = {"heat": "measured", "flow": "arrival"}


@dataclass
class AccrualEntry:
    """一条预提：计量期已关闭的迟到增量先挂账，下一期结算时冲平。"""

    accrual_id: str
    meter: str
    station: str
    home_period: int
    amount_mwh: float
    measured_at: float
    arrived_at: float
    state: str = "outstanding"
    reversed_in_period: int | None = None

    def reverse(self, period: int) -> None:
        """在本期结算时冲平这条预提。"""

        self.state = "reversed"
        self.reversed_in_period = period

    def as_dict(self) -> dict:
        """转成可落盘、可展示的字典。"""

        return {
            "accrual_id": self.accrual_id,
            "station": self.station,
            "meter": self.meter,
            "home_period": self.home_period,
            "amount_mwh": round(self.amount_mwh, 4),
            "measured_at": round(self.measured_at, 3),
            "arrived_at": round(self.arrived_at, 3),
            "state": self.state,
            "reversed_in_period": self.reversed_in_period,
        }


@dataclass
class SuspenseEntry:
    """一条挂账：结算时采集断流，缺数挂账（金额未知），补采到达后冲销。"""

    suspense_id: str
    meter: str
    station: str
    home_period: int
    reason: str
    created_at: float
    expected_mwh: float | None = None
    recovered_mwh: float = 0.0
    recovered_at: float | None = None
    state: str = "outstanding"

    def recover(self, amount_mwh: float, ts: float) -> None:
        """补采到达，按归属期冲销挂账；金额未知，只累计恢复量。"""

        self.recovered_mwh += float(amount_mwh)
        self.recovered_at = float(ts)

    def close(self) -> None:
        """恢复量已在结算单上确认，挂账结清。"""

        self.state = "recovered"

    def as_dict(self) -> dict:
        """转成可落盘、可展示的字典。"""

        return {
            "suspense_id": self.suspense_id,
            "station": self.station,
            "meter": self.meter,
            "home_period": self.home_period,
            "reason": self.reason,
            "expected_mwh": self.expected_mwh,
            "recovered_mwh": round(self.recovered_mwh, 4),
            "recovered_at": None if self.recovered_at is None else round(self.recovered_at, 3),
            "state": self.state,
            "created_at": round(self.created_at, 3),
        }


@dataclass(frozen=True)
class CarryoverSlip:
    """一张结转单：一期结算的跨期结转凭证，控制台与审计共用同一份。"""

    slip_id: str
    period: int
    boundary_ts: float
    created_at: float
    partial: bool
    stations: dict
    reversals: list
    recoveries: list
    new_suspense: list
    outstanding: list
    attribution: dict

    def as_dict(self) -> dict:
        """转成可落盘、可展示的字典。"""

        return {
            "slip_id": self.slip_id,
            "period": self.period,
            "boundary_ts": round(self.boundary_ts, 3),
            "created_at": round(self.created_at, 3),
            "partial": self.partial,
            "attribution": dict(self.attribution),
            "stations": self.stations,
            "reversals": self.reversals,
            "recoveries": self.recoveries,
            "new_suspense": self.new_suspense,
            "outstanding": self.outstanding,
        }


class CarryoverBook:
    """跨期结转台账：归属判定、预提冲平、断流挂账与结转单。"""

    def __init__(self, ledger: "QuotaLedger", clock: Clock) -> None:
        self._ledger = ledger
        self._clock = clock
        self._current_period = 1
        self._opened_at = float(clock.now())
        self._history: list[tuple[int, float, float]] = []
        self._arrivals: dict[str, int] = {}
        self._reported: set[str] = set()
        self._meter_station: dict[str, str] = {}
        self._accruals: list[AccrualEntry] = []
        self._suspense: list[SuspenseEntry] = []
        self._slips: list[CarryoverSlip] = []
        self._accrual_seq = 0
        self._suspense_seq = 0

    def book_arrival(
        self,
        meter: str,
        station: str,
        kind: str,
        delta_mwh: float,
        measured_at: float,
        arrived_at: float,
    ) -> dict:
        """给一条到达的增量定归属并入账。

        时序固定：先按表型定归属（计量时刻归当期或到达时刻顺延），再按
        归属期冲销未了挂账，最后才把剩余部分记进本期活账或挂成预提。
        """

        if kind not in ATTRIBUTION_BY_KIND:
            raise PlantError(f"未知表型: {kind!r}", component="quota")
        if measured_at > arrived_at:
            raise ValueError("计量时刻不能晚于到达时刻")
        attribution = ATTRIBUTION_BY_KIND[kind]
        home = self._home_period_for(measured_at)
        self._arrivals[meter] = self._arrivals.get(meter, 0) + 1
        self._reported.add(meter)
        self._meter_station[meter] = station

        recovered = self._recover_suspense(meter, home, delta_mwh, arrived_at)
        decision = {
            "booking": "booked",
            "attribution": attribution,
            "home_period": home,
            "booked_period": self._current_period,
            "deferred": False,
            "accrual": None,
            "recovered": recovered,
        }
        if attribution == "measured" and home < self._current_period:
            self._accrual_seq += 1
            entry = AccrualEntry(
                accrual_id=f"acc-{self._accrual_seq:04d}",
                meter=meter,
                station=station,
                home_period=home,
                amount_mwh=float(delta_mwh),
                measured_at=float(measured_at),
                arrived_at=float(arrived_at),
            )
            self._accruals.append(entry)
            decision.update(booking="accrued", booked_period=None, accrual=entry.accrual_id)
            return decision
        self._ledger.allocate(station, delta_mwh)
        if home < self._current_period:
            decision["deferred"] = True
        return decision

    def close_period(self, period: int, boundary_ts: float) -> CarryoverSlip:
        """关闭当前账期并产出结转单。

        时序固定：先冲平未了预提（变成本期结算的补记），再确认断流恢复，
        然后给本期断流的表挂账，最后出结转单并推进期号。期号必须连续、
        边界不能早于本期开场，乱序直接拒绝。
        """

        if period != self._current_period:
            raise InterlockError(
                f"结算期号乱序: 期望第 {self._current_period} 期，收到第 {period} 期",
                component="quota",
            )
        if boundary_ts < self._opened_at:
            raise InterlockError("结算边界早于本期开场", component="quota")

        reversals: list[dict] = []
        for entry in self._accruals:
            if entry.state != "outstanding":
                continue
            entry.reverse(period)
            reversals.append(entry.as_dict())

        recoveries: list[dict] = []
        for entry in self._suspense:
            if entry.state == "outstanding" and entry.recovered_mwh > 0.0:
                entry.close()
                recoveries.append(entry.as_dict())

        new_suspense: list[dict] = []
        for meter in sorted(self._reported):
            if self._arrivals.get(meter, 0) > 0:
                continue
            self._suspense_seq += 1
            entry = SuspenseEntry(
                suspense_id=f"sus-{self._suspense_seq:04d}",
                meter=meter,
                station=self._meter_station.get(meter, meter),
                home_period=period,
                reason="collection-outage",
                created_at=float(boundary_ts),
            )
            self._suspense.append(entry)
            new_suspense.append(entry.as_dict())

        stations: dict[str, dict] = {}
        for name in self._ledger.stations():
            stations[name] = {
                "booked_mwh": round(self._ledger.allocated_mwh(name), 4),
                "correction_mwh": 0.0,
                "new_suspense": False,
            }
        for item in reversals + new_suspense:
            slot = stations.setdefault(
                item["station"],
                {"booked_mwh": 0.0, "correction_mwh": 0.0, "new_suspense": False},
            )
            if "amount_mwh" in item:
                slot["correction_mwh"] = round(slot["correction_mwh"] + item["amount_mwh"], 4)
            else:
                slot["new_suspense"] = True

        slip = CarryoverSlip(
            slip_id=f"carry-{period:04d}",
            period=period,
            boundary_ts=float(boundary_ts),
            created_at=float(self._clock.now()),
            partial=bool(new_suspense),
            stations=stations,
            reversals=reversals,
            recoveries=recoveries,
            new_suspense=new_suspense,
            outstanding=[
                entry.as_dict() for entry in self._suspense if entry.state == "outstanding"
            ],
            attribution=dict(ATTRIBUTION_BY_KIND),
        )
        self._slips.append(slip)
        self._history.append((period, self._opened_at, float(boundary_ts)))
        self._arrivals = {}
        self._current_period += 1
        self._opened_at = float(boundary_ts)
        return slip

    def describe(self) -> dict:
        """汇总结转台账，供控制台展示与配额报告读取。"""

        return {
            "current_period": self._current_period,
            "opened_at": round(self._opened_at, 3),
            "attribution": dict(ATTRIBUTION_BY_KIND),
            "outstanding": [
                entry.as_dict() for entry in self._suspense if entry.state == "outstanding"
            ],
            "slips": [slip.as_dict() for slip in self._slips],
        }

    def _home_period_for(self, measured_at: float) -> int:
        """按计量时刻找归属期；早于账期起点的归到第一期。"""

        if measured_at >= self._opened_at:
            return self._current_period
        for period, opened, closed in reversed(self._history):
            if opened <= measured_at < closed:
                return period
        return 1

    def _recover_suspense(self, meter: str, home: int, amount: float, ts: float) -> list[str]:
        """补采按计量时刻的归属期冲销挂账，返回被冲销的挂账号。"""

        recovered: list[str] = []
        for entry in self._suspense:
            if (
                entry.state == "outstanding"
                and entry.meter == meter
                and entry.home_period == home
            ):
                entry.recover(amount, ts)
                recovered.append(entry.suspense_id)
        return recovered
