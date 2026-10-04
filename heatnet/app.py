"""平台装配与控制循环。

``Platform`` 把热源、一次网、换热站、二次网、阀门、泵、曲线、定压、热量与审计
按真实调用链装到一起，并暴露四件事：操作编排（气象推送、调质调量、投运、调度、
停运、拆分、定压改造）、控制循环、并发探测与状态快照。控制循环每个周期推进热源
与二次网、把一次网跟随上去、落审计并采集趋势。
"""

from __future__ import annotations

import dataclasses
import threading

from .audit.log import AuditLog
from .audit.record import AuditRecord
from .clock import Clock, SystemClock
from .config import PlatformConfig, default_config
from .errors import PlantError
from .eventbus import Event, EventBus
from .ids import IdFactory
from .ns.registry import NamespaceRegistry
from .ns.topology import Topology, build_default_topology
from .ns.zone import Zone, ZoneRegistry
from .pressure.baseline import PressureBaseline
from .pressure.control import PressurisingController
from .primary.flow import batch_flows
from .primary.network import PrimaryNetwork
from .pump.curve import PumpCurve
from .pump.pump import Pump
from .quota.accumulator import QuotaAccumulator
from .quota.billing import billing_lines
from .quota.carryover import CarryError
from .quota.meter import HeatMeter
from .quota import policy
from .quota.quota import QuotaLedger
from .secondary.cycle import run_cycle
from .secondary.loop import SecondaryLoop
from .sequence import advance_heating, dispatch_demand, recalibrate_flow, shutdown_plant, startup_plant
from .source.adjust import Adjustment, apply_adjustment
from .source.source import HeatSource
from .source.weather import WeatherPush, push_weather
from .station.batch import build_plan
from .station.mapping import StationMapping
from .station.service import CancelToken, StationService
from .station.station import HeatStation
from .store.command import CommandCheckpoint
from .store.jsonfile import JsonStore
from .store.journal import JsonlJournal
from .store.metrics import StoreMetrics
from .store.state import DurableState
from .temp.curve import HeatCurve
from .temp.forecast import build_forecast, describe_forecast
from .telemetry import TrendBuffer
from .valve.move import load_valve
from .valve.queue import ValveCommandQueue, replay_queue
from .valve.valve import Valve

DEFAULT_UNIT = "heatnet"
DEFAULT_ZONE = "north"

# 分区布置：三个供热分区各自带若干换热站，拆分演示从 north 里划出一个新分区。
_ZONE_SPECS: tuple[tuple[str, float, tuple[str, ...]], ...] = (
    ("north", 62.0, ("st01", "st02", "st03")),
    ("south", 58.0, ("st04", "st05")),
    ("east", 54.0, ("st06",)),
)

# 热量表按站登记，回绕阈值取配置值。
_METER_SUFFIX = ".heat-meter"

# 按站指定运行考核表（按到达时刻归属）；其余表按默认表型（贸易结算表，
# 按计量时刻归属）。表型是跨期口径的唯一依据，在装配期定死。
_RUN_KIND_STATIONS = frozenset({"st06"})


class Platform:
    """城市集中供热管网调度平台。"""

    def __init__(
        self,
        config: PlatformConfig | None = None,
        clock: Clock | None = None,
        unit: str = DEFAULT_UNIT,
    ) -> None:
        self.config = config or default_config()
        self.clock = clock or SystemClock()
        self.bus = EventBus()
        self.unit = unit

        store = JsonStore(self.config.storage_root)
        self.store = store
        self.metrics = StoreMetrics(store.root)
        self.durable = DurableState(store)
        self.checkpoint = CommandCheckpoint(store)
        self.audit = AuditLog(
            self.clock, self.bus, JsonlJournal(store.root, "audit", segment_limit=512)
        )
        self.namespaces = NamespaceRegistry(unit)
        self.topology: Topology = build_default_topology()
        self.trend = TrendBuffer(period_s=self.config.quota.sample_period_s)
        self.ids = IdFactory("op")

        self.zones = ZoneRegistry()
        self.curve = HeatCurve(self.config.curve)
        self.source = HeatSource(self.config.source)
        self.network = PrimaryNetwork(self.config.primary)
        self.secondary = SecondaryLoop(self.config.secondary)
        self._build_zones()
        self.mapping = StationMapping(self.zones)

        self.stations, self.valves, self.queues = self._build_stations()
        self.service = StationService(self.stations, self.valves, self.durable)
        self.pump: Pump = self._build_pump()

        self.meters = QuotaAccumulator()
        self.ledger = QuotaLedger(self.config.quota, self.meters)
        self._build_meters()
        self.pressure = PressurisingController(
            PressureBaseline(self.config.pressure), self.config.pressure
        )

        self._cycles = 0
        self._operations = 0
        self._lock = threading.RLock()
        self._register_namespaces()
        self._restore()
        self._restore_carry()

    # ---- 装配 ---------------------------------------------------------

    def _build_zones(self) -> None:
        """登记供热分区与站归属。"""

        for name, setpoint_c, stations in _ZONE_SPECS:
            self.zones.add(Zone(name=name, setpoint_c=setpoint_c))
            for station in stations:
                self.zones.assign(station, name)

    def _build_stations(self) -> tuple[dict[str, HeatStation], dict[str, Valve], dict[str, ValveCommandQueue]]:
        """按分区表登记换热站、混水阀与各自的指令队列。"""

        stations: dict[str, HeatStation] = {}
        valves: dict[str, Valve] = {}
        queues: dict[str, ValveCommandQueue] = {}
        for zone in self.zones.names():
            for name in self.zones.stations(zone):
                station = HeatStation(name, zone, self.config.station)
                stations[name] = station
                valves[name] = Valve(f"{name}.mix", self.config.valve)
                queues[name] = ValveCommandQueue(name)
        return stations, valves, queues

    def _build_pump(self) -> Pump:
        """按一次网基线构造循环泵特性。"""

        baseline = self.config.primary.flow_baseline_t_h
        curve = PumpCurve(
            self.config.pump.min_speed_pct,
            self.config.pump.max_speed_pct,
            baseline * 0.25,
            baseline,
        )
        return Pump("secondary.circulating", self.config.pump, curve)

    def _build_meters(self) -> None:
        """按站登记热量表，并把表型与归属关系定死进结转账册。"""

        wrap = self.config.quota.meter_wrap_mwh
        default_kind = self.config.quota.default_meter_kind
        for name in sorted(self.stations):
            meter_name = f"{name}{_METER_SUFFIX}"
            kind = policy.KIND_RUN if name in _RUN_KIND_STATIONS else default_kind
            self.meters.register(HeatMeter(meter_name, wrap, kind))
            self.ledger.register_station_meter(meter_name, name, kind)

    def _register_namespaces(self) -> None:
        """登记现场设备命名空间（控制台与审计共用同一套名字）。"""

        for area, device, kind in (
            ("plant", "heat_source", "heat-source"),
            ("plant", "weather", "weather-gateway"),
            ("primary", "supply_header", "header"),
            ("primary", "flow_meter", "flow-meter"),
            ("secondary", "circulating_pump", "pump"),
            ("secondary", "makeup_valve", "control-valve"),
            ("secondary", "pressure_sensor", "pressure-sensor"),
        ):
            self.namespaces.register(area, device, kind)
        for name in sorted(self.stations):
            self.namespaces.register("station", name, "heat-exchange-station")
            self.namespaces.register("valve", f"{name}.mix", "mix-valve")
            self.namespaces.register("quota", f"{name}.heat-meter", "heat-meter")

    def _restore(self) -> None:
        """从落盘状态恢复平台计数与阀门开度。"""

        state = self.store.read("platform_state")
        if isinstance(state, dict):
            self._operations = int(state.get("operations", 0))
            self._cycles = int(state.get("cycles", 0))
        for name, valve in self.valves.items():
            load_valve(valve, self.durable)
            applied = self.checkpoint.load(name)
            for command in self.queues[name].commands():
                if command.seq <= applied:
                    continue
        try:
            records = self.audit.to_records(self.audit.replay_from_journal(limit=100_000))
        except PlantError:
            return
        trips = [item for item in records if item.action == "trip"]
        if trips:
            reason = str(trips[-1].payload.get("reason", "unknown"))
            self.source.trip(reason)

    def _persist(self) -> None:
        """落盘平台计数，供重启后对照。"""

        self.store.write(
            "platform_state",
            {"operations": self._operations, "cycles": self._cycles, "unit": self.unit},
        )

    def _persist_carry(self) -> None:
        """把跨期结转账册落盘（结转条目、分站账、期次边界）。"""

        self.store.write("quota_carry", self.ledger.carry.to_dict())

    def _restore_carry(self) -> None:
        """从落盘状态恢复结转账册。"""

        state = self.store.read("quota_carry")
        if isinstance(state, dict):
            self.ledger.carry.load_dict(state)

    def _record_operation(self, op_id: str, operation: str, report: dict) -> dict:
        """把编排步骤逐条写审计，并记一条汇总事件。"""

        asset = str(report.get("asset", report.get("station", "")))
        for step in report.get("steps", []):
            detail = dict(step.get("detail", {}))
            flat = {
                key: value
                for key, value in detail.items()
                if not isinstance(value, (dict, list))
            }
            self.bus.publish(
                Event.of(
                    step["component"],
                    step["component"],
                    step["action"],
                    asset=asset,
                    op_id=op_id,
                    severity="notice",
                    **flat,
                )
            )
        for violation in report.get("violations", []):
            self.bus.publish(
                Event.of(
                    "audit",
                    "audit",
                    "interlock_rejected",
                    asset=asset,
                    op_id=op_id,
                    severity="alarm",
                    message=f"{operation} 被拒绝：{violation}",
                    violation=violation,
                )
            )
        summary = {
            "op_id": op_id,
            "operation": operation,
            "ok": bool(report.get("ok")),
            "violations": list(report.get("violations", [])),
        }
        if report.get("stage"):
            summary["stage"] = report["stage"]
        if report.get("reason"):
            summary["reason"] = report["reason"]
        event_action = f"{operation}_{'complete' if report.get('ok') else 'incomplete'}"
        self.bus.publish(
            Event.of(
                "audit",
                "audit",
                event_action,
                severity="notice" if report.get("ok") else "warning",
                message=f"{operation} 完成" if report.get("ok") else f"{operation} 未完成",
                **summary,
            )
        )
        return summary

    # ---- 生命周期 -----------------------------------------------------

    def start(self) -> dict:
        """启动平台：核对命名空间与调度链拓扑。"""

        with self._lock:
            report = self.topology.verify(set())
            source_device = self.namespaces.resolve("plant.heat_source")
            return {
                "unit": self.unit,
                "topology_missing": report["missing"],
                "source_device": source_device.address,
                "namespaces": self.namespaces.describe()["count"],
                "zones": self.zones.describe()["count"],
                "stations": len(self.stations),
                "operations": self._operations,
            }

    def stop(self) -> dict:
        """停运平台：先停泵关阀，再撤流量，最后热源停机。"""

        return self.shutdown("planned-shutdown")

    def startup(self, outdoor_c: float, demand_pct: float = 60.0, station: str = "console") -> dict:
        """按调度链顺序把平台从冷态带到带负荷。"""

        with self._lock:
            op_id = self.ids.next()
            report = startup_plant(
                self.source,
                self.curve,
                self.network,
                self.secondary,
                self.pump,
                self.valves[sorted(self.valves)[0]],
                self.durable,
                outdoor_c,
                self.clock.now(),
                demand_pct,
            )
            summary = self._record_operation(op_id, "startup", report)
            self._operations += 1
            self._persist()
            return {**report, **summary, "station": station}

    def shutdown(self, reason: str = "planned-shutdown") -> dict:
        """停运平台。"""

        with self._lock:
            op_id = self.ids.next()
            report = shutdown_plant(
                self.source,
                self.network,
                self.secondary,
                self.pump,
                self.valves[sorted(self.valves)[0]],
                reason,
            )
            summary = self._record_operation(op_id, "shutdown", report)
            self._operations += 1
            self._persist()
            return {**report, **summary}

    # ---- 操作编排 -----------------------------------------------------

    def push_weather(self, outdoor_c: float, station: str = "console") -> dict:
        """接收一次气象推送并让热源按新曲线点火。"""

        with self._lock:
            op_id = self.ids.next()
            report = push_weather(
                self.source,
                self.curve,
                WeatherPush(outdoor_c, self.clock.now(), station),
                self.clock.now(),
            )
            summary = self._record_operation(op_id, "weather", report)
            self._operations += 1
            self._persist()
            return {**report, **summary}

    def adjust_source(
        self, temperature_c: float, flow_t_h: float, origin: str = "manual"
    ) -> dict:
        """把热源供温与流量原子地调整到目标值。"""

        with self._lock:
            adjustment: Adjustment = apply_adjustment(self.source, temperature_c, flow_t_h, origin)
            if adjustment.ok:
                self.network.set_supply(adjustment.temperature_c)
                self.network.set_flow(adjustment.flow_t_h)
            self.bus.publish(
                Event.of(
                    "source",
                    "source",
                    "adjust",
                    severity="notice" if adjustment.ok else "warning",
                    message=f"热源调整 {origin}",
                    origin=origin,
                    ok=adjustment.ok,
                    temperature_c=round(adjustment.temperature_c, 2),
                    flow_t_h=round(adjustment.flow_t_h, 2),
                    violations=list(adjustment.violations),
                )
            )
            self._persist()
            return adjustment.as_dict()

    def dispatch(
        self,
        demand_pct: float,
        target_c: float | None = None,
        zones: list[str] | None = None,
        cancel: CancelToken | None = None,
    ) -> dict:
        """按分区下发一次调度。"""

        with self._lock:
            selected = list(zones) if zones else self.zones.names()
            setpoint = self.config.station.setpoint_c if target_c is None else float(target_c)
            op_id = self.ids.next()
            report = dispatch_demand(
                self.service,
                self.mapping,
                self.zones,
                self.network,
                selected,
                demand_pct,
                setpoint,
                cancel,
            )
            summary = self._record_operation(op_id, "dispatch", report)
            self._operations += 1
            self._persist()
            return {**report, **summary}

    def calibrate_flow(self, measured_t_h: float) -> dict:
        """按实测流量重新标定一次网基线。"""

        with self._lock:
            report = recalibrate_flow(self.network, measured_t_h)
            self.bus.publish(
                Event.of(
                    "primary",
                    "primary",
                    "flow_recalibrated",
                    severity="notice",
                    message="一次网流量基线重新标定",
                    **report,
                )
            )
            return report

    def retrofit_baseline(self, target_kpa: float) -> dict:
        """下发一次定压基线改造。"""

        with self._lock:
            report = self.pressure.retrofit(self.secondary, target_kpa)
            self.bus.publish(
                Event.of(
                    "pressure",
                    "pressure",
                    "apply_retrofit",
                    severity="notice" if report["ok"] else "warning",
                    message="定压基线改造" if report["ok"] else "定压基线改造被拒绝",
                    **report,
                )
            )
            return report

    def secondary_cycle(self, dt: float = 1.0) -> dict:
        """跑一个二次网循环（结束前泵一定停稳）。"""

        with self._lock:
            op_id = self.ids.next()
            report = run_cycle(
                self.secondary,
                self.pump,
                self.valves[sorted(self.valves)[0]],
                self.durable,
                dt,
            )
            summary = self._record_operation(op_id, "secondary_cycle", report)
            self._operations += 1
            self._persist()
            return {**report, **summary}

    def start_pump(self, speed_pct: float = 85.0) -> dict:
        """启动循环泵。"""

        with self._lock:
            target = self.pump.start(speed_pct)
            return {"running": self.pump.running(), "target_flow_t_h": round(target, 2)}

    def stop_pump(self) -> dict:
        """停循环泵。"""

        with self._lock:
            self.pump.stop()
            for _ in range(600):
                self.pump.step(1.0)
                if self.pump.idle():
                    break
            return {"running": self.pump.running(), "flow_t_h": round(self.pump.flow_t_h(), 2)}

    def disturb_secondary(self, heat_fraction: float) -> dict:
        """向二次网注入一次热负荷扰动。"""

        with self._lock:
            self.secondary.disturb(heat_fraction)
            self.bus.publish(
                Event.of(
                    "secondary",
                    "secondary",
                    "disturb",
                    severity="warning",
                    message="二次网热负荷扰动",
                    return_c=round(self.secondary.return_c(), 2),
                    disturbances=self.secondary.disturbances(),
                )
            )
            return {
                "return_c": round(self.secondary.return_c(), 2),
                "ceiling_c": round(self.secondary.return_ceiling_c(), 2),
                "disturbances": self.secondary.disturbances(),
            }

    def split_zone(self, source_zone: str, stations: list[str], new_zone: str) -> dict:
        """把一个分区拆成两个，并重建站归属索引。"""

        with self._lock:
            child = self.zones.split(source_zone, stations, new_zone)
            count = self.mapping.rebuild()
            for name in child.stations:
                if name in self.stations:
                    self.stations[name].rezone(child.name)
            self.bus.publish(
                Event.of(
                    "ns",
                    "ns",
                    "zone_split",
                    severity="notice",
                    message=f"分区 {source_zone} 拆出 {child.name}",
                    source_zone=source_zone,
                    new_zone=child.name,
                    stations=list(child.stations),
                    index_entries=count,
                )
            )
            return {
                "ok": True,
                "zone": child.as_dict(),
                "generation": self.mapping.generation(),
                "index": self.mapping.describe()["index"],
            }

    def replay_valves(self, stations: list[str] | None = None) -> dict:
        """按检查点回放阀门指令队列。"""

        with self._lock:
            selected = list(stations) if stations else sorted(self.queues)
            reports = [
                replay_queue(
                    self.valves[name],
                    self.durable,
                    self.queues[name],
                    self.checkpoint,
                )
                for name in selected
            ]
            return {
                "ok": True,
                "queues": reports,
                "replayed": sum(item["replayed"] for item in reports),
                "applied": {item["queue"]: item["applied"] for item in reports},
            }

    def queue_commands(
        self, station: str, targets: list[float], source: str = "dispatch"
    ) -> dict:
        """向某个站的阀门队列写入一组目标开度。"""

        with self._lock:
            queue = self.queues[station]
            commands = [queue.enqueue(target, source) for target in targets]
            return {"station": station, "queued": [command.as_dict() for command in commands]}

    def collect_heat(
        self,
        meter: str,
        delta_mwh: float,
        measured_at: float | None = None,
        window_id: str = "",
    ) -> dict:
        """采集一次热量增量并按表型口径入账。

        增量同时带计量时刻（``measured_at``，缺省取当前时钟）与到达时刻，
        归属由表型口径定死：贸易表按计量时刻、考核表按到达时刻。带
        ``window_id`` 表示断流窗口的恢复补传，走预提冲平路径。
        """

        with self._lock:
            arrived_at = self.clock.now()
            moment = arrived_at if measured_at is None else float(measured_at)
            heat_meter = self.meters.get(meter)
            reading = heat_meter.add(delta_mwh)
            record = self.ledger.carry.intake(
                meter, delta_mwh, moment, arrived_at, window_id=window_id
            )
            self._persist_carry()
            self.bus.publish(
                Event.of(
                    "quota",
                    "quota",
                    "heat_intake",
                    asset=meter,
                    severity="notice",
                    message=(
                        f"热量增量到账 {record.station} "
                        f"({policy.kind_label(heat_meter.kind)}/"
                        f"{'计量时刻' if record.policy == 'measured' else '到达时刻'})"
                    ),
                    meter=meter,
                    station=record.station,
                    kind=heat_meter.kind,
                    policy=record.policy,
                    delta_mwh=round(float(delta_mwh), 4),
                    measured_at=round(moment, 3),
                    arrived_at=round(arrived_at, 3),
                    attributed_period=record.attributed_period,
                    window_id=window_id,
                    recovery=bool(window_id),
                )
            )
            self.trend.append("heat_mwh", arrived_at, self.meters.total_mwh())
            return {
                "meter": meter,
                "station": record.station,
                "kind": heat_meter.kind,
                "policy": record.policy,
                "delta_mwh": round(float(delta_mwh), 4),
                "measured_at": round(moment, 3),
                "arrived_at": round(arrived_at, 3),
                "attributed_period": record.attributed_period,
                "reading_mwh": round(reading, 4),
                "total_mwh": round(self.meters.total_mwh(), 4),
                "recovery": bool(window_id),
            }

    def allocate_heat(self, station: str, delta_mwh: float, origin: str = "console") -> dict:
        """站级手工补录（无表增量）：直接记入本期分站账并落审计。

        用于站点报量等不经过热量表的补录路径；能量同样进入守恒口径，
        不能成为采集与结算之外的对账黑洞。
        """

        with self._lock:
            value = max(0.0, float(delta_mwh))
            total = self.ledger.allocate(station, value)
            self._persist_carry()
            self.bus.publish(
                Event.of(
                    "quota",
                    "quota",
                    "heat_allocated",
                    asset=station,
                    severity="notice",
                    message=f"站级补录 {station} +{value} MWh",
                    station=station,
                    delta_mwh=round(value, 4),
                    period=self.ledger.carry.period(),
                    origin=origin,
                )
            )
            return {
                "station": station,
                "delta_mwh": round(value, 4),
                "allocated_mwh": round(total, 4),
                "period": self.ledger.carry.period(),
            }

    def declare_meter_outage(
        self,
        meter: str,
        estimated_mwh: float,
        *,
        started_at: float | None = None,
        window_id: str = "",
        note: str = "collection_outage",
        station: str = "console",
    ) -> dict:
        """申报某块表结算期采集断流（部分失败），缺量按预估值挂账而不是当零。

        贸易表挂预提并计入本期费用；考核表只挂账跟踪，恢复补传顺延到到账期。
        预估量必须为正——报零等于把断流当作零。
        """

        with self._lock:
            declared_at = self.clock.now()
            start = declared_at if started_at is None else float(started_at)
            heat_meter = self.meters.get(meter)
            try:
                entry = self.ledger.carry.declare_outage(
                    meter,
                    estimated_mwh,
                    declared_at,
                    started_at=start,
                    window_id=window_id,
                    note=note,
                )
            except CarryError as error:
                raise PlantError(str(error), component="quota") from error
            self._persist_carry()
            self.bus.publish(
                Event.of(
                    "quota",
                    "quota",
                    "collection_outage",
                    asset=meter,
                    severity="alarm",
                    message=(
                        f"采集断流挂账 {entry.station}：预估缺口 {estimated_mwh} MWh，"
                        "属部分失败，缺量不当零"
                    ),
                    meter=meter,
                    station=entry.station,
                    kind=heat_meter.kind,
                    window_id=entry.window_id,
                    estimated_mwh=round(float(estimated_mwh), 4),
                    billable=entry.billable,
                    declared_by=station,
                    note=note,
                )
            )
            return {
                "ok": True,
                "entry": entry.as_dict(),
                "meter": meter,
                "kind": heat_meter.kind,
            }

    def settle_quota(self, boundary_at: float | None = None, quota_mwh: float | None = None) -> dict:
        """结束本期配额：先冲平往期结转，再结本期，结转单一并返回并落审计。"""

        with self._lock:
            boundary = self.clock.now() if boundary_at is None else float(boundary_at)
            cap = self.ledger.quota_mwh if quota_mwh is None else float(quota_mwh)
            try:
                settled = self.ledger.settle(boundary_at=boundary, quota_mwh=cap)
            except CarryError as error:
                raise PlantError(str(error), component="quota") from error
            self._persist_carry()
            sheet = settled["carry_sheet"]
            self.bus.publish(
                Event.of(
                    "quota",
                    "quota",
                    "quota_settled",
                    severity="warning" if settled["status"] == "partial" else "notice",
                    message=(
                        f"第 {settled['period']} 期结算：{settled['status']}，"
                        f"新开结转 {sheet['opened_count']} 条、冲平 {sheet['closed_count']} 条、"
                        f"滚动 {sheet['rolled_count']} 条"
                    ),
                    period=settled["period"],
                    boundary_at=settled["boundary_at"],
                    status=settled["status"],
                    total_mwh=settled["total_mwh"],
                    opened_count=sheet["opened_count"],
                    closed_count=sheet["closed_count"],
                    rolled_count=sheet["rolled_count"],
                    carry_sheet=sheet,
                    reconcile=settled["reconcile"],
                )
            )
            return settled

    def trip(self, reason: str = "overtemperature") -> dict:
        """让热源停机。"""

        with self._lock:
            state = self.source.trip(reason)
            self.bus.publish(
                Event.of(
                    "source",
                    "source",
                    "trip",
                    severity="alarm",
                    message=f"热源停机：{reason}",
                    reason=reason,
                    state=state,
                    temperature_c=round(self.source.temperature_c(), 2),
                )
            )
            return {"ok": True, "state": state, "reason": reason, "trips": self.source.trips()}

    def reset_source(self, operator: str = "shift-lead") -> dict:
        """复位热源停机。"""

        with self._lock:
            state = self.source.reset()
            self.bus.publish(
                Event.of(
                    "source",
                    "source",
                    "reset",
                    severity="notice",
                    message=f"{operator} 复位热源",
                    state=state,
                    operator=operator,
                )
            )
            return {"ok": True, "state": state, "operator": operator}

    # ---- 控制循环 -----------------------------------------------------

    def cycle(self, dt: float = 1.0) -> dict:
        """推进一个控制周期，返回本周期摘要。"""

        if dt <= 0:
            raise ValueError("步长必须为正")
        with self._lock:
            self._cycles += 1
            self.clock.sleep(dt)
            moment = self.clock.now()
            flow = self.source.flow_t_h() or self.network.flow_t_h()
            report = advance_heating(
                self.source, self.curve, self.network, self.secondary, flow, moment, dt
            )
            self.trend.append("supply_c", moment, self.source.temperature_c())
            self.trend.append("return_c", moment, self.secondary.return_c())
            self.trend.append("network_flow_t_h", moment, self.network.flow_t_h())
            self.trend.append("pressure_kpa", moment, self.secondary.pressure_kpa())
            self._persist()
            return {"cycle": self._cycles, **report}

    def concurrency_probe(self, rounds: int = 24) -> dict:
        """用真实并行调用探测热源调整与热量采集的原子性。

        两路写方各持一组"温度 + 流量"组合反复调整热源，采样线程同步读取；
        另一组线程并发累加同一块热量表。探测读的是最终状态：热源必须始终落在
        某一组合内（不能出现跨组的混合值），热量表的总量必须等于投递的增量之和。
        """

        if rounds < 1:
            raise ValueError("探测轮数必须为正")
        pairs = ((95.0, 2000.0), (85.0, 3000.0))
        start = self.source.snapshot()
        baseline_pair = (start.temperature_c, start.flow_t_h)
        observed: list[tuple[float, float]] = []
        stop = threading.Event()

        def writer(temperature_c: float, flow_t_h: float) -> None:
            for _ in range(rounds):
                self.source.adjust(temperature_c, flow_t_h)

        def sampler() -> None:
            while not stop.is_set():
                snapshot = self.source.snapshot()
                observed.append((snapshot.temperature_c, snapshot.flow_t_h))

        writers = [threading.Thread(target=writer, args=pair) for pair in pairs]
        watcher = threading.Thread(target=sampler)
        watcher.start()
        for thread in writers:
            thread.start()
        for thread in writers:
            thread.join()
        stop.set()
        watcher.join()

        def consistent(pair: tuple[float, float]) -> bool:
            return any(
                abs(pair[0] - t) < 1e-9 and abs(pair[1] - f) < 1e-9
                for t, f in (*pairs, baseline_pair)
            )

        mixed = [pair for pair in observed if not consistent(pair)]

        meter = self.meters.meters()[0]
        before = self.meters.get(meter).total_mwh()
        increments = rounds * 2

        def collector() -> None:
            for _ in range(rounds):
                self.meters.collect(meter, 1.0)

        threads = [threading.Thread(target=collector) for _ in range(2)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()
        after = self.meters.get(meter).total_mwh()
        expected = increments
        delta = after - before

        return {
            "rounds": rounds,
            "source": {
                "observed": len(observed),
                "mixed_pairs": len(mixed),
                "final": [round(observed[-1][0], 2), round(observed[-1][1], 2)] if observed else [],
                "pairs": [list(pair) for pair in pairs],
                "baseline_pair": [round(baseline_pair[0], 2), round(baseline_pair[1], 2)],
            },
            "meter": {
                "meter": meter,
                "increments": expected,
                "delta_mwh": round(delta, 4),
                "lost_increments": round(expected - delta, 4),
            },
            "ok": not mixed and abs(delta - expected) < 1e-9,
        }

    # ---- 查询 ---------------------------------------------------------

    def meter_names(self) -> list[str]:
        """返回全部热量表名。"""

        return self.meters.meters()

    def curve_report(self) -> dict:
        """返回供热曲线与新鲜度报告。"""

        snapshot = self.curve.snapshot(self.clock.now())
        payload = snapshot.as_dict()
        payload["range"] = [self.config.curve.outdoor_min_c, self.config.curve.outdoor_max_c]
        return payload

    def outdoor_forecast(self, outdoor_c: float | None = None, steps: int = 5) -> dict:
        """按当前（或给定）室外温度外推未来几档供温。"""

        outside = self.curve.outdoor_c() if outdoor_c is None else float(outdoor_c)
        points = build_forecast(self.curve.table, outside, steps=steps)
        return {"start_outdoor_c": round(outside, 2), **describe_forecast(points)}

    def pressure_report(self) -> dict:
        """返回定压基线与二次网定压报告。"""

        payload = self.pressure.baseline.snapshot().as_dict()
        payload["pressure_kpa"] = round(self.secondary.pressure_kpa(), 2)
        payload["settles"] = self.pressure.settles()
        payload["limit"] = [
            self.config.pressure.baseline_min_kpa,
            self.config.pressure.baseline_max_kpa,
        ]
        return payload

    def quota_report(self) -> dict:
        """返回热量表与配额总账报告（含跨期结转视图）。"""

        carry = self.ledger.carry
        latest = carry.latest_sheet()
        return {
            "ledger": self.ledger.snapshot().as_dict(),
            "accumulator": self.meters.describe(),
            "periods": self.ledger.periods(),
            "current_period": carry.period(),
            "quota_mwh": round(self.ledger.quota_mwh, 4),
            "boundaries": [round(value, 3) for value in carry.boundaries()],
            "open_carry": [entry.as_dict() for entry in carry.open_entries()],
            "latest_carry_sheet": latest.as_dict() if latest else None,
            "reconcile": carry.reconcile(),
        }

    def carry_sheets(self, period: int | None = None) -> dict:
        """返回结转单（可按期过滤），供控制台展示。"""

        carry = self.ledger.carry
        sheets = [sheet for sheet in carry.sheets() if period is None or sheet.period == period]
        return {
            "count": len(sheets),
            "current_period": carry.period(),
            "sheets": [sheet.as_dict() for sheet in sheets],
        }

    def reconcile_quota(self) -> dict:
        """校验结转单、分站账与到账流水三账一致。"""

        return self.ledger.carry.reconcile()

    def billing(self, quota_mwh: float | None = None, period: int | None = None) -> list[dict]:
        """按站生成配额结算行（未结算期包含断流预提视角）。"""

        cap = self.ledger.quota_mwh if quota_mwh is None else float(quota_mwh)
        return billing_lines(self.ledger.carry, cap, period)

    def plan_preview(self, demand_pct: float) -> list[dict]:
        """预览一次调度计划（不触碰设备）。"""

        plan = build_plan(self.mapping, self.zones, self.zones.names(), demand_pct)
        flows = batch_flows(self.network, plan)
        self.network.set_flow(sum(flow.flow_t_h for flow in flows))
        return [flow.as_dict() for flow in flows]

    def status(self) -> dict:
        """汇总状态快照，供控制台与命令行读取。"""

        return {
            "platform": self.config.name,
            "unit": self.unit,
            "operations": self._operations,
            "cycles": self._cycles,
            "now": round(self.clock.now(), 3),
            "utc": self.clock.utc_iso(),
            "source": {
                **self.source.snapshot().as_dict(),
                "limits": [self.config.source.flow_min_t_h, self.config.source.flow_max_t_h],
                "quality_confirmed": self.source.quality_confirmed(),
                "trips": self.source.trips(),
            },
            "curve": self.curve_report(),
            "primary": {
                **self.network.snapshot().as_dict(),
                "deviation_t_h": round(self.network.deviation_t_h(), 2),
            },
            "secondary": self.secondary.snapshot().as_dict(),
            "pressure": self.pressure_report(),
            "pump": {**self.pump.snapshot().as_dict(), "curve": self.pump.curve.describe()},
            "stations": {
                "count": len(self.stations),
                "service": {
                    "batches": self.service.batches(),
                    "aborted": self.service.aborted(),
                },
                "detail": {
                    name: {
                        **self.stations[name].snapshot().as_dict(),
                        "valve": self.valves[name].snapshot().as_dict(),
                        "queued": len(self.queues[name].commands()),
                        "applied": self.checkpoint.load(name),
                    }
                    for name in sorted(self.stations)
                },
            },
            "zones": self.zones.describe(),
            "mapping": self.mapping.describe(),
            "quota": self.quota_report(),
            "store": self.metrics.summary(),
            "audit": self.audit.summary(),
            "ids_issued": self.ids.issued(),
        }

    def snapshot(self) -> dict:
        """返回只读的整体快照（状态 + 拓扑 + 命名空间 + 分区 + 趋势）。"""

        return {
            "status": self.status(),
            "topology": self.topology.summary(),
            "namespaces": self.namespaces.describe(),
            "zones": self.zones.describe(),
            "mapping": self.mapping.describe(),
            "trend": self.trend.snapshot(),
            "alarms_recent": self.audit.alarms()[-10:],
            "billing": self.billing(),
        }

    def note_error(self, error: PlantError) -> dict:
        """把平台异常写入审计，返回落盘记录。"""

        payload = error.as_record()
        record = self.audit.record(
            AuditRecord.create(
                self.clock.now(),
                "",
                payload["component"],
                "error",
                severity="warning",
                message=payload["message"],
            )
        )
        return record

    def reset_state(self) -> bool:
        """清掉落盘的平台计数（交付前重新标定用）。"""

        if not self.store.exists("platform_state"):
            return False
        return self.store.remove("platform_state")

    def reset_quota_carry(self) -> bool:
        """清空跨期结转的内存与落盘状态（演示/标定基线用）。

        生产财务结转不能走这里；它只在需要"从第 1 期重新演示"时调用。
        """

        removed = self.store.exists("quota_carry")
        if removed:
            self.store.remove("quota_carry")
        self.ledger.reset_carry()
        return removed


def build_platform(
    config: PlatformConfig | None = None,
    clock: Clock | None = None,
    storage_root: str | None = None,
    unit: str = DEFAULT_UNIT,
) -> Platform:
    """构造一个平台；``storage_root`` 可覆盖配置里的存储目录。"""

    base = config or default_config()
    if storage_root is not None:
        base = dataclasses.replace(base, storage_root=storage_root)
    return Platform(base, clock=clock, unit=unit)
