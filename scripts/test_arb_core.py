"""arb_core 自测：全部使用固定日期与离线样例，不依赖网络与 MCP Token。

运行：python3 scripts/test_arb_core.py
"""

from __future__ import annotations

import sys
from datetime import date, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from arb_core import (  # noqa: E402
    P_MAX,
    P_MIN,
    Asset,
    Campaign,
    _coerce_float,
    _first_of,
    analyze,
    at_risk_value,
    action_day_rationale,
    best_action_day,
    build_report,
    campaign_density,
    normalize_points,
    pick_best_density_day,
    build_report,
    decay,
    effective_value,
    missed_cost,
    normalize_campaigns,
    normalize_coupons,
    urgency,
)

TODAY = date(2026, 10, 10)
FAILED: list[str] = []
PASSED = 0


def check(name: str, cond: bool, detail: str = "") -> None:
    global PASSED
    if cond:
        PASSED += 1
        print(f"  PASS  {name}")
    else:
        FAILED.append(name)
        print(f"  FAIL  {name}  {detail}")


def approx(a: float, b: float, tol: float = 1e-6) -> bool:
    return abs(a - b) <= tol


# ---------------------------------------------------------------- 衰减模型


def test_decay() -> None:
    print("\n[1] 兑现概率（decay）")
    soon = Asset("a", "coupon", "今日到期券", 20.0, TODAY, TODAY)
    week = Asset("b", "coupon", "一周后到期", 20.0, TODAY, TODAY + timedelta(days=7))
    month = Asset("c", "coupon", "一个月后到期", 20.0, TODAY, TODAY + timedelta(days=45))
    dead = Asset("d", "coupon", "已过期", 20.0, TODAY, TODAY - timedelta(days=1))

    check("已过期兑现概率为 0", approx(decay(dead, TODAY), 0.0))
    check("今日到期兑现概率 = P_MIN", approx(decay(soon, TODAY), P_MIN))
    check("远期券兑现概率趋近 P_MAX 且不超过",
          P_MAX - 0.01 <= decay(month, TODAY) <= P_MAX, f"实际 {decay(month, TODAY)}")
    check("兑现概率随到期变远单调递增",
          decay(soon, TODAY) < decay(week, TODAY) < decay(month, TODAY),
          f"{decay(soon, TODAY)} / {decay(week, TODAY)} / {decay(month, TODAY)}")
    check("兑现概率永不为 1（留尾部风险）", decay(month, TODAY) < 1.0)
    check("兑现期望价值 = 面值 × 兑现概率",
          approx(effective_value(week, TODAY), 20.0 * decay(week, TODAY), 1e-4))
    check("已过期券有效价值为 0", approx(effective_value(dead, TODAY), 0.0))
    check("风险敞口 = 面值 - 兑现期望价值",
          approx(at_risk_value(week, TODAY), round(20.0 - effective_value(week, TODAY), 4), 1e-4))
    check("今日到期券仍有大部分价值（悬崖而非折损）",
          effective_value(soon, TODAY) > 20.0 * 0.6,
          f"实际 {effective_value(soon, TODAY)}（券是明天归零，不是今天就贬值）")


def test_urgency() -> None:
    print("\n[2] 过期压力（urgency）")
    soon = Asset("a", "coupon", "", 20.0, TODAY, TODAY)
    week = Asset("b", "coupon", "", 20.0, TODAY, TODAY + timedelta(days=7))
    far = Asset("c", "coupon", "", 20.0, TODAY, TODAY + timedelta(days=60))
    dead = Asset("d", "coupon", "", 20.0, TODAY, TODAY - timedelta(days=1))

    check("已过期压力为 1.0", approx(urgency(dead, TODAY), 1.0))
    check("今日到期压力为 1.0", approx(urgency(soon, TODAY), 1.0))
    check("一周后压力显著低于今日（区分度 > 2.5 倍）",
          urgency(soon, TODAY) > urgency(week, TODAY) * 2.5,
          f"{urgency(soon, TODAY)} vs {urgency(week, TODAY)}")
    check("超饱和窗口压力为 0", approx(urgency(far, TODAY), 0.0))
    check("压力单调：到期越近越大",
          urgency(dead, TODAY) >= urgency(soon, TODAY) >= urgency(week, TODAY) >= urgency(far, TODAY))
    check("压力与兑现概率方向相反（避免两指标共线）",
          urgency(week, TODAY) < decay(week, TODAY))

    print("\n[2b] 压力曲线形状（回归：视野坍缩问题）")
    curve = {
        d: urgency(Asset("x", "coupon", "", 1.0, TODAY, TODAY + timedelta(days=d)), TODAY)
        for d in (0, 1, 2, 3, 5, 7, 10, 14)
    }
    print("       " + "  ".join(f"{d}天={curve[d]:.2f}" for d in curve))
    check("0 天压力为 1.0", approx(curve[0], 1.0))
    check("3 天压力仍在有效区间（未过早坍缩）", 0.55< curve[3] <= 0.75,
          f"实际 {curve[3]}，初版 λ=1.89 时仅 0.003")
    check("7 天压力约 1/e≈0.37", approx(curve[7], 0.3679, 0.01),
          f"实际 {curve[7]}")
    check("14 天压力显著衰减但未归零", 0.05 < curve[14] < 0.25,
          f"实际 {curve[14]}")
    check("压力曲线逐档单调递减",
          all(curve[a] > curve[b] for a, b in ((0, 1), (1, 2), (2, 3), (3, 5), (5, 7), (7, 10), (10, 14))))
    check("相邻档位均有可区分度（无平台期）",
          all(curve[a] - curve[b] > 0.01
              for a, b in ((0, 1), (1, 2), (2, 3), (3, 5), (5, 7), (7, 10), (10, 14))),
          "避免多档天数被压成同一量级")


def test_missed_cost() -> None:
    print("\n[3] 错失成本")
    alive = Asset("a", "coupon", "", 30.0, TODAY, TODAY + timedelta(days=3))
    dead = Asset("b", "coupon", "", 30.0, TODAY, TODAY - timedelta(days=2))
    check("有效券错失成本 = 面值", approx(missed_cost(alive, TODAY), 30.0))
    check("已过期券错失成本为 0", approx(missed_cost(dead, TODAY), 0.0))


# ---------------------------------------------------------------- 最佳行动日


def test_best_action_day_picks_campaign_day() -> None:
    print("\n[4] 最佳行动日")
    # 券还有60 天才过期，等待两天后的高增益活动是安全的
    assets = [Asset("a", "coupon", "长周期券", 20.0, TODAY, TODAY + timedelta(days=60))]
    campaign = Campaign(
        "c1", "周三买赠", TODAY + timedelta(days=3), TODAY + timedelta(days=3), 45.0
    )
    chosen = best_action_day(assets, [campaign], TODAY)
    check("券尚未临期时应选中高增益活动当天",
          chosen == TODAY + timedelta(days=3), f"实际 {chosen}")

    no_campaign = best_action_day(assets, [], TODAY)
    check("无活动时退化为今天", no_campaign == TODAY)

    empty = best_action_day([], [campaign], TODAY)
    check("无资产时返回今天", empty == TODAY)


def test_best_action_day_avoids_trap() -> None:
    """回归测试：曾经的真实 bug。

    场景：¥45 券今日到期，而两天后有 +¥22 的活动。
    旧模型只看活动增益，会推荐两天后 thereby 让 ¥45 归零——净亏损决策。
    修正后的模型必须识别出这一点，推荐今天或明天。
    """
    print("\n[4b] 最佳行动日：不被高增益活动迷惑")
    assets = [Asset("a", "coupon", "今日到期大额券", 45.0, TODAY, TODAY)]
    trap = Campaign(
        "c1", "周末加赠", TODAY + timedelta(days=2), TODAY + timedelta(days=2), 22.0
    )
    chosen = best_action_day(assets, [trap], TODAY)
    check("不为+¥22 活动放弃 ¥45 临期券", chosen == TODAY, f"实际 {chosen}")

    # 若临期券金额很小，则值得等活动
    small = [Asset("a", "coupon", "小额临期券", 3.0, TODAY, TODAY)]
    chosen2 = best_action_day(small, [trap], TODAY)
    check("临期券金额远小于活动增益时可以选择活动日",
          chosen2 == TODAY + timedelta(days=2), f"实际 {chosen2}")

    # 部分临期：多张券分批到期时，应选不会导致归零的那天
    mixed = [
        Asset("a", "coupon", "今日到期", 30.0, TODAY, TODAY),
        Asset("b", "coupon", "五天后到期", 30.0, TODAY, TODAY + timedelta(days=5)),
    ]
    chosen3 = best_action_day(mixed, [trap], TODAY)
    check("混合到期场景不选择会让部分券归零的日期",
          chosen3 == TODAY, f"实际 {chosen3}")


def test_action_day_rationale() -> None:
    print("\n[5b] 行动日理由不引用已归零的券")
    print("\n[5] 最佳行动日的平票处理")
    assets = [
        Asset("a", "coupon", "券A", 20.0, TODAY, TODAY + timedelta(days=10)),
        Asset("b", "coupon", "券B", 20.0, TODAY, TODAY + timedelta(days=10)),
    ]
    chosen = best_action_day(assets, [], TODAY)
    check("收益相同时取最早可执行日（今天）", chosen == TODAY, f"实际 {chosen}")

    # 理由生成：候选日之后的券才可计入收益
    early = Asset("a", "coupon", "今日到期", 45.0, TODAY, TODAY)
    late = Asset("b", "coupon", "下周到期", 40.0, TODAY, TODAY + timedelta(days=7))
    reasons = action_day_rationale(TODAY + timedelta(days=2), [early, late], [], TODAY)
    gain_line = next((r for r in reasons if "该日仍有" in r), "")
    warn_line = next((r for r in reasons if "需更早使用" in r), "")
    check("已归零的券不计入候选日收益项",
          "今日到期" not in gain_line, f"实际收益项：{gain_line}")
    check("已归零的券在收益项中被显式警告",
          "今日到期" in warn_line, f"实际警告项：{warn_line}")
    check("仍在有效期内的券计入收益项", "下周到期" in gain_line, f"实际：{gain_line}")

    # 已过期的券绝不应出现在"需更早使用"的提醒里——那已是无法挽回的事
    dead = Asset("c", "coupon", "早已过期", 99.0, TODAY, TODAY - timedelta(days=4))
    reasons2 = action_day_rationale(TODAY, [dead, late], [], TODAY)
    joined2 = " ".join(reasons2)
    check("已过期券不出现在任何提醒理由中", "早已过期" not in joined2,
          f"实际：{joined2}")

    # 已过期券也不应计入最佳行动日的"需提前使用"金额
    rep2 = build_report([dead, late], [], TODAY)
    check("已过期券不计入行动日预警金额",
          approx(rep2["best_action_day_urgent_before"], 0.0),
          f"实际 {rep2['best_action_day_urgent_before']}")


def test_best_action_day_ignores_expired() -> None:
    print("\n[6] 最佳行动日忽略过期资产")
    assets = [Asset("a", "coupon", "已过期", 100.0, TODAY, TODAY - timedelta(days=5))]
    campaign = Campaign("c1", "明天活动", TODAY + timedelta(days=1),
                        TODAY + timedelta(days=1), 10.0)
    chosen = best_action_day(assets, [campaign], TODAY)
    check("只剩过期资产时不选高增益未来日", chosen == TODAY, f"实际 {chosen}")


# ---------------------------------------------------------------- 归一化


def test_normalize_coupons_multischema() -> None:
    print("\n[7] 券归一化：多字段兼容")
    raw = {
        "data": [
            {
                "couponId": "C1",
                "couponName": "第二份半价",
                "discount": "¥12.00",
                "endTime": "2026-10-12",
                "channel": "app",
            },
            {
                "id": "C2",
                "name": "早餐立减",
                "amount": 8,
                "expireAt": "2026/10/15",
            },
            {"title": "无ID无金额", "endDate": "2026-10-20"},
        ]
    }
    assets = normalize_coupons(raw, TODAY)
    check("解析出 3 张券", len(assets) == 3, f"实际 {len(assets)}")
    if len(assets) == 3:
        check("中文金额串被正确解析", approx(assets[0].face_value, 12.0),
              f"实际 {assets[0].face_value}")
        check("斜杠日期被正确解析", assets[1].expires == date(2026, 10, 15),
              f"实际 {assets[1].expires}")
        check("缺失金额降级为 0", approx(assets[2].face_value, 0.0))
        check("来源被记录", assets[0].source == "app", f"实际 {assets[0].source}")


def test_normalize_coupons_deep_wrapping() -> None:
    print("\n[8] 券归一化：深层包裹与脏数据")
    raw = {"result": {"records": [{"name": "嵌套券", "value": 5, "expireAt": "2026-10-11"}]}}
    assets = normalize_coupons(raw, TODAY)
    check("result.records 嵌套被挖出", len(assets) == 1, f"实际 {len(assets)}")

    check("None 输入返回空列表", normalize_coupons(None, TODAY) == [])
    check("非字典项被安全跳过", len(normalize_coupons(["垃圾", 3, None], TODAY)) == 0)
    check("缺日期时用兜底天数", normalize_coupons(
        {"data": [{"name": "无日期券"}]}, TODAY)[0].expires == TODAY + timedelta(days=7))


def test_normalize_campaigns() -> None:
    print("\n[9] 活动归一化")
    raw = [
        {"activityName": "秋日第二杯", "startTime": "2026-10-08",
         "endTime": "2026-10-20", "estimatedGain": 12},
        {"title": "未来活动", "startDate": "2026-10-25", "endDate": "2026-10-27"},
    ]
    camps = normalize_campaigns(raw, TODAY)
    check("解析出 2 档活动", len(camps) == 2, f"实际 {len(camps)}")
    check("覆盖判断正确", camps[0].covers(date(2026, 10, 15)))
    check("活动区间外不覆盖", not camps[0].covers(date(2026, 10, 21)))
    check("缺失增益降级为 0", approx(camps[1].estimated_gain, 0.0))
    check("未来活动可被识别", camps[1].start > TODAY)


# ---------------------------------------------------------------- 报告


def test_build_report() -> None:
    print("\n[10] 报告生成")
    assets = [
        Asset("a", "coupon", "今日到期大额券", 45.0, TODAY, TODAY, source="query-my-coupons"),
        Asset("b", "coupon", "三天后到期", 18.0, TODAY, TODAY + timedelta(days=3)),
        Asset("c", "coupon", "远期券", 10.0, TODAY, TODAY + timedelta(days=50)),
        Asset("d", "coupon", "已过期券", 99.0, TODAY, TODAY - timedelta(days=4)),
    ]
    camps = [Campaign("e1", "进行中加赠", TODAY, TODAY + timedelta(days=2), 8.0)]
    rep = build_report(assets, camps, TODAY)

    s = rep["summary"]
    check("资产总数为 4", s["asset_count"] == 4)
    check("有效资产 3 个", s["active_count"] == 3, f"实际 {s['active_count']}")
    check("过期资产 1 个", s["expired_count"] == 1)
    check("总面值只统计有效资产", approx(s["total_face_value"], 73.0), f"实际 {s['total_face_value']}")
    check("总错失成本 = 有效面值合计", approx(s["total_missed_cost"], 73.0))
    check("衰减损失 = 面值 - 兑现期望价值",
          approx(s["decay_loss"], round(73.0 - s["total_effective_value"], 2), 1e-6))
    rows_ = {r["asset_id"]: r for r in rep["assets"]}
    check("远期券几乎全额保留（兑现概率趋近 P_MAX）",
          rows_["c"]["effective_value"] > 10.0 * 0.92,
          f"实际 {rows_['c']['effective_value']}")
    check("今日到期券仍保留大部分面值（悬崖式归零而非折损）",
          rows_["a"]["effective_value"] > 45.0 * 0.6,
          f"实际 {rows_['a']['effective_value']}")
    check("风险敞口 = 面值 - 兑现期望价值",
          approx(s["total_at_risk_value"], round(73.0 - s["total_effective_value"], 2), 1e-6))
    check("今日/明日到期清单被单独提取",
          len(s["immediate_action_today"]) == 1 and s["immediate_action_today"][0]["title"] == "今日到期大额券",
          f"实际 {s['immediate_action_today']}")

    check("最佳行动日落在今天或未来", rep["action_day"] >= TODAY.isoformat())
    check("行动日理由非空", len(rep["rationale"]) > 0)
    check("活动被识别为进行中", len(s["active_campaigns"]) == 1)
    check("已过期券被标记", any(r["days_left"] < 0 for r in rep["assets"]))

    rows = {r["asset_id"]: r for r in rep["assets"]}
    check("今日到期券判定为今日必用", rows["a"]["verdict"] == "今日必用",
          f"实际 {rows['a']['verdict']}")
    check("三天券判定为紧急或尽快用", rows["b"]["verdict"] in ("紧急", "尽快用"),
          f"实际 {rows['b']['verdict']}")
    check("远期券判定为可择时或暂缓", rows["c"]["verdict"] in ("可择时", "暂缓"),
          f"实际 {rows['c']['verdict']}")
    check("过期券判定为已失效", rows["d"]["verdict"] == "已失效")


def test_build_report_empty() -> None:
    print("\n[11] 空数据边界")
    rep = build_report([], [], TODAY)
    check("空资产不崩溃", rep["summary"]["asset_count"] == 0)
    check("空资产总面值为 0", approx(rep["summary"]["total_face_value"], 0.0))
    check("空资产最佳行动日为今天", rep["action_day"] == TODAY.isoformat())


def test_analyze_end_to_end() -> None:
    print("\n[12] 端到端：离线快照直接出报告")
    snapshot = {
        "coupons": {"data": [
            {"couponId": "X1", "couponName": "午餐立减", "discount": "¥15",
             "endTime": "2026-10-11"},
        ]},
        "campaigns": [
            {"activityName": "周末双人餐", "startTime": "2026-10-11",
             "endTime": "2026-10-12", "estimatedGain": 20},
        ],
    }
    rep = analyze(snapshot, TODAY)
    check("端到端解析出 1 张券", rep["summary"]["asset_count"] == 1)
    check("端到端识别活动密度日", rep["best_density_day"] == "2026-10-11",
          f"实际 {rep['best_density_day']}")
    check("行动日采用活动密度依据",
          rep["action_day_basis"] == "activity_density",
          f"实际 {rep['action_day_basis']}")
    check("行动日命中活动日",
          rep["action_day"] == "2026-10-11", f"实际 {rep['action_day']}")


def test_stability() -> None:
    print("\n[13] 幂等性：同输入同输出")
    snapshot = {
        "coupons": {"data": [
            {"name": "券A", "value": 10, "expireAt": "2026-10-13"},
            {"name": "券B", "value": 25, "expireAt": "2026-10-20"},
        ]},
        "campaigns": [
            {"title": "活动C", "startTime": "2026-10-14", "endTime": "2026-10-16",
             "estimatedGain": 30},
        ],
    }
    a = analyze(snapshot, TODAY)
    b = analyze(snapshot, TODAY)
    check("两次分析结果完全一致", a == b)


def test_real_mcp_schema() -> None:
    """针对麦当劳 MCP v1.0.0 真实返回结构的回归测试。

    这组断言的来源是真实 API 快照，不是猜测的字段名。
    2026-10-10 实测发现真实结构与离线假设的四处关键差异：
      1. 面额在 denomination，单位「分」（1390 = ¥13.90）
      2. 失效日字段是 tradeEndDate，不是 expireAt
      3. 活动名在 articleDto.title，activityTitle 恒为空串
      4. 活动为 data.dailyList[].events[] 三层嵌套
    """
    print("\n[14] 真实 MCP 结构适配")

    # 真实券结构（脱敏后）
    real_coupon = {
        "data": {"coupons": [{
            "id": "CB8364AECC7DFF7E6A1599A9B41C48C8",
            "code": "MCD60149V808840020910",
            "title": "随心配1+1套餐",
            "discountInfo": {
                "discountValue": "0", "discountDesc": "用券价格",
                "discountTypePosition": 1, "discountTypeText": "¥",
            },
            "tradeStartDate": "2026-10-08",
            "tradeEndDate": "2026-10-15",
            "tradeStartDateTime": "2026-10-08 00:00:00",
            "tradeEndDateTime": "2026-10-15 23:59:59",
            "denomination": 1390,
            "currentBalance": 1390,
            "totalCount": 1, "totalAvailableCount": 1,
            "orderTypes": [1], "weekdays": [],
            "tags": [{"type": 2, "value": "pickup", "label": "到店专用"}],
            "receivedDaysMsg": "2日前收到",
            "activityStage": None,
        }]},
    }
    assets = normalize_coupons(real_coupon, TODAY)
    check("真实结构能解析出券", len(assets) == 1, f"实际 {len(assets)}")
    if assets:
        a = assets[0]
        check("面额从 denomination 的「分」换算为元",
              approx(a.face_value, 13.9), f"实际 {a.face_value}（1390 分）")
        check("未把 13.9 误读成 1390", a.face_value < 100,
              "分/元单位混用会导致夸大 100 倍")
        check("失效日取自 tradeEndDate", a.expires == date(2026, 10, 15),
              f"实际 {a.expires}")
        check("生效日取自 tradeStartDate", a.acquired == date(2026, 10, 8))
        check("discountValue=0 时不被当作缺失",
              a.face_value == 13.9, "0 是有效值，表示固定售价券")

    # discountInfo 兜底路径（无 denomination 的情况）
    no_denom = {"data": [{"title": "减¥5券", "discountInfo": {
        "discountValue": "5.00", "discountTypeText": "¥"},
        "expireAt": "2026-10-12"}]}
    a2 = normalize_coupons(no_denom, TODAY)
    check("无 denomination 时回退 discountInfo",
          len(a2) == 1 and approx(a2[0].face_value, 5.0),
          f"实际 {a2[0].face_value if a2 else 'N/A'}")

    # 真实活动结构
    real_camp = {"data": {"currentTime": "2026-10-10 14:26:40", "dailyList": [
        {"date": "2026-10-07", "dateText": "2026年10月7日 往期回顾", "events": [
            {"activityCode": "45f3", "activityStage": 3, "activityTitle": "",
             "price": "0",
             "articleDto": {"title": "超值9.9元早餐两件套陪你开工啦",
                            "highlights": "9.9元超值早餐正式上新!"}}]},
        {"date": "2026-10-10", "dateText": "2026年10月10日 今日", "events": [
            {"activityCode": "a1", "activityStage": 3, "activityTitle": "",
             "price": "0", "articleDto": {"title": "韩式风味蘸酱上新"}},
            {"activityCode": "a2", "activityStage": 3, "activityTitle": "",
             "price": "0", "articleDto": {"title": "蓝莓爆爆珠麦旋风上新"}}]},
        {"date": "2026-10-12", "dateText": "", "events": [
            {"activityCode": "b1", "activityStage": 3, "activityTitle": "",
             "price": "0", "articleDto": {"title": "蓝莓爆爆珠麦旋风上新"}},
            {"activityCode": "b2", "activityStage": 3, "activityTitle": "",
             "price": "0", "articleDto": {"title": "乐享90分钟亲子时间"}}]},
    ], "subscribedEvents": []}}
    camps = normalize_campaigns(real_camp, TODAY)
    check("真实三层嵌套能解析出 5 场活动", len(camps) == 5, f"实际 {len(camps)}")
    check("活动名取自 articleDto.title（activityTitle 为空串）",
          any(c.title == "韩式风味蘸酱上新" for c in camps))
    check("活动日期取自父级 date", any(c.start == date(2026, 10, 12) for c in camps))
    check("price='0' 不被当作有效增益",
          all(c.estimated_gain == 0.0 for c in camps),
          "接口未提供增益时不应编造数字")

    dens = campaign_density(camps)
    by_day = {d.day: d.total for d in dens}
    check("10-10 密度为 2", by_day.get(date(2026, 10, 10)) == 2,
          f"实际 {by_day.get(date(2026, 10, 10))}")
    check("10-12 密度为 2", by_day.get(date(2026, 10, 12)) == 2)

    # 密度推荐应跳过今天（今天所有进行中活动都堆在今天）
    assets_real = normalize_coupons(real_coupon, TODAY)
    best = pick_best_density_day(dens, TODAY, assets_real, 21)
    check("密度推荐日不会是今天（今天被活动堆砌污染）",
          best is None or best.day > TODAY, f"实际 {best}")

    # 真实积分结构
    real_pts = {"data": {"availablePoint": "973.4", "accumulativePoint": "10715.7",
                         "currentMouthExpirePoint": "36.8", "expiredPoint": "1641.3",
                         "frozenPoint": "0", "currency": "麦享会积分"}}
    p = normalize_points(real_pts)
    check("积分可用值解析（字符串型）", approx(p.available, 973.4))
    check("临期积分解析（本月将过期）", approx(p.expiring_this_month, 36.8),
          f"实际 {p.expiring_this_month}")
    check("临期积分即为风险敞口", approx(p.at_risk, 36.8))
    check("已过期积分单独记录", approx(p.expired, 1641.3))


def test_density_respects_asset_lifetime() -> None:
    """密度高≠该去：若推荐日会让临期券归零，必须回退到今天。"""
    print("\n[15] 活动密度不得凌驾于券的寿命")
    camps = [
        Campaign("c1", "密集活动", TODAY + timedelta(days=3),
                 TODAY + timedelta(days=3), 0.0),
        Campaign("c2", "密集活动2", TODAY + timedelta(days=3),
                 TODAY + timedelta(days=3), 0.0),
        Campaign("c3", "密集活动3", TODAY + timedelta(days=3),
                 TODAY + timedelta(days=3), 0.0),
    ]
    dens = campaign_density(camps)
    # 一张 ¥500 券明天就到期
    assets = [Asset("a", "coupon", "大额明日到期券", 500.0,
                    TODAY, TODAY + timedelta(days=1))]
    rep = build_report(assets, camps, TODAY)
    check("高密度日会让大额券归零时，行动日回退到今天",
          rep["action_day"] == TODAY.isoformat(), f"实际 {rep['action_day']}")
    check("回退依据被标记为 density 但仍保守处理",
          rep["best_density_day"] == (TODAY + timedelta(days=3)).isoformat())
    check("今日仍有资产可用",
          rep["best_action_day_valid_assets"] == 500.0)


def test_multiday_campaign_flat_schema() -> None:
    """扁平格式的多日活动区间不能被退化成单日。"""
    print("\n[16] 扁平结构的多日活动区间")
    flat = [
        {"title": "跨天活动", "startTime": "2026-10-10",
         "endTime": "2026-10-13", "estimatedGain": 10},
    ]
    camps = normalize_campaigns(flat, TODAY)
    check("解析出 1 档活动", len(camps) == 1)
    if camps:
        c = camps[0]
        check("活动区间完整保留（非单日）",
              c.start == date(2026, 10, 10) and c.end == date(2026, 10, 13),
              f"实际 {c.start} ~ {c.end}")
        check("区间内每日都算覆盖", c.covers(date(2026, 10, 12)))
        check("区间外不覆盖", not c.covers(date(2026, 10, 14)))
        check("密度按区间天数累加（4 天）",
              sum(d.total for d in campaign_density(camps)) == 4,
              f"实际 {sum(d.total for d in campaign_density(camps))}")


def test_first_of_keeps_zero() -> None:
    """0 是有效值，不能被当假值跳过。"""
    print("\n[17] 零值不得被误判为缺失")
    item = {"discountValue": "0", "price": "0"}
    check("_first_of 保留 0 值", _first_of(item, "discountValue") == "0",
          f"实际 {_first_of(item, 'discountValue')!r}")
    check("字符串 '0' 可转为浮点 0.0", approx(_coerce_float("0"), 0.0))
    check("None 才视为缺失", _first_of({"a": None}, "a") is None)
    check("空字符串视为缺失", _first_of({"a": ""}, "a") is None)
    check("布尔值不被误转为浮点", _coerce_float(True) is None)


def main() -> int:
    print("=" * 58)
    print("麦门时序套利引擎 · 核心算法自测")
    print(f"基准日期：{TODAY.isoformat()}（固定时钟，结果可复现）")
    print("=" * 58)

    for fn in (
        test_decay,
        test_urgency,
        test_missed_cost,
        test_best_action_day_picks_campaign_day,
        test_best_action_day_avoids_trap,
        test_action_day_rationale,
        test_best_action_day_ignores_expired,
        test_normalize_coupons_multischema,
        test_normalize_coupons_deep_wrapping,
        test_normalize_campaigns,
        test_build_report,
        test_build_report_empty,
        test_analyze_end_to_end,
        test_stability,
        test_real_mcp_schema,
        test_density_respects_asset_lifetime,
        test_multiday_campaign_flat_schema,
        test_first_of_keeps_zero,
    ):
        fn()

    print("\n" + "=" * 58)
    print(f"通过 {PASSED} 项，失败 {len(FAILED)} 项")
    if FAILED:
        for name in FAILED:
            print(f"  - {name}")
        return 1
    print("全部通过")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())