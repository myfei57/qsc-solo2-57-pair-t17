"""跨期结转账册。

结转账册是配额总账里"不归零、要跨期"的那部分状态，与本期分站分配分开存放，
规则与 :mod:`heatnet.quota.policy` 一一对应：

1. **归属定死**：每次到账的增量同时记录计量时刻与到达时刻，归属期由表型口径
    唯一判定，调用方不能选边；
2. **时序定死**：结算先冲平往期（断流恢复冲预提、迟到先冲本期），再结本期
   （新开预提、出具账单），顺序不可交换；
3. **断流是部分失败**：结算期采集断流必须由站点按预估量挂账（预提额必须为正，
   禁止挂零），恢复补传后冲平；到期仍未恢复的条目滚动到下一期，结转单置为
   ``partial``，缺量始终可见；
4. **迟到先冲再补**：贸易表（计量时刻口径）的迟到增量到账即挂条目，结算时在
   到账期冲减、以"前期调整"补回计量期，两笔配对、净额为零，不重开往期账单；
5. **三处一致**：结转单条目、分站账各列、审计事件由同一份状态产出，
   :meth:`CarryBook.reconcile` 随时可校验恒等式。

账册只算能量（MWh）与台账；费用在 :mod:`heatnet.quota.billing` 按费率折算，
结算时由总账把费率传进来落账。
"""

from __future__ import annotations

import threading
import uuid
from collections import defaultdict

from . import policy
from .state import CarryEntry, CarrySheet, IntakeRecord, StationBook

# 条目类型
OUTAGE_MEASURED = "outage_measured"
OUTAGE_ARRIVAL = "outage_arrival"
LATE_MEASURED = "late_measured"
PRIOR_ADJUSTMENT = "prior_adjustment"

STATUS_OPEN = "open"
STATUS_CLOSED = "closed"

# 结转单状态
SHEET_COMPLETE = "complete"
SHEET_PARTIAL = "partial"

_EPS = 1e-9


def _entry_id() -> str:
    return f"carry-{uuid.uuid4().hex[:12]}"


class CarryError(ValueError):
    """结转登记不满足前置条件（重复窗口、预提为零、时刻倒挂等）。"""


class CarryBook:
    """跨期结转账册：采集流水、结转条目、分站账、结转单。"""

    def __init__(self) -> None:
        self._lock = threading.RLock()
        # 表登记：meter -> (station, kind, 归属口径)
        self._meters: dict[str, tuple[str, str, str]] = {}
        # 期次：当前期从 1 开始；_boundaries[p] = 第 p 期结算边界（1 基）
        self._period = 1
        self._boundaries: list[float] = []
        self._period_start_at: float | None = None
        self._seq = 0
        self._intake_total: dict[str, float] = defaultdict(float)
        self._intake_count = 0
        # 无表的站级手工补录（不走 intake 流水），独立计能量守恒。
        self._direct_total: dict[str, float] = defaultdict(float)
        self._entries: list[CarryEntry] = []
        # (期, 站) -> 各列累计
        self._books: dict[tuple[int, str], dict[str, float]] = {}
        self._sheets: list[CarrySheet] = []

    # ---- 表登记 -------------------------------------------------------

    def register_meter(self, meter: str, station: str, kind: str) -> None:
        """登记一块表的归属信息（站名与表型在装配期定死）。"""

        normalized = policy.normalize_kind(kind)
        with self._lock:
            if meter in self._meters:
                raise CarryError(f"热量表已登记归属: {meter}")
            self._meters[meter] = (station, normalized, policy.policy_for(normalized))

    def meter_info(self, meter: str) -> tuple[str, str, str]:
        """返回 (站名, 表型, 归属口径)。"""

        with self._lock:
            try:
                return self._meters[meter]
            except KeyError:
                raise CarryError(f"未登记归属的热量表: {meter}") from None

    # ---- 站级直接入账 -------------------------------------------------

    def credit_arrival(self, station: str, mwh: float) -> None:
        """把一笔归属当期的到账记入分站账本期列。

        :meth:`intake` 只记流水与跨期挂账；归属判定为"当期"的能量（正常
        到账、考核表全部到账）由调用方（总账）通过本方法入账，保证本期
        ``current`` 只有一个写入来源。
        """

        value = max(0.0, float(mwh))
        with self._lock:
            self._direct_total[station] += value
            self._add_book_column(self._period, station, "current", value)

    def _open_outage(self, meter: str) -> CarryEntry | None:
        for entry in self._entries:
            if entry.meter == meter and entry.is_outage and entry.status == STATUS_OPEN:
                return entry
        return None

    # ---- 增量到账 -----------------------------------------------------

    def intake(
        self,
        meter: str,
        delta_mwh: float,
        measured_at: float,
        arrived_at: float,
        window_id: str = "",
    ) -> IntakeRecord:
        """登记一笔增量到账，按表型口径确定归属，返回流水记录。

        计量时刻晚于到达时刻直接拒绝（倒挂的时间戳无法归属）；带 ``window_id``
        的到账视为该断流窗口的恢复补传，按对应口径冲预提或顺延入账。
        """

        delta = float(delta_mwh)
        if delta < 0:
            raise CarryError("热量增量不能为负")
        if measured_at > arrived_at + _EPS:
            raise CarryError("计量时刻不能晚于到达时刻")
        station, kind, meter_policy = self.meter_info(meter)
        with self._lock:
            self._seq += 1
            seq = self._seq
            self._intake_total[meter] += delta
            self._intake_count += 1

            window = str(window_id or "").strip()
            outage = self._open_outage(meter) if window else None
            if outage is not None and window and outage.window_id != window:
                raise CarryError(
                    f"表 {meter} 存在未关闭断流窗口 {outage.window_id}，不能按其他窗口补传"
                )

            attributed = self._period
            # 到账在先：无论最终归属哪期，能量先进到账期 current，结算时再按
            # 表型重分类（贸易表把归属往期的部分冲减并前期调整；考核表留在
            # 到账期）。这样本期 current 只有"到达"这一个写入来源。
            self._add_book_column(self._period, station, "current", delta)
            if outage is not None:
                # 恢复补传：累计到窗口，结算时按表型重分类。
                outage.actual_mwh += delta
                outage.detail.setdefault("recoveries", []).append(
                    {"seq": seq, "mwh": round(delta, 4), "arrived_at": round(arrived_at, 3)}
                )
                if meter_policy == policy.POLICY_MEASURED:
                    attributed = outage.period
            elif meter_policy == policy.POLICY_MEASURED:
                origin = self._period_of(measured_at)
                if origin != self._period:
                    # 权责发生制的迟到量：归属计量期；挂条目，结算时在到账期
                    # 冲减、再以"前期调整"补回计量期。
                    attributed = origin
                    entry = CarryEntry(
                        entry_id=_entry_id(),
                        period=self._period,
                        station=station,
                        meter=meter,
                        kind=kind,
                        policy=meter_policy,
                        entry_type=LATE_MEASURED,
                        mwh=delta,
                        window_id=window,
                        opened_at=arrived_at,
                        detail={
                            "measured_at": round(measured_at, 3),
                            "origin_period": origin,
                            "seq": seq,
                        },
                    )
                    self._entries.append(entry)
            # 考核表按到达时刻：attributed 保持为到账当期，无需挂账。
            return IntakeRecord(
                seq=seq,
                meter=meter,
                station=station,
                kind=kind,
                policy=meter_policy,
                delta_mwh=delta,
                measured_at=measured_at,
                arrived_at=arrived_at,
                attributed_period=attributed,
                window_id=window,
            )

    def declare_outage(
        self,
        meter: str,
        estimated_mwh: float,
        declared_at: float,
        *,
        started_at: float | None = None,
        window_id: str = "",
        note: str = "",
    ) -> CarryEntry:
        """申报结算期采集断流，按预估量挂账（部分失败，缺量不得当零）。

        贸易表挂 ``outage_measured`` 预提，结算时计入本期费用；考核表挂
        ``outage_arrival``，只跟踪缺口、不提前计费，恢复补传顺延到到账期。
        预估量必须为正——把缺口估成零等于把断流当零，正是要杜绝的口径。
        """

        estimate = float(estimated_mwh)
        if estimate <= 0:
            raise CarryError("断流预提量必须为正：缺的部分要挂账，不能当成零")
        station, kind, meter_policy = self.meter_info(meter)
        start = declared_at if started_at is None else float(started_at)
        if start > declared_at + _EPS:
            raise CarryError("断流开始时刻不能晚于申报时刻")
        window = str(window_id or "").strip() or f"win-{uuid.uuid4().hex[:10]}"
        with self._lock:
            if self._open_outage(meter) is not None:
                raise CarryError(f"表 {meter} 已有未关闭的断流窗口")
            for entry in self._entries:
                if entry.window_id == window and entry.status == STATUS_OPEN:
                    raise CarryError(f"断流窗口标识重复: {window}")
            if self._boundaries and start <= self._boundaries[-1] + _EPS:
                raise CarryError("断流申报必须针对当前结算期，不能补报已结算期")
            entry = CarryEntry(
                entry_id=_entry_id(),
                period=self._period,
                station=station,
                meter=meter,
                kind=kind,
                policy=meter_policy,
                entry_type=OUTAGE_MEASURED if meter_policy == policy.POLICY_MEASURED else OUTAGE_ARRIVAL,
                mwh=estimate,
                window_id=window,
                opened_at=declared_at,
                detail={"started_at": round(start, 3), "note": str(note), "estimated_mwh": estimate},
            )
            self._entries.append(entry)
            return entry

    # ---- 分站账列 -----------------------------------------------------

    def _add_book_column(self, period: int, station: str, column: str, value: float) -> None:
        book = self._books.setdefault(
            (period, station),
            {"current": 0.0, "accrued": 0.0, "reversal": 0.0, "adjustment": 0.0},
        )
        book[column] += value

    def _period_of(self, moment: float) -> int:
        """按已落定的结算边界把时刻归到某一期。"""

        for index, boundary in enumerate(self._boundaries, start=1):
            if moment <= boundary + _EPS:
                return index
        return self._period

    def period_view(self, period: int) -> dict[str, dict[str, float]]:
        """返回某期各站四列能量（未结算期包含挂账中的预提视角）。"""

        with self._lock:
            view: dict[str, dict[str, float]] = {}
            for (p, station), columns in self._books.items():
                if p != period:
                    continue
                view[station] = {key: value for key, value in columns.items()}
            # 只有"当前期"已申报未结算的贸易表断流才计入预览预提列；往期条目
            # 在往期结算时已经把预提落进 accrued，滚动条目不能二次计入往期视图。
            if period == self._period:
                for entry in self._entries:
                    if (
                        entry.period == period
                        and entry.status == STATUS_OPEN
                        and entry.entry_type == OUTAGE_MEASURED
                    ):
                        row = view.setdefault(
                            entry.station,
                            {"current": 0.0, "accrued": 0.0, "reversal": 0.0, "adjustment": 0.0},
                        )
                        row["accrued"] += entry.mwh
            return view

    # ---- 结算 ---------------------------------------------------------

    def settle(
        self,
        boundary_at: float,
        rate_for,
    ) -> dict:
        """结算当前期。严格按"先冲平往期，再结本期，再出单"的时序执行。

        ``rate_for(period, station, billed_mwh)`` 由总账提供，按期次与站折算费用，
        保证前期调整用计量期费率口径，不被到账期的超配额倍率污染。
        """

        with self._lock:
            period = self._period
            if self._boundaries and boundary_at < self._boundaries[-1] - _EPS:
                raise CarryError("结算边界不能早于上一期边界")

            opened: list[CarryEntry] = []
            closed: list[CarryEntry] = []
            rolled: list[CarryEntry] = []
            # 本次结算对往期分站账的追加调整（结转单与分站账对账用）
            amendments: list[tuple[int, str, float, float]] = []

            # 时序 1：冲平往期遗留条目。
            for entry in list(self._entries):
                if entry.status != STATUS_OPEN or entry.period >= period:
                    continue
                self._close_carry(
                    entry, period, boundary_at, closed, amendments, rate_for
                )
                if entry.status == STATUS_OPEN:
                    rolled.append(entry)

            # 时序 2：结本期——本期申报的断流条目在本单开立（预提落账），
            # 同期已补传的先轧差：缺口只按"预估 - 已补回"的差额预提，补齐即关，
            # 未补齐的净缺口滚动到下一期，继续挂账、绝不当零。
            for entry in list(self._entries):
                if entry.period != period or entry.status != STATUS_OPEN:
                    continue
                if not entry.is_outage:
                    # 迟到条目在下面统一关。
                    continue
                opened.append(entry)
                recovered = entry.actual_mwh
                shortfall = max(0.0, entry.mwh - recovered)
                if entry.entry_type == OUTAGE_MEASURED:
                    # 权责发生制：补回的能量虽在到账期进了 current，但它计量
                    # 时刻在断流窗口（本期），要重分类到预提列，与仍缺的净缺口
                    # 一起按预估全额计入本期；否则同一窗口只计净缺口会少计补回。
                    self._add_book_column(period, entry.station, "current", -recovered)
                    self._add_book_column(period, entry.station, "accrued", recovered + shortfall)
                if recovered + _EPS >= entry.mwh:
                    # 同期补齐（或超补）：关闭条目。超补部分已重分类进预提列，
                    # 归属本计量期，无需再调整。
                    entry.status = STATUS_CLOSED
                    entry.closed_period = period
                    entry.closed_at = boundary_at
                    entry.variance_mwh = recovered - entry.mwh
                    closed.append(entry)
                else:
                    # 只补回一部分：净缺口作为滚动挂账进入下一期。
                    entry.mwh = shortfall
                    entry.actual_mwh = 0.0
                    entry.detail.setdefault("rolled_shortfalls", []).append(
                        {"at_period": period, "recovered": round(recovered, 4)}
                    )
                    rolled.append(entry)

            # 本期到账的贸易表迟到量：冲减到账期，再补前期调整到计量期。
            for entry in list(self._entries):
                if (
                    entry.period == period
                    and entry.status == STATUS_OPEN
                    and entry.entry_type == LATE_MEASURED
                ):
                    origin = int(entry.detail.get("origin_period", period - 1))
                    self._add_book_column(period, entry.station, "reversal", -entry.mwh)
                    entry.status = STATUS_CLOSED
                    entry.closed_period = period
                    entry.closed_at = boundary_at
                    entry.charge = 0.0
                    closed.append(entry)
                    adjustment = CarryEntry(
                        entry_id=_entry_id(),
                        period=period,
                        station=entry.station,
                        meter=entry.meter,
                        kind=entry.kind,
                        policy=entry.policy,
                        entry_type=PRIOR_ADJUSTMENT,
                        mwh=entry.mwh,
                        status=STATUS_CLOSED,
                        opened_at=boundary_at,
                        closed_period=period,
                        closed_at=boundary_at,
                        detail={"origin_period": origin, "paired_with": entry.entry_id},
                    )
                    charge = rate_for(origin, entry.station, entry.mwh)
                    adjustment.charge = charge
                    self._add_book_column(origin, entry.station, "adjustment", entry.mwh)
                    amendments.append((origin, entry.station, entry.mwh, charge))
                    closed.append(adjustment)
                    self._entries.append(adjustment)

            # 时序 3：出分站账与费用。
            stations = sorted(
                {station for (p, station) in self._books if p == period}
                | {entry.station for entry in opened}
            )
            books: list[StationBook] = []
            for station in stations:
                columns = self._books.setdefault(
                    (period, station),
                    {"current": 0.0, "accrued": 0.0, "reversal": 0.0, "adjustment": 0.0},
                )
                billed = (
                    columns["current"]
                    + columns["accrued"]
                    + columns["reversal"]
                    + columns["adjustment"]
                )
                charge = rate_for(period, station, billed)
                row = self._books[(period, station)]
                row["_charge"] = charge
                book = StationBook(
                    period=period,
                    station=station,
                    current_mwh=columns["current"],
                    accrued_mwh=columns["accrued"],
                    reversal_mwh=columns["reversal"],
                    adjustment_mwh=columns["adjustment"],
                    billed_mwh=billed,
                    charge=charge,
                )
                books.append(book)

            # 往期条目的前期调整费用落到对应往期账（amendments 已加能量列）。
            for old_period, station, _mwh, charge in amendments:
                prior = self._books.get((old_period, station))
                if prior is not None:
                    prior["_charge_adjust"] = prior.get("_charge_adjust", 0.0) + charge

            sheet = CarrySheet(
                period=period,
                boundary_at=boundary_at,
                status=SHEET_PARTIAL if rolled else SHEET_COMPLETE,
                opened=tuple(opened),
                closed=tuple(closed),
                rolled=tuple(rolled),
                station_books=tuple(books),
            )

            # 时序 4：一致性校验通过后才允许翻页。
            check = self._check_settlement(sheet, amendments)
            if check["failures"]:
                raise CarryError(f"结算一致性校验失败: {check['failures']}")

            self._sheets.append(sheet)
            self._boundaries.append(boundary_at)
            self._period += 1
            return {
                "period": period,
                "boundary_at": round(boundary_at, 3),
                "status": sheet.status,
                "sheet": sheet,
                "amendments": [
                    {
                        "period": old_period,
                        "station": station,
                        "adjustment_mwh": round(mwh, 4),
                        "charge": round(charge, 2),
                    }
                    for old_period, station, mwh, charge in amendments
                ],
            }

    def _close_carry(
        self,
        entry: CarryEntry,
        period: int,
        boundary_at: float,
        closed: list[CarryEntry],
        amendments: list[tuple[int, str, float, float]],
        rate_for,
    ) -> None:
        """冲平一条往期断流挂账（恢复量与预估量轧差，差额走前期调整）。"""

        actual = entry.actual_mwh
        if actual <= _EPS:
            # 仍未恢复：条目保持 open 并滚动，缺量继续挂账，不当零。
            return
        variance = actual - entry.mwh
        entry.status = STATUS_CLOSED
        entry.closed_period = period
        entry.closed_at = boundary_at
        entry.variance_mwh = variance
        closed.append(entry)
        if entry.policy == policy.POLICY_MEASURED:
            # 恢复量属于计量期：从到账期冲减；预估与实际的差额前期调整回计量期。
            self._add_book_column(period, entry.station, "reversal", -actual)
            charge = 0.0
            if abs(variance) > _EPS:
                self._add_book_column(entry.period, entry.station, "adjustment", variance)
                charge = rate_for(entry.period, entry.station, variance)
                amendments.append((entry.period, entry.station, variance, charge))
            entry.charge = charge
        # 考核表按到达时刻：恢复量已在到账期正常入账，这里只关闭跟踪条目。

    # ---- 一致性 -------------------------------------------------------

    def _check_settlement(
        self, sheet: CarrySheet, amendments: list[tuple[int, str, float, float]]
    ) -> dict:
        failures: list[str] = []

        # 1) 预提列：本期贸易表断流按预估额全额计入 accrued（补回部分是
        #    从 current 重分类过来的，净缺口是缺量预提），两列之和要对得上。
        #    条目若已滚动，mwh 会被改写成净缺口，要用申报时的原始预估额。
        accrued_opened = sum(
            float(entry.detail.get("estimated_mwh", entry.mwh))
            for entry in sheet.opened
            if entry.entry_type == OUTAGE_MEASURED
        )
        accrued_books = sum(book.accrued_mwh for book in sheet.station_books)
        if abs(accrued_opened - accrued_books) > 1e-6:
            failures.append(
                f"预提列不平: 结转单 {accrued_opened:.4f} != 分站账 {accrued_books:.4f}"
            )

        # 2) 冲减列：迟到冲减 + 往期贸易断流恢复冲减 == reversal 列（符号相反配对）。
        reversal_entries = sum(
            entry.mwh
            for entry in sheet.closed
            if entry.entry_type == LATE_MEASURED
        ) + sum(
            entry.actual_mwh
            for entry in sheet.closed
            if entry.entry_type == OUTAGE_MEASURED
            and entry.closed_period == sheet.period
            and entry.period < sheet.period
        )
        reversal_books = sum(book.reversal_mwh for book in sheet.station_books)
        if abs(reversal_entries + reversal_books) > 1e-6:
            failures.append(
                f"冲减列不平: 结转单 {reversal_entries:.4f} 与分站账 {reversal_books:.4f} 不配对"
            )

        # 3) 前期调整：条目金额 == 对往期分站账的追加。
        adjustment_entries = sum(
            entry.mwh
            for entry in sheet.closed
            if entry.entry_type == PRIOR_ADJUSTMENT
        ) + sum(
            entry.variance_mwh
            for entry in sheet.closed
            if entry.entry_type == OUTAGE_MEASURED
            and entry.closed_period == sheet.period
            and entry.period < sheet.period
        )
        adjustment_amendments = sum(mwh for _p, _s, mwh, _c in amendments)
        if abs(adjustment_entries - adjustment_amendments) > 1e-6:
            failures.append(
                f"前期调整不平: 结转单 {adjustment_entries:.4f} != 分站账 {adjustment_amendments:.4f}"
            )

        # 4) 能量守恒：各期计费合计 == 累计入账 + 挂账净额（滚动预提为正、
        #    已到账未冲的迟到量为负）。
        total_logged = self._total_logged()
        billed_all = self._total_billed()
        held = self._held_mwh()
        if abs(billed_all - total_logged - held) > 1e-6:
            failures.append(
                f"能量不守恒: 计费 {billed_all:.4f} != 入账 {total_logged:.4f} + 挂账净额 "
                f"{held:.4f}"
            )
        return {"ok": not failures, "failures": failures}

    def _total_logged(self) -> float:
        return sum(self._intake_total.values()) + sum(self._direct_total.values())

    def _held_mwh(self) -> float:
        """挂账净额：滚动中的贸易表断流净预提（正），减去已到账未冲的迟到量（负）。

        恒等关系：各期计费量合计 = 累计入账 + 挂账净额。
        """

        accrued_rolled = sum(
            entry.mwh
            for entry in self._entries
            if entry.status == STATUS_OPEN and entry.entry_type == OUTAGE_MEASURED
        )
        late_pending = sum(
            entry.mwh
            for entry in self._entries
            if entry.status == STATUS_OPEN and entry.entry_type == LATE_MEASURED
        )
        return accrued_rolled - late_pending

    def _book_charge(self, period: int, station: str, columns: dict | None = None) -> float:
        """返回某期某站的落账费用（含往期前期调整的费用追加）。"""

        book = self._books.get((period, station))
        if book is None:
            return 0.0
        return float(book.get("_charge", 0.0)) + float(book.get("_charge_adjust", 0.0))

    def _total_billed(self) -> float:
        total = 0.0
        for (_period, station), columns in self._books.items():
            total += (
                columns["current"]
                + columns["accrued"]
                + columns["reversal"]
                + columns["adjustment"]
            )
        return total

    def reconcile(self) -> dict:
        """对外的三账一致校验（结转单 / 分站账 / 到账流水）。

        任何时刻（含跨期结算进行中）都应满足：

            累计入账 = 已结算各期计费量 + 当前期视图能量 + 净挂账

        其中净挂账为正表示已计费但能量尚未到账（滚动中的断流预提），为负
        表示能量已到账但归属往期、等下一次结算冲减（迟到/恢复量）。

        恒等式：累计入账 = 已结各期计费 + 当前期计费 - 净挂账。
        """

        with self._lock:
            failures: list[str] = []
            total_logged = self._total_logged()
            settled = sum(
                columns["current"]
                + columns["accrued"]
                + columns["reversal"]
                + columns["adjustment"]
                for (p, _station), columns in self._books.items()
                if p < self._period
            )
            current = sum(
                columns.get("current", 0.0)
                + columns.get("accrued", 0.0)
                + columns.get("reversal", 0.0)
                + columns.get("adjustment", 0.0)
                for columns in self.period_view(self._period).values()
            )
            # 任何时刻：累计入账 = 已结 + 当期 - 净挂账。
            # 正挂账（断流净预提）是"已计费、能量未到账"，从账单侧扣除；
            # 负挂账（已到账、归属往期等冲减）加回来。
            held = self._held_mwh()
            if abs(total_logged - settled - current + held) > 1e-6:
                failures.append(
                    f"分站账计费量与累计入账+净挂账不守恒：入账 {total_logged:.4f} != "
                    f"已结 {settled:.4f} + 当期 {current:.4f} - 净挂账 {held:.4f}"
                )

            # 每条未平条目都必须能在结转单（滚动列）或当前期挂账中找到归属，
            # 不允许出现"开了条目但哪张单都看不见"的黑洞。
            rolled_ids: set[str] = set()
            for sheet in self._sheets:
                rolled_ids.update(entry.entry_id for entry in sheet.rolled)
            current_pending = {
                entry.entry_id
                for entry in self._entries
                if entry.status == STATUS_OPEN and entry.period >= self._period
            }
            for entry in self._entries:
                if (
                    entry.status == STATUS_OPEN
                    and entry.entry_id not in rolled_ids
                    and entry.entry_id not in current_pending
                ):
                    failures.append(f"未平条目 {entry.entry_id} 在结转单与当期挂账中均无归属")
            billed_all = settled + current
            return {
                "ok": not failures,
                "failures": failures,
                "logged_mwh": round(total_logged, 4),
                "settled_mwh": round(settled, 4),
                "current_mwh": round(current, 4),
                "billed_mwh": round(billed_all, 4),
                "net_carried_mwh": round(held, 4),
            }

    # ---- 查询 ---------------------------------------------------------

    def period(self) -> int:
        """返回当前结算期次。"""

        return self._period

    def periods_settled(self) -> int:
        """返回已结算期数。"""

        return len(self._boundaries)

    def boundaries(self) -> list[float]:
        """返回各期结算边界。"""

        return list(self._boundaries)

    def entries(self, status: str | None = None) -> list[CarryEntry]:
        """返回结转条目（可按状态过滤）。"""

        with self._lock:
            return [
                entry
                for entry in self._entries
                if status is None or entry.status == status
            ]

    def sheets(self) -> list[CarrySheet]:
        """返回全部结转单。"""

        return list(self._sheets)

    def latest_sheet(self) -> CarrySheet | None:
        """返回最近一张结转单（控制台展示用）。"""

        return self._sheets[-1] if self._sheets else None

    def open_entries(self) -> list[CarryEntry]:
        """返回全部未平条目。"""

        return self.entries(STATUS_OPEN)

    def open_carry_mwh(self) -> float:
        """返回挂账中的缺口/迟到总量（控制台摘要用）。"""

        return sum(entry.mwh for entry in self.open_entries())

    def station_books(self, period: int | None = None) -> list[StationBook]:
        """返回分站账（不指定期次则返回全部，含往期被前期调整追加的口径）。"""

        books: list[StationBook] = []
        for (p, station) in sorted(self._books, key=lambda key: (key[0], key[1])):
            if period is not None and p != period:
                continue
            columns = self._books[(p, station)]
            billed = (
                columns["current"]
                + columns["accrued"]
                + columns["reversal"]
                + columns["adjustment"]
            )
            books.append(
                StationBook(
                    period=p,
                    station=station,
                    current_mwh=columns["current"],
                    accrued_mwh=columns["accrued"],
                    reversal_mwh=columns["reversal"],
                    adjustment_mwh=columns["adjustment"],
                    billed_mwh=billed,
                    charge=self._book_charge(p, station, columns),
                )
            )
        return books

    # ---- 持久化 -------------------------------------------------------

    def to_dict(self) -> dict:
        """序列化成可落盘的映射。"""

        return {
            "period": self._period,
            "boundaries": self._boundaries,
            "period_start_at": self._period_start_at,
            "seq": self._seq,
            "intake_total": dict(self._intake_total),
            "direct_total": dict(self._direct_total),
            "intake_count": self._intake_count,
            "meters": [
                {"meter": meter, "station": info[0], "kind": info[1]}
                for meter, info in sorted(self._meters.items())
            ],
            "entries": [_entry_to_dict(entry) for entry in self._entries],
            "books": [
                {
                    "period": p,
                    "station": station,
                    **columns,
                    "charge": self._book_charge(p, station, columns),
                }
                for (p, station), columns in sorted(self._books.items())
            ],
            "sheets": [
                {
                    "period": sheet.period,
                    "boundary_at": sheet.boundary_at,
                    "status": sheet.status,
                    "opened": [entry.entry_id for entry in sheet.opened],
                    "closed": [entry.entry_id for entry in sheet.closed],
                    "rolled": [entry.entry_id for entry in sheet.rolled],
                }
                for sheet in self._sheets
            ],
        }

    def load_dict(self, raw: dict) -> None:
        """从落盘映射恢复（装配后、投入使用前调用）。"""

        with self._lock:
            self._period = int(raw.get("period", 1))
            self._boundaries = [float(value) for value in raw.get("boundaries", [])]
            self._period_start_at = raw.get("period_start_at")
            self._seq = int(raw.get("seq", 0))
            self._intake_total = defaultdict(float)
            for meter, value in raw.get("intake_total", {}).items():
                self._intake_total[meter] = float(value)
            self._direct_total = defaultdict(float)
            for station, value in raw.get("direct_total", {}).items():
                self._direct_total[station] = float(value)
            self._intake_count = int(raw.get("intake_count", 0))
            self._meters = {}
            for item in raw.get("meters", []):
                kind = policy.normalize_kind(item["kind"])
                self._meters[item["meter"]] = (
                    item["station"],
                    kind,
                    policy.policy_for(kind),
                )
            self._entries = [_entry_from_dict(item) for item in raw.get("entries", [])]
            self._books = {}
            for item in raw.get("books", []):
                self._books[(int(item["period"]), str(item["station"]))] = {
                    "current": float(item.get("current", 0.0)),
                    "accrued": float(item.get("accrued", 0.0)),
                    "reversal": float(item.get("reversal", 0.0)),
                    "adjustment": float(item.get("adjustment", 0.0)),
                    "_charge_adjust": float(item.get("_charge_adjust", 0.0)),
                    "_charge": float(
                        item.get("_charge", item.get("charge", 0.0))
                        - item.get("_charge_adjust", 0.0)
                    ),
                }
            self._sheets = []
            by_id = {entry.entry_id: entry for entry in self._entries}

            def pick(ids: list[str]) -> tuple[CarryEntry, ...]:
                return tuple(by_id[item] for item in ids if item in by_id)

            for item in raw.get("sheets", []):
                period = int(item["period"])
                self._sheets.append(
                    CarrySheet(
                        period=period,
                        boundary_at=float(item["boundary_at"]),
                        status=str(item["status"]),
                        opened=pick(list(item.get("opened", []))),
                        closed=pick(list(item.get("closed", []))),
                        rolled=pick(list(item.get("rolled", []))),
                        station_books=tuple(self.station_books(period)),
                    )
                )


def _entry_to_dict(entry: CarryEntry) -> dict:
    payload = entry.as_dict()
    payload["detail"] = dict(entry.detail)
    return payload


def _entry_from_dict(raw: dict) -> CarryEntry:
    return CarryEntry(
        entry_id=str(raw.get("entry_id", _entry_id())),
        period=int(raw.get("period", 1)),
        station=str(raw.get("station", "")),
        meter=str(raw.get("meter", "")),
        kind=str(raw.get("kind", "trade")),
        policy=str(raw.get("policy", "measured")),
        entry_type=str(raw.get("type", raw.get("entry_type", LATE_MEASURED))),
        mwh=float(raw.get("mwh", 0.0)),
        status=str(raw.get("status", STATUS_OPEN)),
        window_id=str(raw.get("window_id", "")),
        opened_at=float(raw.get("opened_at", 0.0)),
        closed_period=int(raw.get("closed_period", 0)),
        closed_at=float(raw.get("closed_at", 0.0)),
        actual_mwh=float(raw.get("actual_mwh", 0.0)),
        variance_mwh=float(raw.get("variance_mwh", 0.0)),
        charge=float(raw.get("charge", 0.0)),
        detail=dict(raw.get("detail", {})),
    )
