"""麦门时序套利引擎 —— 资产快照与时间价值评分核心。

设计原则：纯函数、无网络、无 MCP 依赖。
所有来自麦当劳 MCP 的原始返回先被归一化为 Asset / Campaign，
再由本模块计算"过期衰减""最佳行动日""错失成本"。

这样做的目的：核心算法可以在没有 MCP Token 的情况下用离线
样例数据完整验证，MCP 只负责喂数据。
"""

from __future__ import annotations

import json
import math
from dataclasses import dataclass, field, asdict
from datetime import date, datetime, timedelta
from typing import Any, Iterable

# ---------------------------------------------------------------- 资产模型


@dataclass
class Asset:
    """一张券 / 一次抽奖机会 / 一项活动加成，统一为可量化的时间资产。

    face_value 为面值（元）；kind 为 coupon / lottery / bonus。
    expires 为失效日期（含当日可用的按当日计，调用方负责约定）。
    """

    asset_id: str
    kind: str
    title: str
    face_value: float
    acquired: date
    expires: date
    source: str = ""
    note: str = ""
    stackable: bool = True

    def days_left(self, today: date) -> int:
        return (self.expires - today).days

    def is_expired(self, today: date) -> bool:
        return self.expires < today


@dataclass
class Campaign:
    """活动日历中的一档活动，作为"当日下单可叠加的额外收益"。"""

    campaign_id: str
    title: str
    start: date
    end: date
    estimated_gain: float = 0.0
    desc: str = ""

    def covers(self, d: date) -> bool:
        return self.start <= d <= self.end

    def days_left(self, today: date) -> int:
        return (self.end - today).days


# ---------------------------------------------------------------- 衰减模型

# ------------------------------------------------------------ 模型参数说明
#
# 本模块严格区分两个独立的量，切勿混淆：
#
#   urgency —— 过期压力。回答“现在要不要动”。从到期日反向指数增长，
#              只用于排序提醒强度，不参与任何金额计算。
#
#   decay   —— 兑现概率 P(这张券最终真的被用掉)。回答“这块钱能否留住”。
#              这**不是**“价值折损”：券不是慢慢贬值的，它是悬崖式归零，
#              今天值 ¥45、明天值 ¥0。所以 decay 建模的是行为概率——
#              剩余天数越多，越不可能被遗忘，故随剩余天数单调递增并饱和。

# 压力衰减率：λ = 1/7，即每过一天压力乘以 e^(-1/7) ≈ 0.867，
# 7 天后降至 1/e ≈ 0.368，21 天降至 0.05。
# 该取值让压力在有效视野内平滑铺开——初版取 ln(50)/21≈1.89 时，
# 1 天就掉到 0.15、3 天归 0.007，视野形同坍缩，
# 导致「本周内」与「今天」无法区分。平滑衰减才符合真实决策节奏。
URGENCY_RATE = 1.0 / 7.0
URGENCY_SATURATION_DAYS = 21.0

# 兑现概率的双端锚点：
#   P_MIN —— 今天到期：时间仍充裕，但确实存在忘掉的尾部风险
#   P_MAX —— 远期券上限：仍留出“彻底遗忘”的风险，不取 1.0
P_MIN = 0.68
P_MAX = 0.93
REDEMPTION_TAU = 8.0  # 天，兑现概率爬升的时间常数


def urgency(asset: Asset, today: date) -> float:
    """过期压力指数：0 ~ 1。随剩余天数减少锐增，超出饱和窗口后归零。

    urgency(0 天) = 1.00，urgency(1 天) ≈ 0.83，urgency(7 天) ≈ 0.27，
    urgency(≥21 天) = 0.0。第一周内具备足够区分度，不会把
    “今天必须用”和“本周内用”判成同一个量级。
    """
    d = asset.days_left(today)
    if d < 0:
        return 1.0
    if d >= URGENCY_SATURATION_DAYS:
        return 0.0
    return round(math.exp(-URGENCY_RATE * d), 6)


def decay(asset: Asset, today: date) -> float:
    """兑现概率：这张券在到期前被真正用掉的概率估计 ∈ [0, P_MAX]。

    单调递增于剩余天数：今日到期 0.68，一周后 ≈0.83，一个月后 ≈0.93。
    """
    d = asset.days_left(today)
    if d < 0:
        return 0.0
    rate = P_MAX - P_MIN
    return round(P_MIN + rate * (1.0 - math.exp(-d / REDEMPTION_TAU)), 6)


def effective_value(asset: Asset, today: date) -> float:
    """兑现期望价值（元）= 面值 × 兑现概率。

    含义：这张券今天“实际能变成钱”的期望值，而非标称面值。
    """
    return round(asset.face_value * decay(asset, today), 4)


def at_risk_value(asset: Asset, today: date) -> float:
    """风险敞口（元）= 面值 × (1 - 兑现概率)。

    这才是“现在不用就真会亏掉”的钱，也是本引擎的核心驱动量。
    """
    return round(asset.face_value * (1.0 - decay(asset, today)), 4)


def missed_cost(asset: Asset, today: date) -> float:
    """错失成本：今天不用，到期将永久损失的面值（元）。"""
    if asset.is_expired(today):
        return 0.0
    return asset.face_value


# ---------------------------------------------------------------- 最佳行动日


def best_action_day(
    assets: Iterable[Asset],
    campaigns: Iterable[Campaign],
    today: date,
    horizon_days: int = 45,
) -> date:
    """在窗口内挑选净收益最高的一天。

    净收益 = 当日可叠加的活动增益
           + 当日仍未失效的资产面值（按压力加权）
           − 在 today..day 之前就已到期的资产面值（必须在此之前用掉，
             否则在候选日当天它们已经归零，属于真实损失）

    第三项是本函数存在的关键：若缺少它，模型会推荐一个
    "活动增益更高、但已让多张临期券白白过期"的日期。
    例如午餐立减 ¥45 今日到期，而周末活动 +¥22 在两天后，
    只看增益会选两天后并丢掉 ¥45——那是净亏损决策。

    仅在"今天到 horizon_days"之间搜索，保证结果是未来可执行的日期。
    """
    assets = [a for a in assets if not a.is_expired(today)]
    campaigns = list(campaigns)
    if not assets:
        return today

    best_day, best_score = today, None
    for offset in range(0, horizon_days + 1):
        day = today + timedelta(days=offset)

        # 候选日当天仍然有效的资产价值（压力越高越应该当天就用掉）
        alive_value = sum(
            a.face_value * (0.25 + urgency(a, today))
            for a in assets
            if a.expires >= day
        )
        # 在 day 之前就归零的资产——必须提前用掉，否则是净损失
        lost_before = sum(
            a.face_value for a in assets if a.expires < day
        )
        campaign_value = sum(c.estimated_gain for c in campaigns if c.covers(day))

        score = alive_value + campaign_value - lost_before
        if best_score is None or score > best_score:
            best_day, best_score = day, score
    return best_day


def action_day_rationale(
    day: date,
    assets: Iterable[Asset],
    campaigns: Iterable[Campaign],
    today: date,
) -> list[str]:
    """生成人类可读的行动日理由。"""
    reasons: list[str] = []
    for c in campaigns:
        if c.covers(day) and c.estimated_gain > 0:
            reasons.append(f"{c.title} 进行中，当日下单可叠加约 ¥{c.estimated_gain:.0f} 增益")

    # 只统计在 day 当天仍然有效的券；已在其之前到期的不能算进收益
    window = [a for a in assets if a.expires >= day and a.days_left(today) <= 14]
    if window:
        total = sum(a.face_value for a in window)
        names = "、".join(a.title for a in window[:3])
        reasons.append(
            f"该日仍有 {len(window)} 张券有效（合计 ¥{total:.0f}，如{names}"
            f"{'等' if len(window) > 3 else ''}），当天用掉可避免归零"
        )

    # 若存在必须抢在此日之前用掉的券，明确提示（已过期的不计入）
    urgent = [
        a for a in assets
        if a.expires < day and not a.is_expired(today) and a.days_left(today) <= 7
    ]
    if urgent:
        total = sum(a.face_value for a in urgent)
        names = "、".join(a.title for a in urgent[:3])
        reasons.append(
            f"注意：{names}{'等' if len(urgent) > 3 else ''}（合计 ¥{total:.0f}）"
            f"会在该日之前到期，需更早使用"
        )

    return reasons or ["窗口内无显著衰减或叠加因素，取最近可用日"]


# ---------------------------------------------------------------- 汇总报告


@dataclass
class DecisionRow:
    asset_id: str
    kind: str
    title: str
    face_value: float
    expires: str
    days_left: int
    urgency: float
    effective_value: float
    verdict: str
    advice: str
    source: str = ""


def _verdict(days_left: int, value: float, today: date, campaign_gain: float) -> tuple[str, str]:
    """把天数与价值映射成一句人话决策。"""
    if days_left < 0:
        return "已失效", "已过期，本次不再计入"
    if days_left == 0:
        return "今日必用", f"¥{value:.0f} 今日到期，务必今天下单"
    if days_left <= 2:
        bonus = f"，且当日有活动增益约 ¥{campaign_gain:.0f}" if campaign_gain > 0 else ""
        return "紧急", f"仅剩 {days_left} 天，建议今天或明天使用{bonus}"
    if days_left <= 7:
        bonus = f"，叠加活动增益约 ¥{campaign_gain:.0f}" if campaign_gain > 0 else ""
        return "尽快用", f"剩 {days_left} 天，建议本周内使用{bonus}"
    if days_left <= 21:
        return "可择时", f"剩 {days_left} 天，择活动日或最佳行动日使用更划算"
    return "暂缓", f"还有 {days_left} 天，暂不使用，等待更优叠加窗口"


def build_report(
    assets: list[Asset],
    campaigns: list[Campaign],
    today: date,
    horizon_days: int = 45,
) -> dict[str, Any]:
    """生成完整的时间价值决策报告。"""
    active = [a for a in assets if not a.is_expired(today)]
    expired = [a for a in assets if a.is_expired(today)]

    best_day = best_action_day(assets, campaigns, today, horizon_days)
    day_gain = sum(c.estimated_gain for c in campaigns if c.covers(best_day))
    # 只统计在候选日当天仍然有效的券，避免把早已归零的券算进收益
    day_expiring = sum(
        a.face_value for a in active if a.expires >= best_day and a.days_left(today) <= 14
    )
    # 在候选日之前就会归零的资产——必须更早用掉（已过期的本就不在 active 里）
    day_urgent = sum(
        a.face_value for a in active if a.expires < best_day and a.days_left(today) <= 7
    )

    rows: list[DecisionRow] = []
    for a in sorted(assets, key=lambda x: (x.days_left(today), -x.face_value)):
        d = a.days_left(today)
        verdict, advice = _verdict(d, a.face_value, today, day_gain if d >= 0 else 0)
        rows.append(
            DecisionRow(
                asset_id=a.asset_id,
                kind=a.kind,
                title=a.title,
                face_value=a.face_value,
                expires=a.expires.isoformat(),
                days_left=d,
                urgency=urgency(a, today),
                effective_value=effective_value(a, today),
                verdict=verdict,
                advice=advice,
                source=a.source,
            )
        )

    total_face = round(sum(a.face_value for a in active), 2)
    total_effective = round(sum(effective_value(a, today) for a in active), 2)
    total_at_risk = round(sum(at_risk_value(a, today) for a in active), 2)
    total_missed = round(sum(missed_cost(a, today) for a in active), 2)

    return {
        "generated_for": today.isoformat(),
        "horizon_days": horizon_days,
        "best_action_day": best_day.isoformat(),
        "best_action_day_gain": round(day_gain, 2),
        "best_action_day_valid_assets": round(day_expiring, 2),
        "best_action_day_urgent_before": round(day_urgent, 2),
        "rationale": action_day_rationale(best_day, assets, campaigns, today),
        "summary": {
            "asset_count": len(assets),
            "active_count": len(active),
            "expired_count": len(expired),
            "total_face_value": total_face,
            "total_effective_value": total_effective,
            "total_at_risk_value": total_at_risk,
            "total_missed_cost": total_missed,
            "decay_loss": round(total_face - total_effective, 2),
            "immediate_action_today": [
                {"title": a.title, "face_value": a.face_value, "expires": a.expires.isoformat()}
                for a in active
                if a.days_left(today) <= 1
            ],
            "active_campaigns": [
                {"title": c.title, "gain": c.estimated_gain, "end": c.end.isoformat()}
                for c in campaigns
                if c.covers(today)
            ],
            "upcoming_campaigns": [
                {"title": c.title, "gain": c.estimated_gain, "start": c.start.isoformat()}
                for c in campaigns
                if c.start > today
            ],
        },
        "assets": [asdict(r) for r in rows],
    }


# ---------------------------------------------------------------- 归一化


def _parse_date(value: Any, fallback: date | None = None) -> date:
    """尽最大努力从 MCP 返回里解析日期，失败则退回fallback。"""
    if value is None:
        if fallback is None:
            raise ValueError("缺少日期且无兜底值")
        return fallback
    if isinstance(value, date) and not isinstance(value, datetime):
        return value
    if isinstance(value, datetime):
        return value.date()
    text = str(value).strip()
    for fmt in ("%Y-%m-%d", "%Y/%m/%d", "%Y.%m.%d", "%Y-%m-%d %H:%M:%S", "%Y%m%d"):
        try:
            return datetime.strptime(text[: len(fmt) + 6], fmt).date()
        except ValueError:
            continue
    if fallback is not None:
        return fallback
    raise ValueError(f"无法解析日期：{value!r}")


def normalize_coupons(raw: Any, today: date, default_days: int = 7) -> list[Asset]:
    """把 query-my-coupons / query-store-coupons 的返回归一化为 Asset 列表。

    麦当劳 MCP 不同版本字段名略有差异，因此这里做多字段兼容：
    名称兼容 title/name/couponName，金额兼容 faceValue/amount/value/discount。
    """
    items = _iter_items(raw)
    assets: list[Asset] = []
    for i, item in enumerate(items):
        if not isinstance(item, dict):
            continue
        title = _first_str(item, "title", "name", "couponName", "couponTitle", "activityName")
        if not title:
            continue
        value = _first_float(
            item, "faceValue", "face_value", "amount", "value", "discount", "discountAmount"
        )
        expires = _parse_date(
            _first_of(item, "expiresAt", "expireAt", "endTime", "validEndTime", "endDate"),
            fallback=today + timedelta(days=default_days),
        )
        assets.append(
            Asset(
                asset_id=_first_str(item, "couponId", "id", "code") or f"coupon-{i}",
                kind="coupon",
                title=title,
                face_value=value or 0.0,
                acquired=_parse_date(
                    _first_of(item, "startTime", "validStartTime", "beginTime", "startDate"),
                    fallback=today,
                ),
                expires=expires,
                source=_first_str(item, "source", "channel", "scene") or "query-my-coupons",
                note=_first_str(item, "desc", "description", "useDesc"),
                stackable=bool(item.get("stackable", True)),
            )
        )
    return assets


def normalize_campaigns(raw: Any, today: date) -> list[Campaign]:
    """把 campaign-calendar 返回归一化为 Campaign 列表。"""
    items = _iter_items(raw)
    campaigns: list[Campaign] = []
    for i, item in enumerate(items):
        if not isinstance(item, dict):
            continue
        title = _first_str(item, "title", "name", "activityName", "campaignName")
        if not title:
            continue
        start = _parse_date(
            _first_of(item, "startTime", "startDate", "beginTime"), fallback=today
        )
        end = _parse_date(
            _first_of(item, "endTime", "endDate", "finishTime"),
            fallback=today + timedelta(days=3),
        )
        campaigns.append(
            Campaign(
                campaign_id=_first_str(item, "id", "campaignId", "code") or f"campaign-{i}",
                title=title,
                start=start,
                end=end,
                estimated_gain=_first_float(item, "estimatedGain", "gain", "value") or 0.0,
                desc=_first_str(item, "desc", "description"),
            )
        )
    return campaigns


def _iter_items(raw: Any) -> list[Any]:
    """从 MCP 返回的多种包裹结构里挖出列表。

    兼容直接数组、data 数组、result.records 等常见形态。
    """
    if raw is None:
        return []
    if isinstance(raw, list):
        return raw
    if isinstance(raw, dict):
        for key in ("data", "records", "list", "items", "result", "coupons", "campaigns"):
            val = raw.get(key)
            if isinstance(val, list):
                return val
            if isinstance(val, dict):
                nested = _iter_items(val)
                if nested:
                    return nested
    return []


def _first_of(item: dict, *keys: str) -> Any:
    for k in keys:
        if k in item and item[k] not in (None, ""):
            return item[k]
    return None


def _first_str(item: dict, *keys: str) -> str:
    val = _first_of(item, *keys)
    return str(val).strip() if val is not None else ""


def _first_float(item: dict, *keys: str) -> float | None:
    val = _first_of(item, *keys)
    if val is None:
        return None
    try:
        return float(str(val).replace("¥", "").replace(",", "").strip())
    except (TypeError, ValueError):
        return None


def load_snapshot(path: str) -> dict[str, Any]:
    """读取离线样例快照，供无Token 环境下自测。"""
    with open(path, "r", encoding="utf-8") as f:
        return json.load(f)


def analyze(snapshot: dict[str, Any], today: date | None = None) -> dict[str, Any]:
    """从归一化快照直接出报告：{"coupons": [...], "campaigns": [...]}"""
    base = today or date.today()
    coupons = normalize_coupons(snapshot.get("coupons"), base)
    campaigns = normalize_campaigns(snapshot.get("campaigns"), base)
    return build_report(coupons, campaigns, base)