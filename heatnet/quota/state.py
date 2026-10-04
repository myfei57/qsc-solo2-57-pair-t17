"""热量表、配额总账与跨期结转的状态快照。

跨期部分有四个结构，口径各自独立、字段一一对应，保证"结转单、分站账、审计"
说的是同一件事：

* :class:`IntakeRecord`：一次增量到账的原始流水，同时带计量时刻与到达时刻，
  归属由表型口径决定，而不是由投递方决定；
* :class:`CarryEntry`：结转条目（断流预提 / 在途迟到 / 恢复 / 前期调整），
  贯穿结算期，状态只能 ``open → closed``，不允许删除或改写；
* :class:`StationBook`：分站账对某一期的全口径分解（本期、预提、冲平、调整）；
* :class:`CarrySheet`：一张期次的结转单，随结算生成，供控制台展示与审计落盘。
"""

from __future__ import annotations

from dataclasses import dataclass, field


def _r4(value: float) -> float:
    return round(float(value), 4)


def _r2(value: float) -> float:
    return round(float(value), 2)


@dataclass(frozen=True)
class MeterSnapshot:
    """一次热量表读取的结果。"""

    name: str
    reading_mwh: float
    total_mwh: float
    wraps: int
    increments: int
    kind: str = "trade"

    def as_dict(self) -> dict:
        return {
            "name": self.name,
            "reading_mwh": _r4(self.reading_mwh),
            "total_mwh": _r4(self.total_mwh),
            "wraps": self.wraps,
            "increments": self.increments,
            "kind": self.kind,
        }


@dataclass(frozen=True)
class QuotaSnapshot:
    """一次配额总账读取的结果。"""

    total_mwh: float
    meters: int
    wraps: int
    stations: int
    period_hours: float
    open_carry: int = 0
    carry_pending_mwh: float = 0.0

    def as_dict(self) -> dict:
        return {
            "total_mwh": _r4(self.total_mwh),
            "meters": self.meters,
            "wraps": self.wraps,
            "stations": self.stations,
            "period_hours": self.period_hours,
            "open_carry": self.open_carry,
            "carry_pending_mwh": _r4(self.carry_pending_mwh),
        }


@dataclass(frozen=True)
class IntakeRecord:
    """一次增量到账流水（归属判定所需的全部时间口径都在这里）。"""

    seq: int
    meter: str
    station: str
    kind: str
    policy: str
    delta_mwh: float
    measured_at: float
    arrived_at: float
    attributed_period: int
    window_id: str = ""

    def as_dict(self) -> dict:
        return {
            "seq": self.seq,
            "meter": self.meter,
            "station": self.station,
            "kind": self.kind,
            "policy": self.policy,
            "delta_mwh": _r4(self.delta_mwh),
            "measured_at": _r4(self.measured_at),
            "arrived_at": _r4(self.arrived_at),
            "attributed_period": self.attributed_period,
            "window_id": self.window_id,
        }


@dataclass
class CarryEntry:
    """一条跨期结转记录。

    ``entry_type`` 取值：

    * ``outage_measured``：贸易表结算期断流，按预估量预提到本期，``billable`` 为真；
    * ``outage_arrival``：考核表结算期断流，只挂账跟踪、不计提前期费用；
    * ``late_measured``：贸易表迟到增量，先冲减到账期再补回计量期，净账为 0；
    * ``prior_adjustment``：迟到增量归属计量期的前期调整，与冲减配对出现。

    状态只允许在结算时由 ``open`` 变为 ``closed``，并记录关闭期与结算时落定的金额。
    """

    entry_id: str
    period: int
    station: str
    meter: str
    kind: str
    policy: str
    entry_type: str
    mwh: float
    status: str = "open"
    window_id: str = ""
    opened_at: float = 0.0
    closed_period: int = 0
    closed_at: float = 0.0
    actual_mwh: float = 0.0
    variance_mwh: float = 0.0
    charge: float = 0.0
    detail: dict = field(default_factory=dict)

    @property
    def billable(self) -> bool:
        """是否计入某期费用（断流预提与前期调整计费；冲减与考核口径挂账不计费）。"""

        return self.entry_type in ("outage_measured", "prior_adjustment")

    @property
    def is_outage(self) -> bool:
        """是否为断流挂账条目（两类表型各一种）。"""

        return self.entry_type in ("outage_measured", "outage_arrival")

    def as_dict(self) -> dict:
        return {
            "entry_id": self.entry_id,
            "period": self.period,
            "station": self.station,
            "meter": self.meter,
            "kind": self.kind,
            "policy": self.policy,
            "type": self.entry_type,
            "mwh": _r4(self.mwh),
            "status": self.status,
            "window_id": self.window_id,
            "opened_at": _r4(self.opened_at),
            "closed_period": self.closed_period,
            "closed_at": _r4(self.closed_at),
            "actual_mwh": _r4(self.actual_mwh),
            "variance_mwh": _r4(self.variance_mwh),
            "billable": self.billable,
            "charge": _r2(self.charge),
            "detail": dict(self.detail),
        }


@dataclass(frozen=True)
class StationBook:
    """分站账对某一期的全口径分解。

    各列恒等：``current + accrued + reversal + adjustment = billed``（能量口径），
    结转单上每个站的条目汇总必须能与这四个数对上。
    """

    period: int
    station: str
    current_mwh: float = 0.0
    accrued_mwh: float = 0.0
    reversal_mwh: float = 0.0
    adjustment_mwh: float = 0.0
    billed_mwh: float = 0.0
    charge: float = 0.0

    def as_dict(self) -> dict:
        return {
            "period": self.period,
            "station": self.station,
            "current_mwh": _r4(self.current_mwh),
            "accrued_mwh": _r4(self.accrued_mwh),
            "reversal_mwh": _r4(self.reversal_mwh),
            "adjustment_mwh": _r4(self.adjustment_mwh),
            "billed_mwh": _r4(self.billed_mwh),
            "charge": _r2(self.charge),
        }


@dataclass(frozen=True)
class CarrySheet:
    """一张结算期的结转单。

    结转单是该期跨期事项的唯一权威视图：新开条目（``opened``）、本期冲平的往期
    条目（``closed``，含实际量与差额）、本期账单受影响的分站账（``station_books``）。
    状态为 ``partial`` 表示存在滚动到下一期的未平条目（断流未恢复），缺的那部分
    仍是挂账，绝不当作零。
    """

    period: int
    boundary_at: float
    status: str
    opened: tuple[CarryEntry, ...] = ()
    closed: tuple[CarryEntry, ...] = ()
    rolled: tuple[CarryEntry, ...] = ()
    station_books: tuple[StationBook, ...] = ()

    def as_dict(self) -> dict:
        return {
            "period": self.period,
            "boundary_at": _r4(self.boundary_at),
            "status": self.status,
            "opened_count": len(self.opened),
            "closed_count": len(self.closed),
            "rolled_count": len(self.rolled),
            "opened": [entry.as_dict() for entry in self.opened],
            "closed": [entry.as_dict() for entry in self.closed],
            "rolled": [entry.as_dict() for entry in self.rolled],
            "station_books": [book.as_dict() for book in self.station_books],
        }
