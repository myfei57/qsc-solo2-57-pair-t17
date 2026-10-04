"""表型与跨期归属口径。

增量跨过结算边界时算哪一期，必须由表型定死，采集端与站点都不能在到账时临时
选边，否则同一笔增量会在两期之间重复或落空：

* ``trade`` 贸易结算表按**计量时刻**归属（权责发生制）：计量时刻落在哪期就归哪期。
  结算时刻还没到账的缺口先挂预提进本期账，下一期到账后冲平预提、差额走前期调整；
  没挂预提的迟到增量先冲减到账期，再以前期调整补回计量期。
* ``run`` 运行考核表按**到达时刻**归属（收付实现制）：哪期到账算哪期。断流缺口只
  挂账跟踪，不计提到账期之前的费用；恢复后的补传顺延到到账期正常计费。

两套口径在结算边界前后给出唯一且可复核的归属，不允许出现"站点自己说算哪期"。
"""

from __future__ import annotations

# 表型
KIND_TRADE = "trade"
KIND_RUN = "run"
KINDS = (KIND_TRADE, KIND_RUN)

# 归属口径
POLICY_MEASURED = "measured"
POLICY_ARRIVAL = "arrival"
POLICIES = (POLICY_MEASURED, POLICY_ARRIVAL)

_KIND_POLICY = {
    KIND_TRADE: POLICY_MEASURED,
    KIND_RUN: POLICY_ARRIVAL,
}

_KIND_LABELS = {
    KIND_TRADE: "贸易结算表",
    KIND_RUN: "运行考核表",
}

_POLICY_MOMENT = {
    POLICY_MEASURED: "measured_at",
    POLICY_ARRIVAL: "arrived_at",
}


def normalize_kind(kind: str) -> str:
    """校验表型，非法表型直接拒绝（不允许带着未知口径入账）。"""

    value = str(kind).strip().lower()
    if value not in _KIND_POLICY:
        raise ValueError(f"未知热量表表型: {kind!r}，可选 {sorted(KINDS)}")
    return value


def policy_for(kind: str) -> str:
    """返回表型对应的归属口径。"""

    return _KIND_POLICY[normalize_kind(kind)]


def kind_label(kind: str) -> str:
    """返回表型中文说明。"""

    return _KIND_LABELS[normalize_kind(kind)]


def attributing_moment(policy: str) -> str:
    """返回该口径用于归属判定的时刻字段名（计量时刻或到达时刻）。"""

    if policy not in POLICIES:
        raise ValueError(f"未知归属口径: {policy!r}")
    return _POLICY_MOMENT[policy]
