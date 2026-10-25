#!/usr/bin/env python3
"""麦门时序套利引擎 —— 命令行报告生成。

用法：
    python3 scripts/run_report.py --demo                 # 离线样例，无需 Token
    python3 scripts/run_report.py --snapshot data.json   # 自定义快照
    python3 scripts/run_report.py --snapshot data.json --format md
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import date
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from arb_core import analyze  # noqa: E402

WEEKDAYS = ["周一", "周二", "周三", "周四", "周五", "周六", "周日"]
ROOT = Path(__file__).resolve().parent.parent


def _money(val: float) -> str:
    """金额显示：整数不带小数，非整数保留一位，避免把 13.9 显示成 14。"""
    return f"{val:.0f}" if abs(val - round(val)) < 0.005 else f"{val:.1f}"


def _fmt(day_iso: str) -> str:
    y, m, d = (int(x) for x in day_iso.split("-"))
    return f"{day_iso}（{WEEKDAYS[date(y, m, d).weekday()]}）"


def render_text(rep: dict) -> str:
    s = rep["summary"]
    lines: list[str] = []
    lines.append("=" * 62)
    lines.append("麦门时序套利引擎 · 时间价值决策报告")
    lines.append(f"基准日期：{rep['generated_for']}")
    lines.append("=" * 62)

    if s["asset_count"] == 0:
        lines.append("")
        lines.append("未获取到任何券数据。请确认 MCP 已连接且账户内有可用券。")
        return "\n".join(lines)

    today_items = [a for a in rep["assets"] if 0 <= a["days_left"] <= 1]
    if today_items:
        total = sum(a["face_value"] for a in today_items)
        lines.append("")
        lines.append(f"⚠今日 / 明日到期：{len(today_items)} 项，合计 ¥{_money(total)}")

    lines.append("")
    lines.append("-" * 62)
    lines.append("资产清单")
    lines.append("-" * 62)
    width = max(len(a["title"]) for a in rep["assets"])
    for a in rep["assets"]:
        dl = a["days_left"]
        dl_txt = f"已过期 {-dl} 天" if dl < 0 else f"剩 {dl} 天"
        lines.append(
            f"  [{a['verdict']:<4}] {a['title']:<{width}}  "
            f"¥{_money(a['face_value']):>6}  {dl_txt:>10}  {a['advice']}"
        )

    lines.append("")
    lines.append("-" * 62)
    lines.append("汇总")
    lines.append("-" * 62)
    lines.append(f"  有效券 {s['active_count']} 张（另有 {s['expired_count']} 张已过期）")
    lines.append(f"  总面值           ¥{_money(s['total_face_value'])}")
    lines.append(f"  兑现期望价值     ¥{_money(s['total_effective_value'])}")
    lines.append(f"  风险敞口         ¥{_money(s['total_at_risk_value'])}   ← 不行动可能永久损失")
    if s["active_campaigns"]:
        lines.append(
            f"  今日进行中       {len(s['active_campaigns'])} 场（详见下方活动密度）"
        )
    if s["upcoming_campaigns"]:
        lines.append(
            f"  未来活动         {len(s['upcoming_campaigns'])} 场（详见下方活动密度）"
        )

    lines.append("")
    lines.append("-" * 62)
    lines.append("活动密度（未来 21 天，来自 campaign-calendar 真实数据）")
    lines.append("-" * 62)
    density = sorted(rep.get("activity_density", []), key=lambda x: x["day"])
    shown = 0
    if density:
        for row in density:
            if row["total"] == 0:
                continue
            mark = ""
            if rep.get("best_density_day") == row["day"]:
                mark = "  ← 推荐"
            title = row["titles"][0]
            if len(title) > 26:
                title = title[:26] + "…"
            lines.append(f"  {row['day']}  {row['total']:>2} 场  {title}{mark}")
            shown += 1
    if shown == 0:
        lines.append("  无活动数据")

    if rep.get("best_density_day"):
        lines.append("")
        lines.append(f"  推荐日：{rep['best_density_day']}　{rep['best_density_summary']}")

    pts = rep.get("points")
    if pts:
        lines.append("")
        lines.append("-" * 62)
        lines.append("积分账户（来自 query-my-account）")
        lines.append("-" * 62)
        lines.append(f"  可用积分{pts['available']:>10g}")
        lines.append(f"  累计积分         {pts['accumulative']:>10g}")
        lines.append(f"  本月将过期{pts['expiring_this_month']:>10g}   ← 不换掉就归零")
        lines.append(f"  历史已过期       {pts['expired']:>10g}")
        lines.append(f"  {pts['advice']}")

    lines.append("")
    lines.append("-" * 62)
    lines.append("最佳行动日")
    lines.append("-" * 62)
    lines.append(f"  {_fmt(rep['action_day'])}")
    basis = rep.get("action_day_basis")
    if basis == "activity_density":
        lines.append("  依据：活动密度（campaign-calendar 真实数据）")
    elif basis == "gain_estimate":
        lines.append("  依据：活动增益估算（接口未提供增益字段）")
    if rep["best_action_day_valid_assets"] > 0:
        lines.append(
            f"  当日仍有 ¥{_money(rep['best_action_day_valid_assets'])} 券有效"
        )
    if rep.get("best_action_day_urgent_before", 0) > 0:
        lines.append(
            f"  注意：另有 ¥{_money(rep['best_action_day_urgent_before'])} 券在该日之前到期，需更早用"
        )
    for reason in rep["rationale"]:
        lines.append(f"  · {reason}")

    lines.append("")
    lines.append("本报告为算法估算，仅供参考，不构成消费建议。")
    lines.append("价格与供应状态以麦当劳官方渠道实时结果为准。")
    return "\n".join(lines)


def render_md(rep: dict) -> str:
    s = rep["summary"]
    if s["asset_count"] == 0:
        return "## 麦门时序套利报告\n\n未获取到券数据，请确认 MCP 已连接。\n"

    out: list[str] = []
    out.append("## 麦门时序套利报告")
    out.append("")
    out.append(f"基准日期 `{rep['generated_for']}`　|　有效券 **{s['active_count']}** 张")
    out.append("")
    out.append("### 资产清单")
    out.append("")
    out.append("| 判定 | 券名 | 面值 | 剩余 | 建议 |")
    out.append("|---|---|---:|---|---|")
    for a in rep["assets"]:
        dl = a["days_left"]
        dl_txt = f"已过期 {-dl} 天" if dl < 0 else f"{dl} 天"
        out.append(
            f"| **{a['verdict']}** | {a['title']} | ¥{_money(a['face_value'])} "
            f"| {dl_txt} | {a['advice']} |"
        )
    out.append("")
    out.append("### 汇总")
    out.append("")
    out.append(f"- 总面值：**¥{_money(s['total_face_value'])}**")
    out.append(f"- 兑现期望价值：¥{_money(s['total_effective_value'])}")
    out.append(f"- 风险敞口：**¥{_money(s['total_at_risk_value'])}**（不行动可能永久损失）")
    out.append("")
    out.append("### 最佳行动日")
    out.append("")
    out.append(f"**{_fmt(rep['action_day'])}**")
    out.append("")
    for reason in rep["rationale"]:
        out.append(f"- {reason}")
    out.append("")
    out.append("> 本报告为算法估算，仅供参考。价格与供应状态以麦当劳官方渠道为准。")
    return "\n".join(out)


def main() -> int:
    ap = argparse.ArgumentParser(
        description="麦门时序套利引擎 · 报告生成",
    )
    ap.add_argument("--snapshot", help="MCP 返回数据的快照 JSON 路径")
    ap.add_argument("--demo", action="store_true", help="使用内置离线样例数据")
    ap.add_argument("--today", help="覆盖基准日期 YYYY-MM-DD（默认取系统日期）")
    ap.add_argument("--format", choices=["text", "md", "json"], default="text")
    args = ap.parse_args()

    if args.demo:
        path = ROOT / "fixtures" / "demo-snapshot.json"
    elif args.snapshot:
        path = Path(args.snapshot)
    else:
        ap.error("需要 --demo 或 --snapshot <path>")
        return 2

    if not path.exists():
        print(f"找不到快照文件：{path}", file=sys.stderr)
        return 2

    with open(path, "r", encoding="utf-8") as f:
        snapshot = json.load(f)

    # 基准日期优先级：--today 显式指定 > 快照自带 _baseline_date > 系统日期。
    # 快照自带基准日是「采集当天」的真实语义；直接拿系统日期会让快照里的券
    # 全部显示为已过期（访客照抄 README 命令却看到「有效券 0 张」）。
    base = date.fromisoformat(args.today) if args.today else None
    if base is None:
        raw_base = snapshot.get("_baseline_date")
        if raw_base:
            try:
                base = date.fromisoformat(raw_base)
            except ValueError:
                base = None
    if base is None:
        print(
            "提示：快照未声明 _baseline_date，已回退到系统日期"
            "（券可能显示为已过期，可加 --today YYYY-MM-DD 固定基准日）",
            file=sys.stderr,
        )

    rep = analyze(snapshot, base)

    if args.format == "json":
        print(json.dumps(rep, ensure_ascii=False, indent=2))
    elif args.format == "md":
        print(render_md(rep))
    else:
        print(render_text(rep))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())