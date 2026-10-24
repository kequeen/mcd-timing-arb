#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
生成仓库社交预览图（Social Preview / Open Graph 卡片）
输出: .github/social-preview.png   (1280x640, <1MB)

用途: 任何人把仓库链接贴到微信/Slack/X 时，展开的卡片目前是 GitHub 通用模板
      （只显示仓库名+头像+"Stars 0"，无任何产品视觉）。本脚本生成一张真实卡片：
      左侧一句话价值主张 + 三个能力点，右侧是**真实运行输出**（与 README 首屏
      截图同源同数字，非伪造）。

与 scripts/make_demo_shot.py 共用字体回退链（macOS）：
  Menlo            -> ASCII / 拉丁
  Hiragino Sans GB -> CJK 汉字 + 全角标点（：！（）「」、，）
  Apple Symbols    -> ⚠ 等符号
  emoji            -> Apple Color Emoji 为 bitmap 字体，任意尺寸不可渲染，剔除

刻意不写 Star 数：卡片一旦生成数字就固化，社交平台还会缓存，
陈旧的 "Stars 3" 比不写更伤。仅保留行动号召。
"""
import os
import re
import shutil
import subprocess
import sys

from PIL import Image, ImageDraw, ImageFont

# 仓库根目录按脚本位置推导，避免把某人的本地绝对路径写进仓库
REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
OUT = os.path.join(REPO, ".github", "social-preview.png")

# 渲染本脚本需要 PIL（第三方库），而 run_report.py 是纯标准库。
# 优先用带 PIL 的解释器跑真实输出；找不到就退到当前解释器。
PY = os.environ.get("MCD_PYTHON") or shutil.which("python3") or sys.executable

F_ASCII = "/System/Library/Fonts/Menlo.ttc"
F_CJK = "/System/Library/Fonts/Hiragino Sans GB.ttc"
F_SYM = "/System/Library/Fonts/Apple Symbols.ttf"

W, H = 1280, 640

BG = (17, 19, 24)
PANEL = (26, 29, 35)
PANEL_BAR = (38, 42, 49)
FG = (226, 230, 237)
DIM = (140, 148, 162)
GREEN = (126, 208, 132)
YELLOW = (232, 193, 112)
CYAN = (114, 190, 220)
ORANGE = (232, 160, 112)
WHITE = (255, 255, 255)
ACCENT = (255, 60, 60)          # 麦当劳红
ACCENT_SOFT = (255, 122, 122)


def is_emoji(ch):
    o = ord(ch)
    return (0x1F000 <= o <= 0x1FAFF) or (0x2600 <= o <= 0x27BF) or o in (0x2764, 0xFE0F)


def is_cjk(ch):
    o = ord(ch)
    return (0x4E00 <= o <= 0x9FFF or 0x3000 <= o <= 0x303F
            or 0xFF00 <= o <= 0xFFEF or o in (0x2190, 0x2192))


def font(path, size, index=0):
    try:
        return ImageFont.truetype(path, size, index=index)
    except Exception:
        return ImageFont.truetype(path, size)


def pick_font(ch, table):
    if is_emoji(ch):
        return None
    if is_cjk(ch):
        return table["cjk"]
    if ord(ch) in (0x26A0, 0x2713, 0x2714, 0x263A):
        return table["sym"]
    return table["ascii"]


def render_line(draw, x, y, text, color, table):
    cx = x
    for ch in text:
        f = pick_font(ch, table)
        if f is None:
            continue
        draw.text((cx, y), ch, font=f, fill=color)
        cx += draw.textlength(ch, font=f)
    return cx


def measure(draw, text, table):
    w = 0.0
    for ch in text:
        f = pick_font(ch, table)
        if f is None:
            continue
        w += draw.textlength(ch, font=f)
    return w


def clean(s):
    return "".join(c for c in s if not is_emoji(c))


def real_output_lines():
    """跑真实脚本取输出，保证卡片数字与 README/截图一致。

    只取四个有叙事张力的块（活动密度太吵，会被截断成乱码，故弃用）：
      资产清单 1 行 + 汇总 4 行 + 积分账户 2 行 + 最佳行动日 1 行
    → 券会过期 / 积分会过期 / 所以推荐具体某一天
    """
    p = subprocess.run(
        [PY, "scripts/run_report.py", "--snapshot",
         "fixtures/real-snapshot.json", "--today", "2026-10-10"],
        cwd=REPO, capture_output=True, text=True, timeout=120)
    if p.returncode != 0:
        print("运行失败:", p.stderr[-600:])
        sys.exit(1)
    lines = [l.strip() for l in clean(p.stdout).splitlines()]

    def block(start_marker, ends, want=None):
        """从 start_marker 行开始收集，直到遇到 ends 中任一标记"""
        got, on = [], False
        for l in lines:
            if l.startswith(start_marker):
                on = True
                continue
            if on and any(l.startswith(e) for e in ends):
                break
            if on and l and not l.startswith("---"):
                got.append(l)
        if want:
            got = got[:want]
        return got

    ALL = ["活动密度", "积分账户", "最佳行动日", "本报告为"]
    out = []
    # 资产清单那行较长（含对齐用的定宽空格），取其核心三段重排：
    # 判定 / 券名 / 面额 / 剩余天数。数字全部来自真实输出，不改写含义。
    asset = block("资产清单", ALL, want=1)
    if asset:
        m = re.match(
            r"\s*\[(?P<v>[^\]]+)\]\s*(?P<t>.+?)\s+¥\s*(?P<amt>[\d.]+)\s+"
            r"(?:剩\s*(?P<dl>-?\d+)\s*天|已过期\s*-?\d+\s*天)?",
            asset[0])
        if m:
            bits = [f"[{m.group('v').strip()}]", m.group("t").strip(),
                    f"¥{m.group('amt')}"]
            if m.group("dl"):
                bits.append(f"剩 {m.group('dl')} 天")
            out.append("  ".join(bits))
    out += block("汇总", ALL, want=4)
    # 积分账户取「可用积分」与「本月将过期」两行：
    # 前者说明账户有余额，后者体现「积分也会归零」这第二个时间维度
    pts = block("积分账户", ALL)
    out += [l for l in pts if l.startswith("可用积分")]
    out += [l for l in pts if l.startswith("本月将过期")]
    out += block("最佳行动日", ALL, want=2)
    return out


def main():
    os.makedirs(os.path.dirname(OUT), exist_ok=True)

    t_title = {"ascii": font(F_ASCII, 46), "cjk": font(F_CJK, 46), "sym": font(F_SYM, 46)}
    t_sub = {"ascii": font(F_ASCII, 23), "cjk": font(F_CJK, 23), "sym": font(F_SYM, 23)}
    t_bullet = {"ascii": font(F_ASCII, 21), "cjk": font(F_CJK, 21), "sym": font(F_SYM, 21)}
    t_kicker = {"ascii": font(F_ASCII, 19), "cjk": font(F_CJK, 19), "sym": font(F_SYM, 19)}
    t_mono = {"ascii": font(F_ASCII, 19), "cjk": font(F_CJK, 19), "sym": font(F_SYM, 19)}
    t_mono_s = {"ascii": font(F_ASCII, 17), "cjk": font(F_CJK, 17), "sym": font(F_SYM, 17)}
    t_repo = {"ascii": font(F_ASCII, 19), "cjk": font(F_CJK, 19), "sym": font(F_SYM, 19)}

    img = Image.new("RGB", (W, H), BG)
    d = ImageDraw.Draw(img)

    # 顶部品牌条
    d.rectangle([0, 0, W, 6], fill=ACCENT)

    # ---- 左侧 ----
    LX = 72
    d.text((LX, 52), "★", font=font(F_SYM, 19), fill=ACCENT)
    render_line(d, LX + 26, 50, "麦当劳程序员创意开发大赛 · 参赛作品",
                DIM, t_kicker)

    render_line(d, LX, 92, "麦门时序套利引擎", WHITE, t_title)

    sub1 = "把麦当劳 MCP 数据转成"
    sub2 = "「今天该动还是该等」的行动日历"
    render_line(d, LX, 164, sub1, CYAN, t_sub)
    render_line(d, LX, 198, sub2, CYAN, t_sub)

    bullets = [
        ("过期压力", "券什么时候不行动就归零", ORANGE),
        ("兑现概率", "这钱到底能不能留住", GREEN),
        ("行动日历", "具体到某一天的建议", CYAN),
    ]
    y = 268
    for name, desc, c in bullets:
        d.ellipse([LX, y + 8, LX + 9, y + 17], fill=c)
        render_line(d, LX + 24, y, name, c, t_bullet)
        nw = int(measure(d, name, t_bullet))
        render_line(d, LX + 24 + nw + 14, y, "· " + desc, DIM, t_bullet)
        y += 40

    # 机制链路：MCP 数据 → 双指标模型 → 具体行动日
    y = 400
    d.rectangle([LX, y + 4, LX + 4, y + 56], fill=(58, 64, 74))
    for i, (label, val) in enumerate([
        ("数据", "券 · 活动日历 · 积分"),
        ("模型", "过期压力 × 兑现概率"),
        ("输出", "风险敞口 + 最佳行动日"),
    ]):
        ly = y + i * 26
        render_line(d, LX + 16, ly, label, DIM, t_mono_s)
        render_line(d, LX + 16 + 48, ly, val, FG, t_mono_s)

    # 底部 CTA
    d.rectangle([LX, 560, LX + 4, 592], fill=ACCENT)
    render_line(d, LX + 18, 563, "点个 Star", WHITE, t_bullet)
    cta_w = measure(d, "点个 Star", t_bullet)
    render_line(d, LX + 18 + int(cta_w) + 12, 563, "让它出现在排行榜上",
                DIM, t_bullet)

    # ---- 右侧终端面板 ----
    out = real_output_lines()
    PX, PY = 636, 74
    PW = 580
    PADX = 26
    BAR_H = 40
    LH = 30
    inner_max = PW - PADX * 2

    # 逐行裁剪到面板内宽，避免溢出边框
    tmp = ImageDraw.Draw(img)
    fitted = []
    for l in out:
        # 资产清单那行含固定宽度对齐用的多余空格，先压缩再量
        l = re.sub(r"\s{2,}", "  ", l).strip()
        if measure(tmp, l, t_mono) <= inner_max:
            fitted.append(l)
            continue
        s = l
        while s and measure(tmp, s + "…", t_mono) > inner_max:
            s = s[:-1]
        fitted.append(s + "…")

    foot = "真实数据采集于 2026-10-10 · 快照随仓库提供"
    PH = BAR_H + 22 + LH * len(fitted) + 26 + 34
    d.rounded_rectangle([PX, PY, PX + PW, PY + PH], radius=12, fill=PANEL,
                        outline=(52, 57, 66), width=2)
    d.rounded_rectangle([PX, PY, PX + PW, PY + BAR_H], radius=12, fill=PANEL_BAR)
    d.rectangle([PX, PY + 28, PX + PW, PY + BAR_H], fill=PANEL_BAR)
    for i, c in enumerate([(255, 95, 86), (255, 189, 46), (39, 201, 63)]):
        cx = PX + 22 + i * 24
        d.ellipse([cx - 7, PY + 13, cx + 7, PY + 27], fill=c)
    render_line(d, PX + 104, PY + 11, "mcd-timing-arb · 真实运行输出",
                DIM, t_mono_s)

    ty = PY + BAR_H + 16
    for l in fitted:
        c = FG
        if "风险敞口" in l:
            c = YELLOW
        elif "兑现期望价值" in l or "有效券" in l or "总面值" in l:
            c = GREEN
        elif "今日进行中" in l or "未来活动" in l:
            c = CYAN
        elif "尽快用" in l:
            c = ORANGE
        elif l.startswith("最佳行动日"):
            c = WHITE
        render_line(d, PX + PADX, ty, l, c, t_mono)
        ty += LH

    # 面板底部脚注
    render_line(d, PX + PADX, PY + PH - 34, foot, (108, 116, 130), t_mono_s)

    # ---- 仓库名水印（右下） ----
    repo = "github.com/kequeen/mcd-timing-arb"
    w = measure(d, repo, t_repo)
    render_line(d, W - w - 40, H - 46, repo, (92, 99, 112), t_repo)

    img.save(OUT, "PNG", optimize=True)
    size = os.path.getsize(OUT)
    print("saved:", OUT, img.size, size, "bytes")
    if size > 1024 * 1024:
        print("!! 超过 GitHub 1MB 限制")
        sys.exit(1)


if __name__ == "__main__":
    main()
