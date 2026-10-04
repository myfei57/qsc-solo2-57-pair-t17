"""跨期结转：归属定死、预提冲平、断流挂账与结转单一致性。"""

from __future__ import annotations

import pytest

from heatnet.config import default_config
from heatnet.console.api import Api
from heatnet.errors import InterlockError, PlantError
from heatnet.quota.accumulator import QuotaAccumulator
from heatnet.quota.carryover import ATTRIBUTION_BY_KIND, CarryoverBook
from heatnet.quota.meter import HeatMeter
from heatnet.quota.quota import QuotaLedger


def test_late_arrival_accrues_then_reverses_at_next_settle(platform, clock):
    platform.collect_heat("st01.heat-meter", 5.0)
    first = platform.settle_quota()
    assert first["period"] == 1
    assert first["partial"] is False
    assert first["carryover"]["reversals"] == []

    boundary1 = first["carryover"]["boundary_ts"]
    clock.advance(600.0)
    late = platform.collect_heat("st01.heat-meter", 2.0, measured_at=boundary1 - 1.0)
    assert late["booking"] == "accrued"
    assert late["attribution"] == "measured"
    assert late["home_period"] == 1
    assert late["booked_period"] is None
    # 预提不进本期活账：分站账上看不到这笔迟到增量
    assert platform.ledger.allocated_mwh("st01") == 0.0

    second = platform.settle_quota()
    assert second["period"] == 2
    slip = second["carryover"]
    assert [item["amount_mwh"] for item in slip["reversals"]] == [pytest.approx(2.0)]
    assert slip["reversals"][0]["home_period"] == 1
    assert slip["reversals"][0]["state"] == "reversed"
    corrections = [line for line in second["lines"] if line.get("correction")]
    assert len(corrections) == 1
    assert corrections[0]["station"] == "st01"
    assert corrections[0]["mwh"] == pytest.approx(2.0)
    assert corrections[0]["charge"] == pytest.approx(2.0 * 32.0)
    assert corrections[0]["home_period"] == 1
    # 冲平之后预提不留未了事项
    assert slip["outstanding"] == []


def test_arrival_kind_defers_to_open_period(clock):
    accumulator = QuotaAccumulator()
    accumulator.register(HeatMeter("f01.flow-meter", 1000.0, kind="flow"))
    ledger = QuotaLedger(default_config().quota, accumulator)
    book = CarryoverBook(ledger, clock)
    clock.advance(100.0)
    book.close_period(1, clock.now())
    ledger.settle()
    clock.advance(100.0)

    decision = book.book_arrival(
        meter="f01.flow-meter",
        station="f01",
        kind="flow",
        delta_mwh=3.0,
        measured_at=clock.now() - 150.0,
        arrived_at=clock.now(),
    )
    assert decision["attribution"] == "arrival"
    assert decision["booking"] == "booked"
    assert decision["home_period"] == 1
    assert decision["booked_period"] == 2
    assert decision["deferred"] is True
    assert ledger.allocated_mwh("f01") == pytest.approx(3.0)

    with pytest.raises(PlantError):
        book.book_arrival(
            meter="f01.flow-meter",
            station="f01",
            kind="unknown-kind",
            delta_mwh=1.0,
            measured_at=clock.now(),
            arrived_at=clock.now(),
        )


def test_outage_is_partial_failure_and_suspense_is_not_zero(platform, clock):
    platform.collect_heat("st01.heat-meter", 5.0)
    platform.settle_quota()
    clock.advance(3600.0)

    second = platform.settle_quota()
    assert second["partial"] is True
    slip = second["carryover"]
    assert len(slip["new_suspense"]) == 1
    entry = slip["new_suspense"][0]
    assert entry["station"] == "st01"
    assert entry["home_period"] == 2
    assert entry["reason"] == "collection-outage"
    assert entry["expected_mwh"] is None
    # 缺数挂账：分站账上是挂账标记而不是结零
    assert slip["stations"]["st01"]["new_suspense"] is True
    assert slip["stations"]["st01"]["booked_mwh"] == 0.0
    assert [item["suspense_id"] for item in slip["outstanding"]] == [entry["suspense_id"]]
    # 断流挂账落审计告警档
    assert any(item["action"] == "carryover_suspense" for item in platform.audit.alarms())


def test_backfill_recovers_suspense_and_audit_matches_console(platform, clock):
    platform.collect_heat("st01.heat-meter", 5.0)
    first = platform.settle_quota()
    boundary1 = first["carryover"]["boundary_ts"]
    clock.advance(3600.0)
    second = platform.settle_quota()
    suspense_id = second["carryover"]["new_suspense"][0]["suspense_id"]

    clock.advance(600.0)
    backfill = platform.collect_heat("st01.heat-meter", 3.0, measured_at=boundary1 + 10.0)
    assert backfill["booking"] == "accrued"
    assert backfill["home_period"] == 2
    assert backfill["recovered"] == [suspense_id]

    third = platform.settle_quota()
    slip = third["carryover"]
    assert slip["partial"] is False
    assert slip["recoveries"][0]["suspense_id"] == suspense_id
    assert slip["recoveries"][0]["recovered_mwh"] == pytest.approx(3.0)
    assert slip["outstanding"] == []
    corrections = [line for line in third["lines"] if line.get("correction")]
    assert corrections[0]["mwh"] == pytest.approx(3.0)
    assert corrections[0]["home_period"] == 2

    # 结转单、控制台与审计一致：三处读的是同一份结转单
    api = Api(platform)
    status, report = api.handle("GET", "/api/carryover", {}, {})
    assert status == 200
    audited = [
        entry for entry in platform.audit.entries(limit=50)
        if entry["action"] == "carryover_settled"
    ]
    assert audited[0]["payload"]["slip"] == report["slips"][-1]
    assert any(
        entry["action"] == "carryover_recovered" for entry in platform.audit.entries(limit=50)
    )


def test_close_period_order_and_measured_time_validation(platform, clock):
    with pytest.raises(InterlockError) as excinfo:
        platform.carryover.close_period(3, clock.now())
    assert excinfo.value.component == "quota"
    with pytest.raises(InterlockError):
        platform.carryover.close_period(1, clock.now() - 1.0)
    with pytest.raises(ValueError):
        platform.collect_heat("st01.heat-meter", 1.0, measured_at=clock.now() + 100.0)
    # 乱序与非法计量时刻都被拒绝后，正常结算不受影响
    settled = platform.settle_quota()
    assert settled["period"] == 1


def test_carryover_endpoint_exposes_pinned_attribution(platform):
    api = Api(platform)
    platform.collect_heat("st01.heat-meter", 4.0)
    platform.settle_quota()

    status, report = api.handle("GET", "/api/carryover", {}, {})
    assert status == 200
    assert report["attribution"] == ATTRIBUTION_BY_KIND == {"heat": "measured", "flow": "arrival"}
    assert report["current_period"] == 2
    assert report["slips"][0]["attribution"] == report["attribution"]

    status, one = api.handle("GET", "/api/carryover", {"period": "1"}, {})
    assert status == 200
    assert one["slip"]["slip_id"] == "carry-0001"
    status, missing = api.handle("GET", "/api/carryover", {"period": "99"}, {})
    assert status == 404

    status, bad = api.handle(
        "POST", "/api/meter", {},
        {"meter": "st01.heat-meter", "delta_mwh": 1.0, "measured_at": 9e18},
    )
    assert status == 400

    quota = platform.quota_report()
    assert quota["carryover"]["attribution"] == report["attribution"]
    assert quota["carryover"]["slips"] == 1
    assert quota["carryover"]["outstanding"] == 0
