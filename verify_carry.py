"""跨期结转规则验证（不依赖 pytest，标准库断言）。"""
import sys, tempfile, os
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from heatnet.config import default_config
from heatnet.app import build_platform
from heatnet.clock import ManualClock
from heatnet.quota.carryover import CarryBook, CarryError
from heatnet.quota import policy
from heatnet.quota.billing import billing_lines, BASE_RATE

failures = []

def check(name, cond, detail=""):
    status = "ok  " if cond else "FAIL"
    print(f"[{status}] {name}" + (f" :: {detail}" if detail and not cond else ""))
    if not cond:
        failures.append(name)

# ---------- 1. 表型 -> 口径定死 ----------
check("trade 表按计量时刻", policy.policy_for("trade") == "measured")
check("run 表按到达时刻", policy.policy_for("run") == "arrival")
try:
    policy.normalize_kind("bogus"); check("非法表型拒绝", False)
except ValueError:
    check("非法表型拒绝", True)

# ---------- 2. 直接用 CarryBook 跑完整跨期场景 ----------
book = CarryBook()
book.register_meter("st01.heat-meter", "st01", "trade")
book.register_meter("st02.heat-meter", "st02", "trade")
book.register_meter("st06.heat-meter", "st06", "run")

T0 = 1_758_000_000.0
B1 = T0 + 86400.0
B2 = T0 + 2 * 86400.0
CAP = 1000.0

def rate_for(period, station, mwh):
    from heatnet.quota.billing import charge_for
    return charge_for(mwh, CAP)


def safe_zero_outage(b, meter="st02.heat-meter"):
    try:
        b.declare_outage(meter, 0.0, B1, window_id="zero")
        return None
    except CarryError:
        return "rejected"

# P1 正常到账：st01=100, st02=50, st06=20
def arrive(meter, station, delta, measured, arrived, window=""):
    return book.intake(meter, delta, measured, arrived, window_id=window)
arrive("st01.heat-meter", "st01", 100.0, T0 + 100, T0 + 100)
arrive("st02.heat-meter", "st02", 50.0, T0 + 100, T0 + 100)
arrive("st06.heat-meter", "st06", 20.0, T0 + 100, T0 + 100)

# 断流申报：st01 贸易表缺口 30（预提计费）；st06 考核表缺口 10（只挂账）
e_trade = book.declare_outage("st01.heat-meter", 30.0, B1 - 10, window_id="g1")
e_run = book.declare_outage("st06.heat-meter", 10.0, B1 - 10, window_id="g2")
check("断流预提必须为正", isinstance(safe_zero_outage(book), str))

# 结算前预览：st01 计费量含预提 = 130
preview = {l["station"]: l for l in billing_lines(book, CAP)}
check("预览含贸易表预提", abs(preview["st01"]["mwh"] - 130.0) < 1e-9, preview["st01"])
check("预览考核表不计提", abs(preview["st06"]["mwh"] - 20.0) < 1e-9, preview["st06"])

# P1 结算
s1 = book.settle(B1, rate_for)
sheet1 = s1["sheet"]
check("P1 有滚动条目 => partial", s1["status"] == "partial", s1["status"])
opened_ids = {e.entry_id for e in sheet1.opened}
rolled_ids = {e.entry_id for e in sheet1.rolled}
check("两个断流条目开立", len(sheet1.opened) == 2)
check("两个断流条目滚动", len(sheet1.rolled) == 2)
check("预提条目 billable", e_trade.billable is True)
check("考核表挂账不 billable", e_run.billable is False)
books1 = {b.station: b for b in sheet1.station_books}
check("st01 P1 计费=100+30 预提", abs(books1["st01"].billed_mwh - 130.0) < 1e-9,
      books1["st01"])
check("st06 P1 计费=20（缺口不计提）", abs(books1["st06"].billed_mwh - 20.0) < 1e-9)
check("st01 预提费用=130*32", abs(books1["st01"].charge - 130 * BASE_RATE) < 1e-6)
check("P1 守恒", book.reconcile()["ok"], book.reconcile())

# P2：补传恢复（两个窗口，实际量都等于预估 30 / 10），另加 st02 迟到量 40（P1 计量）
arrive("st01.heat-meter", "st01", 30.0, B1 - 5, B1 + 200, window="g1")
arrive("st06.heat-meter", "st06", 10.0, B1 - 5, B1 + 200, window="g2")
late = arrive("st02.heat-meter", "st02", 40.0, B1 - 5, B1 + 300)
check("迟到量归属期=1", late.attributed_period == 1, late)
# P2 还有正常到账
arrive("st01.heat-meter", "st01", 80.0, B1 + 400, B1 + 400)
arrive("st06.heat-meter", "st06", 15.0, B1 + 400, B1 + 400)

s2 = book.settle(B2, rate_for)
sheet2 = s2["sheet"]
check("P2 无滚动 => complete", s2["status"] == "complete", s2["status"])
closed_types = [e.entry_type for e in sheet2.closed]
check("冲平含两断流+迟到+前期调整",
      closed_types.count("outage_measured") == 1
      and closed_types.count("outage_arrival") == 1
      and closed_types.count("late_measured") == 1
      and closed_types.count("prior_adjustment") == 1,
      closed_types)
books2 = {b.station: b for b in sheet2.station_books}
# st01 P2：正常 80，恢复 30 先进本期再冲减，无差额 => billed=80
check("st01 P2：current=80+30恢复、冲减30 => 80", abs(books2["st01"].billed_mwh - 80.0) < 1e-9, books2["st01"])
check("st01 P2 current 含恢复 110", abs(books2["st01"].current_mwh - 110.0) < 1e-9)
# st06 P2：恢复 10 按到达时刻进本期 + 15 正常 = 25
check("st06 P2 恢复顺延=25", abs(books2["st06"].billed_mwh - 25.0) < 1e-9, books2["st06"])
# st02 P2：迟到 40 冲减；前期调整 40 落回 P1，P2 本身为 0
check("st02 P2=0（冲减40）", abs(books2["st02"].billed_mwh - 0.0) < 1e-9, books2["st02"])
# P1 的 st02 应被追加 adjustment=40
p1_books = {b.station: b for b in book.station_books(1)}
check("st02 P1 追加前期调整 40", abs(p1_books["st02"].adjustment_mwh - 40.0) < 1e-9,
      p1_books["st02"])
check("P2 三账一致", book.reconcile()["ok"], book.reconcile())

# 全周期能量守恒：到账 100+50+20+30+10+40+80+15 = 345
# 计费：P1 st01=130 st02=50(+40 调整=90) st06=20；P2 st01=80 st02=0 st06=25
# => 240 + 105 = 345，挂账净额为 0。
r = book.reconcile()
check("最终守恒数值", abs(r["logged_mwh"] - 345.0) < 1e-6, r)
check("最终计费=345", abs(r["billed_mwh"] - 345.0) < 1e-6, r)

# ---------- 3. 断流未恢复：继续滚动，不能当零，且不许报零 ----------
book2 = CarryBook()
book2.register_meter("m.heat-meter", "m", "trade")
book2.declare_outage("m.heat-meter", 12.0, B1 - 1, window_id="h1")
sa = book2.settle(B1, rate_for)
check("未恢复 P1 partial", sa["status"] == "partial")
# P2 仍不补传 -> 再结算，条目仍 open
sb = book2.settle(B2, rate_for)
check("未恢复条目继续 open", len(book2.open_entries()) == 1)
check("滚动条目不二次预提",
      abs(sum(b.accrued_mwh for b in sb["sheet"].station_books)) < 1e-9,
      sb["sheet"].station_books)


# ---------- 4. 时序倒挂 / 重复窗口拒绝 ----------
book3 = CarryBook()
book3.register_meter("x.heat-meter", "x", "trade")
try:
    book3.intake("x.heat-meter", 1.0, B1 + 100, B1); check("计量晚于到达拒绝", False)
except CarryError:
    check("计量晚于到达拒绝", True)
book3.declare_outage("x.heat-meter", 5.0, B1 - 1, window_id="dup")
try:
    book3.declare_outage("x.heat-meter", 5.0, B1 - 1, window_id="dup2")
    check("未关闭窗口重复申报拒绝", False)
except CarryError:
    check("未关闭窗口重复申报拒绝", True)
try:
    book3.intake("x.heat-meter", 1.0, B1 - 2, B1 - 2, window_id="other")
    check("窗口不匹配拒绝", False)
except CarryError:
    check("窗口不匹配拒绝", True)

# ---------- 5. 平台端到端：API / 审计 / 持久化 / 控制台展示 ----------
tmp = tempfile.mkdtemp()
clock = ManualClock()
platform = build_platform(default_config(), clock=clock, storage_root=tmp)
check("st06 是考核表", platform.meters.get("st06.heat-meter").kind == "run")
b1 = clock.now()
r1 = platform.collect_heat("st01.heat-meter", 60.0)
r6 = platform.collect_heat("st06.heat-meter", 60.0)
check("trade 到账归属当期", r1["attributed_period"] == 1)
out = platform.declare_meter_outage("st01.heat-meter", 25.0, window_id="p1", note="test")
check("平台断流挂账", out["entry"]["mwh"] == 25.0)
settled = platform.settle_quota(boundary_at=b1 + 1000)
check("P1 partial（有滚动）", settled["status"] == "partial", settled["status"])
check("结算返回结转单", settled["carry_sheet"]["period"] == 1)
check("审计含 quota_settled/collection_outage",
      platform.audit.count("quota_settled") == 1
      and platform.audit.count("collection_outage") == 1)
# 控制台展示
sheets = platform.carry_sheets()
check("控制台可取结转单", sheets["count"] == 1 and sheets["sheets"][0]["rolled_count"] == 1)
# 恢复 + 迟到
clock.advance(90000)
platform.collect_heat("st01.heat-meter", 25.0, measured_at=b1 + 500, window_id="p1")
late_p = platform.collect_heat("st02.heat-meter", 15.0, measured_at=b1 + 500)
b2 = clock.now()
settled2 = platform.settle_quota(boundary_at=b2)
check("P2 complete", settled2["status"] == "complete", settled2["status"])
check("迟到量到账冲 P2", late_p["attributed_period"] == 1)
check("平台三账一致", platform.reconcile_quota()["ok"], platform.reconcile_quota())
# 重启恢复
from heatnet.app import Platform as P2mod  # noqa
platform_b = build_platform(default_config(), clock=clock, storage_root=tmp)
rec = platform_b.reconcile_quota()
check("重启后结转恢复且一致", rec["ok"] and platform_b.ledger.periods() == 2, rec)
check("重启后期次=3", platform_b.ledger.carry.period() == 3)
check("重启后结转单两张", platform_b.carry_sheets()["count"] == 2)

print()
if failures:
    print(f"{len(failures)} FAILURES: {failures}")
    sys.exit(1)
print("ALL CHECKS PASSED")
