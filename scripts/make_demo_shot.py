#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
生成 README 首屏用的终端风格截图（真实运行输出渲染，非伪造）
输出: docs/demo-terminal.png

字体回退链（macOS）：
  Menlo            -> ASCII / 拉丁
  Hiragino Sans GB -> CJK 汉字 + 全角标点（：！（）「」、，）+ 箭头
  Apple Symbols    -> ⚠ 等符号
  emoji            -> Apple Color Emoji 仅 bitmap 尺寸，任意尺寸不可渲染，故剔除
"""
import os
import re
import subprocess
import sys

from PIL import Image, ImageDraw, ImageFont

PY = "/Users/liangnian/.workbuddy/binaries/python/envs/default/bin/python3"
REPO = "/Users/liangnian/WorkBuddy/mcd"
OUT = os.path.join(REPO, "docs", "demo-terminal.png")

F_ASCII = "/System/Library/Fonts/Menlo.ttc"
F_CJK = "/System/Library/Fonts/Hiragino Sans GB.ttc"
F_SYM = "/System/Library/Fonts/Apple Symbols.ttf"

BG = (22, 24, 29)
BAR = (38, 41, 48)
FG = (222, 226, 233)
DIM = (138, 146, 160)
GREEN = (126, 208, 132)
YELLOW = (232, 193, 112)
CYAN = (114, 190, 220)
ORANGE = (232, 160, 112)
WHITE = (255, 255, 255)

FS = 21
LH = 31
PAD_X = 34
BAR_H = 44


def is_emoji(ch):
    o = ord(ch)
    return (0x1F000 <= o <= 0x1FAFF) or (0x2600 <= o <= 0x27BF) or o in (0x2764, 0xFE0F)


def is_cjk(ch):
    o = ord(ch)
    return (0x4E00 <= o <= 0x9FFF or 0x3000 <= o <= 0x303F
            or 0xFF00 <= o <= 0xFFEF or o in (0x2190, 0x2192))


def build_fonts():
    f_ascii = ImageFont.truetype(F_ASCII, FS)
    f_cjk = ImageFont.truetype(F_CJK, FS)
    try:
        f_sym = ImageFont.truetype(F_SYM, FS)
    except Exception:
        f_sym = f_cjk
    return f_ascii, f_cjk, f_sym


F_MAIN, F_CJKF, F_SYM = build_fonts()


def pick_font(ch):
    if is_emoji(ch):
        return None                      # 剔除
    if is_cjk(ch):
        return F_CJKF
    if ord(ch) in (0x26A0, 0x2713, 0x2714, 0x263A):
        return F_SYM
    return F_MAIN


def clean(s):
    return "".join(c for c in s if not is_emoji(c))


def render_line(draw, x, y, text, color):
    cx = x
    for ch in text:
        f = pick_font(ch)
        if f is None:
            continue
        draw.text((cx, y), ch, font=f, fill=color)
        cx += draw.textlength(ch, font=f)
    return cx


def measure(draw, text):
    w = 0
    for ch in text:
        f = pick_font(ch)
        if f is None:
            continue
        w += draw.textlength(ch, font=f)
    return w


def color_of(l):
    if re.match(r"^\s*=+\s*$", l):
        return (88, 94, 106)
    if "今日 / 明日到期" in l or "⚠" in l:
        return YELLOW
    if "今日必用" in l or "紧急" in l:
        return ORANGE
    if "风险敞口" in l or "←" in l:
        return YELLOW
    if "兑现期望价值" in l or "有效券" in l or "总面值" in l:
        return GREEN
    if "活动密度" in l or "推荐日" in l or "最佳行动日" in l or "推荐" in l:
        return CYAN
    if l.strip().startswith("麦门时序套利引擎"):
        return WHITE
    if l.strip().startswith("基准日期"):
        return DIM
    return FG


def main():
    # 真实运行，固定基准日 2026-10-10（与快照采集日一致，数字与 README 相同）
    p = subprocess.run(
        [PY, "scripts/run_report.py", "--snapshot",
         "fixtures/real-snapshot.json", "--today", "2026-10-10"],
        cwd=REPO, capture_output=True, text=True, timeout=120)
    if p.returncode != 0:
        print("运行失败:", p.stderr[-600:])
        sys.exit(1)

    raw = clean(p.stdout)
    lines = [l.rstrip() for l in raw.splitlines()]

    # 保留到"最佳行动日"块结束，控制在 40 行内
    keep = []
    for l in lines:
        keep.append(l)
        if l.strip().startswith("最佳行动日"):
            # 再留 3 行（推荐日期 + 依据）
            keep.extend([""] * 0)
    # 截断
    if len(keep) > 40:
        keep = keep[:40]
    # 去掉尾部空行与"本报告为算法估算"免责声明（README 正文已有）
    while keep and not keep[-1].strip():
        keep.pop()
    cut = None
    for i, l in enumerate(keep):
        if l.strip().startswith("本报告为"):
            cut = i
            break
    if cut:
        keep = keep[:cut]
    while keep and not keep[-1].strip():
        keep.pop()

    tmp = Image.new("RGB", (10, 10))
    d0 = ImageDraw.Draw(tmp)
    maxw = max((measure(d0, l) for l in keep), default=600)
    title = "mcd-timing-arb — python3 scripts/run_report.py --snapshot"
    title_w = measure(d0, title)
    # 标题左右各留出交通灯(3*26) 与边距
    width = max(int(max(maxw + PAD_X * 2, title_w + 200)), 900)
    height = BAR_H + 14 + LH * len(keep) + 22

    img = Image.new("RGB", (width, height), BG)
    d = ImageDraw.Draw(img)

    # 标题栏
    d.rectangle([0, 0, width, BAR_H], fill=BAR)
    for i, c in enumerate([(255, 95, 86), (255, 189, 46), (39, 201, 63)]):
        cx = 24 + i * 26
        d.ellipse([cx - 7, BAR_H // 2 - 7, cx + 7, BAR_H // 2 + 7], fill=c)
    tx = (width - title_w) // 2
    tx = max(tx, 24 + 3 * 26 + 16)          # 不压交通灯
    render_line(d, tx, 11, title, DIM)

    y = BAR_H + 14
    for l in keep:
        render_line(d, PAD_X, y, l, color_of(l))
        y += LH

    os.makedirs(os.path.dirname(OUT), exist_ok=True)
    img.save(OUT, "PNG", optimize=True)
    print("saved:", OUT, img.size, os.path.getsize(OUT), "bytes", "| lines:", len(keep))


if __name__ == "__main__":
    main()
