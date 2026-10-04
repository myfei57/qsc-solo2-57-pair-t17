"""热量表回绕、并发累加、配额结算与跨期结转。"""

from __future__ import annotations

import threading

import pytest

from heatnet.config import default_config
from heatnet.quota.accumulator import QuotaAccumulator
from heatnet.quota.billing import billing_lines, charge_for
from heatnet.quota.carryover import CarryBook, CarryError
from heatnet.quota.meter import HeatMeter
from heatnet.quota.policy import KIND_RUN, KIND_TRADE
from heatnet.quota.quota import QuotaLedger


def _rate(quota_mwh):
    return lambda _period, _station, mwh: charge_for(mwh, quota_mwh)


def test_meter_wraps_reading_but_keeps_total():
    meter = HeatMeter("st01.heat-meter", 10.0)
    meter.add(6.0)
    meter.add(6.0)
    assert meter.reading_mwh() == pytest.approx(2.0)
    assert meter.total_mwh() == pytest.approx(12.0)
    assert meter.wraps() == 1
    assert meter.increments() == 2
    assert meter.snapshot().as_dict()["reading_mwh"] == pytest.approx(2.0)
    assert meter.name == "st01.heat-meter"
    assert meter.wrap_mwh == pytest.approx(10.0)
    assert meter.kind == KIND_TRADE
    with pytest.raises(ValueError):
        meter.add(-1.0)
    with pytest.raises(ValueError):
        HeatMeter("bad", 0.0)


def test_meter_kind_pins_attribution_policy():
    assert HeatMeter("a.heat-meter", 10.0, KIND_TRADE).policy() == "measured"
    assert HeatMeter("b.heat-meter", 10.0, KIND_RUN).policy() == "arrival"
    with pytest.raises(ValueError):
        HeatMeter("c.heat-meter", 10.0, "bogus")


def test_concurrent_increments_are_not_lost():
    meter = HeatMeter("st01.heat-meter", 10_000_000.0)

    def worker() -> None:
        for _ in range(250):
            meter.add(1.0)

    threads = [threading.Thread(target=worker) for _ in range(4)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    assert meter.total_mwh() == pytest.approx(1000.0)
    assert meter.increments() == 1000


def test_accumulator_and_ledger():
    accumulator = QuotaAccumulator()
    accumulator.register(HeatMeter("st01.heat-meter", 1000.0))
    accumulator.register(HeatMeter("st02.heat-meter", 1000.0))
    with pytest.raises(ValueError):
        accumulator.register(HeatMeter("st01.heat-meter", 1000.0))
    accumulator.collect("st01.heat-meter", 12.0)
    accumulator.collect("st02.heat-meter", 8.0)
    assert accumulator.meters() == ["st01.heat-meter", "st02.heat-meter"]
    assert accumulator.total_mwh() == pytest.approx(20.0)
    assert accumulator.collections() == 2
    assert accumulator.describe()["count"] == 2
    with pytest.raises(KeyError):
        accumulator.get("nope")

    ledger = QuotaLedger(default_config().quota, accumulator)
    ledger.register_station_meter("st01.heat-meter", "st01", KIND_TRADE)
    ledger.register_station_meter("st02.heat-meter", "st02", KIND_TRADE)
    assert ledger.allocate("st01", 12.0) == pytest.approx(12.0)
    ledger.allocate("st02", 8.0)
    assert ledger.stations() == ["st01", "st02"]
    assert ledger.total_mwh() == pytest.approx(20.0)
    lines = billing_lines(ledger.carry, 10.0)
    assert [line["station"] for line in lines] == ["st01", "st02"]
    assert lines[0]["overage_mwh"] == pytest.approx(2.0)
    assert lines[0]["charge"] == pytest.approx(10.0 * 32.0 + 2.0 * 32.0 * 1.6)
    assert lines[1]["overage_mwh"] == pytest.approx(0.0)

    settled = ledger.settle(boundary_at=1_758_086_400.0, quota_mwh=10.0)
    assert settled["period"] == 1
    assert settled["total_mwh"] == pytest.approx(20.0)
    assert ledger.periods() == 1
    assert ledger.stations() == []
    assert ledger.snapshot().as_dict()["period_hours"] == pytest.approx(24.0)
    assert ledger.accumulator is accumulator
    assert settled["status"] == "complete"
    assert settled["reconcile"]["ok"] is True


def test_trade_meter_attributes_by_measured_time_late_amount():
    """贸易表迟到量：冲减到账期 + 前期调整补回计量期，净额为零。"""

    book = CarryBook()
    book.register_meter("s.heat-meter", "st", KIND_TRADE)
    t0 = 1000.0
    boundary = t0 + 100.0
    book.intake("s.heat-meter", 50.0, t0, t0)
    book.settle(boundary, _rate(1000.0))
    # 边界之后才到账，但计量时刻在第 1 期
    record = book.intake("s.heat-meter", 7.0, boundary - 1, boundary + 10)
    assert record.attributed_period == 1
    outcome = book.settle(boundary + 200.0, _rate(1000.0))
    sheet = outcome["sheet"]
    types = [entry.entry_type for entry in sheet.closed]
    assert "late_measured" in types and "prior_adjustment" in types
    period2 = {b.station: b for b in sheet.station_books}["st"]
    # 到账先进本期再冲减：50.. 第 2 期自身没有正常量
    assert period2.billed_mwh == pytest.approx(0.0)
    period1 = {b.station: b for b in book.station_books(1)}["st"]
    assert period1.billed_mwh == pytest.approx(57.0)
    assert book.reconcile()["ok"] is True


def test_run_meter_attributes_by_arrival_time():
    """考核表迟到/补传一律顺延到到账期，不产生前期调整。"""

    book = CarryBook()
    book.register_meter("s.heat-meter", "st", KIND_RUN)
    t0 = 1000.0
    boundary = t0 + 100.0
    book.intake("s.heat-meter", 50.0, t0, t0)
    first = book.settle(boundary, _rate(1000.0))
    assert {b.station: b for b in first["sheet"].station_books}["st"].billed_mwh == pytest.approx(50.0)
    record = book.intake("s.heat-meter", 7.0, boundary - 1, boundary + 10)
    assert record.attributed_period == 2
    second = book.settle(boundary + 200.0, _rate(1000.0))
    assert {b.station: b for b in second["sheet"].station_books}["st"].billed_mwh == pytest.approx(7.0)
    assert all(entry.entry_type != "prior_adjustment" for entry in second["sheet"].closed)
    assert book.reconcile()["ok"] is True


def test_outage_is_partial_failure_accrued_not_zero():
    """断流是部分失败：贸易表预提计费、滚动 partial；考核表只挂账不计提。"""

    trade = CarryBook()
    trade.register_meter("s.heat-meter", "st", KIND_TRADE)
    trade.declare_outage("s.heat-meter", 9.0, 1099.0, window_id="gap")
    outcome = trade.settle(1100.0, _rate(1000.0))
    assert outcome["status"] == "partial"
    book = {b.station: b for b in outcome["sheet"].station_books}["st"]
    assert book.accrued_mwh == pytest.approx(9.0)
    assert book.billed_mwh == pytest.approx(9.0)
    with pytest.raises(CarryError):
        trade.declare_outage("s.heat-meter", 0.0, 1200.0, window_id="zero")

    run = CarryBook()
    run.register_meter("s.heat-meter", "st", KIND_RUN)
    run.declare_outage("s.heat-meter", 9.0, 1099.0, window_id="gap")
    run_outcome = run.settle(1100.0, _rate(1000.0))
    assert run_outcome["status"] == "partial"
    assert run.open_entries()[0].billable is False


def test_same_period_partial_recovery_is_reclassified_to_accrued():
    """结算前已补回一部分：补回量从本期重分类到预提，净缺口滚动，不重不漏。"""

    book = CarryBook()
    book.register_meter("s.heat-meter", "st", KIND_TRADE)
    # 正常到账 100；申报断流缺口 10，窗口内先补回 4
    book.intake("s.heat-meter", 100.0, 1000.0, 1000.0)
    book.declare_outage("s.heat-meter", 10.0, 1099.0, window_id="gap")
    book.intake("s.heat-meter", 4.0, 1095.0, 1098.0, window_id="gap")
    outcome = book.settle(1100.0, _rate(1000.0))
    assert outcome["status"] == "partial"
    row = {b.station: b for b in outcome["sheet"].station_books}["st"]
    # current=100（补回的 4 已重分类出去），accrued=4+6=10（预估全额）
    assert row.current_mwh == pytest.approx(100.0)
    assert row.accrued_mwh == pytest.approx(10.0)
    assert row.billed_mwh == pytest.approx(110.0)
    rolled = outcome["sheet"].rolled
    assert len(rolled) == 1 and rolled[0].mwh == pytest.approx(6.0)
    assert book.reconcile()["ok"] is True


def test_outage_recovery_reverses_and_squares():
    """预提在下期恢复后冲平：恢复先进到账期，全额冲减，与预估的差额补计量期。"""

    book = CarryBook()
    book.register_meter("s.heat-meter", "st", KIND_TRADE)
    book.declare_outage("s.heat-meter", 10.0, 1099.0, window_id="gap")
    book.settle(1100.0, _rate(1000.0))
    # 实际补回 12（超预提 2）
    book.intake("s.heat-meter", 12.0, 1095.0, 1200.0, window_id="gap")
    outcome = book.settle(1300.0, _rate(1000.0))
    period2 = {b.station: b for b in outcome["sheet"].station_books}["st"]
    assert period2.current_mwh == pytest.approx(12.0)
    assert period2.reversal_mwh == pytest.approx(-12.0)
    # 到账期净额为 0：恢复能量按计量时刻全部归属第 1 期。
    assert period2.billed_mwh == pytest.approx(0.0)
    period1 = {b.station: b for b in book.station_books(1)}["st"]
    # 第 1 期：预提 10 + 前期调整 2 = 实际 12。
    assert period1.accrued_mwh == pytest.approx(10.0)
    assert period1.adjustment_mwh == pytest.approx(2.0)
    assert period1.billed_mwh == pytest.approx(12.0)
    assert outcome["status"] == "complete"
    assert book.reconcile()["ok"] is True
