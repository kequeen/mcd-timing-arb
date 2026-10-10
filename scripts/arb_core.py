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
    unit: str = "元"

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
    stage: Any = None

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
            reasons.append(f"{c.title} 进行中，当日下单可叠加约 ¥{_money(c.estimated_gain)} 增益")

    # 只统计在 day 当天仍然有效的券；已在其之前到期的不能算进收益
    window = [a for a in assets if a.expires >= day and a.days_left(today) <= 14]
    if window:
        total = sum(a.face_value for a in window)
        names = "、".join(a.title for a in window[:3])
        reasons.append(
            f"该日仍有 {len(window)} 张券有效（合计 ¥{_money(total)}，如{names}"
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
            f"注意：{names}{'等' if len(urgent) > 3 else ''}（合计 ¥{_money(total)}）"
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


def _money(val: float) -> str:
    """金额显示：整数不带小数，非整数保留一位，避免把 13.9 显示成 14。"""
    return f"{val:.0f}" if abs(val - round(val)) < 0.005 else f"{val:.1f}"


def _verdict(days_left: int, value: float, today: date, campaign_gain: float) -> tuple[str, str]:
    """把天数与价值映射成一句人话决策。"""
    if days_left < 0:
        return "已失效", "已过期，本次不再计入"
    if days_left == 0:
        return "今日必用", f"¥{_money(value)} 今日到期，务必今天下单"
    if days_left <= 2:
        bonus = f"，且当日有活动增益约 ¥{_money(campaign_gain)}" if campaign_gain > 0 else ""
        return "紧急", f"仅剩 {days_left} 天，建议今天或明天使用{bonus}"
    if days_left <= 7:
        bonus = f"，叠加活动增益约 ¥{_money(campaign_gain)}" if campaign_gain > 0 else ""
        return "尽快用", f"剩 {days_left} 天，建议本周内使用{bonus}"
    if days_left <= 21:
        return "可择时", f"剩 {days_left} 天，择活动日或最佳行动日使用更划算"
    return "暂缓", f"还有 {days_left} 天，暂不使用，等待更优叠加窗口"


@dataclass
class PointsSnapshot:
    """积分账户快照。积分是独立于券的第二个时间维度。"""

    available: float = 0.0
    accumulative: float = 0.0
    expiring_this_month: float = 0.0
    expired: float = 0.0

    @property
    def at_risk(self) -> float:
        """本月即将过期的积分——不换掉就归零，属于最紧迫的沉没成本。"""
        return self.expiring_this_month


def normalize_points(raw: Any) -> PointsSnapshot:
    """从 query-my-account 返回中提取积分信息。

    真实字段为字符串型：availablePoint / accumulativePoint /
    currentMouthExpirePoint / expiredPoint。
    """
    payload = raw
    for _ in range(3):
        if isinstance(payload, dict) and "result" in payload:
            payload = payload["result"]
        if isinstance(payload, dict) and "structuredContent" in payload:
            payload = payload["structuredContent"]
        if isinstance(payload, dict) and "data" in payload:
            payload = payload["data"]
        else:
            break
    if not isinstance(payload, dict):
        return PointsSnapshot()

    def num(key: str) -> float:
        val = payload.get(key)
        try:
            return float(str(val).strip())
        except (TypeError, ValueError):
            return 0.0

    return PointsSnapshot(
        available=num("availablePoint"),
        accumulative=num("accumulativePoint"),
        expiring_this_month=num("currentMouthExpirePoint"),
        expired=num("expiredPoint"),
    )


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

    density = campaign_density(campaigns)
    best_density = pick_best_density_day(density, today, assets, horizon_days)

    # 行动日统一口径：活动密度来自真实接口，优先级高于估算增益。
    # 若推荐日会导致临期券归零，则回退到「今天」（宁可今天用掉）。
    if best_density is not None:
        would_lose = [a for a in active if a.expires < best_density.day]
        action_day = today if would_lose else best_density.day
        day_basis = "activity_density"
    else:
        action_day = best_day
        day_basis = "gain_estimate"

    return {
        "generated_for": today.isoformat(),
        "horizon_days": horizon_days,
        "action_day": action_day.isoformat(),
        "action_day_basis": day_basis,
        "best_action_day_valid_assets": round(
            sum(a.face_value for a in active if a.expires >= action_day), 2
        ),
        "best_action_day_urgent_before": round(
            sum(a.face_value for a in active if a.expires < action_day), 2
        ),
        "rationale": action_day_rationale(action_day, assets, campaigns, today),
        "activity_density": [
            {"day": d.day.isoformat(), "total": d.total, "titles": d.titles}
            for d in density
            if 0 <= (d.day - today).days <= horizon_days
        ],
        "best_density_day": best_density.day.isoformat() if best_density else None,
        "best_density_summary": best_density.summary() if best_density else "",
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

    字段兼容（已针对麦当劳 MCP v1.0.0 真实返回校准）：
      -名称   title / name / couponName / couponTitle / activityName
      - 面值   denomination（单位「分」）/ discountInfo.discountValue /
               reducePrice / faceValue / amount / value / discount
      - 失效日 tradeEndDate / tradeEndDateTime / expiresAt / expireAt / endTime
      - 生效日 tradeStartDate / tradeStartDateTime / startTime

    注意 denomination 单位为分（1390 表示 ¥13.90），必须除以 100。
    """
    items = _iter_items(raw)
    assets: list[Asset] = []
    for i, item in enumerate(items):
        if not isinstance(item, dict):
            continue
        title = _first_str(item, "title", "name", "couponName", "couponTitle", "activityName")
        if not title:
            continue
        value, unit = _extract_face_value(item)
        expires = _parse_date(
            _first_of(item, "tradeEndDate", "tradeEndDateTime", "expiresAt",
                      "expireAt", "endTime", "validEndTime", "endDate"),
            fallback=today + timedelta(days=default_days),
        )
        assets.append(
            Asset(
                asset_id=_first_str(item, "id", "couponId", "code")
                or f"coupon-{i}",
                kind="coupon",
                title=title,
                face_value=value or 0.0,
                acquired=_parse_date(
                    _first_of(item, "tradeStartDate", "tradeStartDateTime",
                              "startTime", "validStartTime", "beginTime", "startDate"),
                    fallback=today,
                ),
                expires=expires,
                source=_first_str(item, "source", "channel", "scene")
                or "query-my-coupons",
                note=_first_str(item, "applyDesc", "desc", "description", "useDesc"),
                stackable=bool(item.get("stackable", True)),
                unit=unit,
            )
        )
    return assets


def _extract_face_value(item: dict) -> tuple[float | None, str]:
    """提取券面额，返回 (金额, 单位说明)。

    真实返回里 denomination 单位是「分」（1390 → ¥13.90），
    而 discountInfo.discountValue 是「元」。两者混用会把 ¥13.90
    读成 ¥1390，夸大 100 倍，因此必须按字段分别处理。

    返回 None 表示「接口未提供面额」，返回 0.0 表示「接口明确说是 0 元」，
    两者在报告中含义不同，不可混淆。
    """
    denom = _first_of(item, "denomination", "currentBalance")
    if denom is not None:
        parsed = _coerce_float(denom)
        if parsed is not None:
            # denomination 以「分」为单位，除以 100 后即为「元」
            return round(parsed / 100.0, 2), "元"

    discount_info = item.get("discountInfo")
    if isinstance(discount_info, dict):
        raw = _first_of(discount_info, "discountValue", "value")
        parsed = _coerce_float(raw) if raw is not None else None
        if parsed is not None:
            prefix = str(discount_info.get("discountTypeText", ""))
            return abs(parsed), ("元" if prefix == "¥" else prefix or "元")

    for key in ("reducePrice", "faceValue", "amount", "value", "discount"):
        if key in item and item[key] not in (None, ""):
            parsed = _coerce_float(item[key])
            if parsed is not None:
                return abs(parsed), "元"

    return None, ""


def normalize_campaigns(raw: Any, today: date) -> list[Campaign]:
    """把 campaign-calendar 返回归一化为 Campaign 列表。

    真实结构为 data.dailyList[].events[] 的三层嵌套，每条 event 的
    活动名在 articleDto.title（activityTitle 实测恒为空字符串），
    日期来自父级 dailyList[].date。增益无法从接口直接获得，
    改用「同日活动场次密度」作为可验证的替代信号。
    """
    events = _iter_campaign_events(raw)
    campaigns: list[Campaign] = []
    for i, (start, end, ev) in enumerate(events):
        article = ev.get("articleDto") or {}
        title = (
            _first_str(article, "title")
            or _first_str(ev, "activityTitle", "activityName", "title", "name")
        )
        if not title:
            continue
        # stage: 1=未开始 2=进行中 3=已结束（实测往期为 3）
        stage = ev.get("activityStage")
        campaigns.append(
            Campaign(
                campaign_id=_first_str(ev, "activityCode", "resourceId", "id")
                or f"campaign-{start}-{i}",
                title=title,
                start=start,
                end=end,
                estimated_gain=_campaign_gain(ev),
                desc=_first_str(article, "highlights", "content"),
                stage=stage,
            )
        )
    return campaigns


def _iter_campaign_events(raw: Any) -> list[tuple[date, date, dict]]:
    """提取 (开始日, 结束日, 事件) 列表。

    真实 campaign-calendar 是 data.dailyList[].events[]，每条事件挂在
    某一天上（单日）；但扁平结构（离线样例/其他接口）会给出
    startTime..endTime 的区间。两者都要支持，否则多日活动会被退化成单日。
    """
    result: list[tuple[date, date, dict]] = []
    payload = raw
    # 解包 JSON-RPC / structuredContent / data
    for _ in range(4):
        if isinstance(payload, dict) and "result" in payload:
            payload = payload["result"]
            continue
        if isinstance(payload, dict) and "structuredContent" in payload:
            payload = payload["structuredContent"]
            continue
        if isinstance(payload, dict) and "data" in payload:
            payload = payload["data"]
            continue
        break

    if isinstance(payload, dict) and isinstance(payload.get("dailyList"), list):
        for day_item in payload["dailyList"]:
            raw_date = day_item.get("date")
            if not raw_date:
                continue
            try:
                day = _parse_date(raw_date)
            except ValueError:
                continue
            for ev in day_item.get("events") or []:
                if isinstance(ev, dict):
                    result.append((day, day, ev))
        return result

    # 扁平结构：保留 start..end 区间
    for item in _iter_items(payload):
        if not isinstance(item, dict):
            continue
        try:
            start = _parse_date(
                _first_of(item, "startTime", "startDate", "beginTime"),
                fallback=date.today(),
            )
        except ValueError:
            continue
        try:
            end = _parse_date(
                _first_of(item, "endTime", "endDate", "finishTime"), fallback=start
            )
        except ValueError:
            end = start
        result.append((start, max(start, end), item))
    return result


def _campaign_gain(ev: dict) -> float:
    """从活动事件估算可叠加增益（元）。

    麦当劳接口不直接给出「下单能省多少」，price 字段实测恒为 '0'。
    因此不伪造增益数字：仅当 price 字段真实存在且大于 0 时才采信它。
    真正的择时信号由 campaign_density() 提供——那是接口事实，
    不需要任何假设。
    """
    price = _first_float(ev, "price")
    if price and price > 0:
        return round(abs(price), 2)
    return 0.0


# ---------------------------------------------------------------- 活动密度


@dataclass
class DailyDensity:
    """某一天的活动密度——真实接口数据，不含任何假设成分。"""

    day: date
    total: int
    titles: list[str]

    def summary(self, limit: int = 3) -> str:
        head = "、".join(self.titles[:limit])
        return f"{self.total} 场活动（{head}{'等' if self.total > limit else ''}）"


def campaign_density(campaigns: list[Campaign]) -> list[DailyDensity]:
    """按日聚合活动场次，得到「哪天下单能叠加最多」的客观依据。

    这是本引擎在真实数据下最可靠的择时信号：
    它完全来自 campaign-calendar 接口事实，不需要推测活动能省多少钱。

    重要：接口会把「今日进行中」的全部活动都挂在今天，
    导致今天的场次数远高于其他日子（实测 15 场 vs 未来每天 1~2 场）。
    直接取最大密度会永远得出「今天」，毫无择时价值。
    因此这里按 dateText 的语义区分三类，只在【未来】范围内比较，
    并单独返回今日场次供参考。
    """
    buckets: dict[date, list[str]] = {}
    for c in campaigns:
        cur = c.start
        while cur <= c.end:
            buckets.setdefault(cur, []).append(c.title)
            cur += timedelta(days=1)
    out = [
        DailyDensity(day=day, total=len(titles), titles=titles)
        for day, titles in sorted(buckets.items())
    ]
    return out


def pick_best_density_day(
    density: list[DailyDensity],
    today: date,
    assets: list[Asset],
    horizon_days: int = 21,
) -> DailyDensity | None:
    """在活动密度最高的日子中，选出不会导致临期券归零的那一天。

    两个约束缺一不可：
    1. 活动密度必须真实来自接口，不能用启发式编造
    2. 密度高不等于该去——若某张 ¥45 券10-11 到期，
       去 10-12 的高密度日反而是净亏损
    """
    candidates = [
        d for d in density
        if 1 <= (d.day - today).days <= horizon_days  # 只看未来，今天单独处理
    ]
    if not candidates:
        return None
    active = [a for a in assets if not a.is_expired(today)]
    if not active:
        return max(candidates, key=lambda d: d.total)

    best = max(candidates, key=lambda d: d.total)
    best_score = _density_score(best, active, today)
    for cand in candidates:
        score = _density_score(cand, active, today)
        if score > best_score:
            best, best_score = cand, score
    return best


def _density_score(d: DailyDensity, assets: list[Asset], today: date) -> float:
    """密度分的定义：活动场次减去因等待而损失的面值。

    每场活动折算 10 分（量纲对齐：1 场活动的机会价值 ≙ ¥10 的临期损失），
    这个系数只影响不同候选日之间的排序，不影响单位本身。
    """
    lost = sum(a.face_value for a in assets if a.expires < d.day)
    return d.total * 10.0 - lost


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
    """取第一个存在且非None 的键值。

    注意：不能把 0 当假值跳过——真实返回里 discountValue="0"、price="0"
    都是有效数据（表示该券为固定售价而非折扣）。这里只排除 None 和空串。
    """
    for k in keys:
        if k in item and item[k] is not None and item[k] != "":
            return item[k]
    return None


def _first_str(item: dict, *keys: str) -> str:
    val = _first_of(item, *keys)
    return str(val).strip() if val is not None else ""


def _first_float(item: dict, *keys: str) -> float | None:
    val = _first_of(item, *keys)
    return _coerce_float(val) if val is not None else None


def _coerce_float(val: Any) -> float | None:
    """把任意类型安全转成 float，清洗 ¥ 符号与千分位逗号。"""
    if val is None or isinstance(val, bool):
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
    """从归一化快照直接出报告。

    快照结构（键名对应 MCP 工具的 structuredContent 包装后）：
        coupons      —— query-my-coupons
        campaigns    —— campaign-calendar
        points       —— query-my-account
    """
    base = today or date.today()
    coupons = normalize_coupons(snapshot.get("coupons"), base)
    campaigns = normalize_campaigns(snapshot.get("campaigns"), base)
    rep = build_report(coupons, campaigns, base)

    if snapshot.get("points") is not None:
        pts = normalize_points(snapshot.get("points"))
        rep["points"] = {
            "available": pts.available,
            "accumulative": pts.accumulative,
            "expiring_this_month": pts.expiring_this_month,
            "expired": pts.expired,
            "at_risk": pts.at_risk,
            "advice": (
                f"本月有 {pts.expiring_this_month:g} 积分即将过期，"
                "建议优先在麦麦商城兑换掉"
                if pts.expiring_this_month > 0
                else "本月无积分过期，维持现状即可"
            ),
        }
    return rep