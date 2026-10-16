# -*- coding: utf-8 -*-
"""生成讲解视频用的示意图（PNG）。

用法：`python scripts/make_diagrams.py` → 输出到 `docs/diagrams/*.png`（1920×1080）。

设计约定（**改图先读这三条**）：
  1. 图上只放**标签**，不放句子 —— 解释留给讲解词，图里每格不超过 10 个字；
  2. 每个概念配一个图标（Segoe Fluent Icons，Windows 11 自带），先看图标再看字；
  3. 没有副标题堆叠、没有页脚注释 —— 全图只有一处标题。

只依赖 Pillow + 系统字体。改配色/文案改本文件顶部的常量，不手改图片。
"""

from __future__ import annotations

import math
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont

# ---------------------------------------------------------------- 画布与配色
S = 2                     # 超采样倍数（画完缩回 1×，边缘才平滑）
W, H = 1920, 1080

BG = (255, 255, 255)
PANEL = (247, 249, 252)
BOX = (255, 255, 255)
BORDER = (208, 216, 226)
ACCENT = (0, 120, 212)
ACCENT_SOFT = (232, 241, 252)
ACCENT_DEEP = (0, 84, 148)
INK = (28, 32, 39)
MUTED = (110, 120, 134)
OK = (15, 118, 62)
OK_SOFT = (234, 247, 239)
WARN = (176, 86, 0)
WARN_SOFT = (255, 245, 234)
BAD = (176, 40, 40)
BAD_SOFT = (255, 244, 244)
GRID = (226, 232, 240)

FONT_DIR = Path(r"C:\Windows\Fonts")
_F_REG = FONT_DIR / "msyh.ttc"
_F_BOLD = FONT_DIR / "msyhbd.ttc"
_F_ICON = FONT_DIR / "SegoeIcons.ttf"
_F_LATIN = FONT_DIR / "segoeuib.ttf"

# ---------------------------------------------------------------- 图标码位
I_DOC = 0xE8A5      # 文档
I_PHOTO = 0xE91B    # 图片
I_BOOK = 0xE736     # 论文 / 阅读
I_LIST = 0xE8FD     # 列表 / 段落
I_GRID = 0xE80A     # 表格 / 小块
I_SEARCH = 0xE721   # 检索
I_ROBOT = 0xE99A    # 大模型 / Agent
I_CHAT = 0xE8BD     # 提问 / 对话
I_BULB = 0xEA80     # 精读 / 理解
I_LINK = 0xE71B     # 引用
I_CHECK = 0xE930    # 校验
I_SORT = 0xE8CB     # 重排
I_SWAP = 0xE8AB     # 融合 / 换向
I_CHART = 0xE9D2    # 数字 / 向量
I_SERVER = 0xE8BF   # 存储 / 后端
I_KEY = 0xE8D7      # 密钥
I_GLOBE = 0xE774    # 浏览器 / 语言
I_NOTE = 0xE70B     # 笔记 / 页面
I_GEAR = 0xE713     # 代码 / 处理
I_PIN = 0xE718      # 标记
I_SCAN = 0xE8FE     # 扫描件
I_LOCK = 0xE72E     # 禁止 / 锁

_fonts: dict = {}


def font(size: int, bold: bool = False, icon: bool = False, latin: bool = False):
    key = (size, bold, icon, latin)
    if key not in _fonts:
        if icon:
            path = _F_ICON
        elif latin:
            path = _F_LATIN
        else:
            path = _F_BOLD if bold else _F_REG
        _fonts[key] = ImageFont.truetype(str(path), int(size * S))
    return _fonts[key]


# ---------------------------------------------------------------- 基本图元
def text(d, x, y, s, *, size=20, bold=False, color=INK, anchor="la", spacing=1.5,
         latin=False):
    f = font(size, bold, latin=latin)
    for i, line in enumerate(s.split("\n")):
        d.text((x * S, (y + i * size * spacing) * S), line, font=f, fill=color, anchor=anchor)


def icon(d, x, y, code, *, size=44, color=ACCENT, anchor="mm"):
    d.text((x * S, y * S), chr(code), font=font(size, icon=True), fill=color, anchor=anchor)


def llm_badge(d, cx, cy, *, size=46, fill=ACCENT, color=(255, 255, 255)):
    """“LLM”文字徽章 —— 表示这里有一次大模型调用（比机器人图标直白）。"""
    w, h = size * 1.78, size
    d.rounded_rectangle([(cx - w / 2) * S, (cy - h / 2) * S, (cx + w / 2) * S,
                         (cy + h / 2) * S], radius=size * 0.3 * S, fill=fill)
    text(d, cx, cy, "LLM", size=size * 0.5, bold=True, color=color, anchor="mm",
         latin=True)


def rbox(d, x, y, w, h, *, fill=BOX, border=BORDER, r=14, bw=2):
    d.rounded_rectangle([x * S, y * S, (x + w) * S, (y + h) * S], radius=r * S, fill=fill,
                        outline=None if border is None else border,
                        width=0 if border is None else bw * S)


def line(d, p1, p2, *, color=BORDER, w=2, dash=False, dash_len=12, gap=9):
    if not dash:
        d.line([p1[0] * S, p1[1] * S, p2[0] * S, p2[1] * S], fill=color, width=w * S)
        return
    (x1, y1), (x2, y2) = p1, p2
    total = math.hypot(x2 - x1, y2 - y1)
    if total == 0:
        return
    ux, uy = (x2 - x1) / total, (y2 - y1) / total
    pos = 0.0
    while pos < total:
        end = min(pos + dash_len, total)
        d.line([(x1 + ux * pos) * S, (y1 + uy * pos) * S,
                (x1 + ux * end) * S, (y1 + uy * end) * S], fill=color, width=w * S)
        pos = end + gap


def head(d, p_from, p_to, *, color=ACCENT, size=13):
    ang = math.atan2(p_to[1] - p_from[1], p_to[0] - p_from[0])
    pts = [((p_to[0] + size * math.cos(ang + delta)) * S,
            (p_to[1] + size * math.sin(ang + delta)) * S)
           for delta in (math.radians(152), math.radians(-152))]
    d.polygon([(p_to[0] * S, p_to[1] * S), pts[0], pts[1]], fill=color)


def arrow(d, pts, *, color=ACCENT, w=3, size=13, dash=False):
    for i in range(len(pts) - 1):
        line(d, pts[i], pts[i + 1], color=color, w=w, dash=dash)
    head(d, pts[-2], pts[-1], color=color, size=size)


def cross(d, x, y, r=13, color=BAD):
    d.line([(x - r) * S, (y - r) * S, (x + r) * S, (y + r) * S], fill=color, width=4 * S)
    d.line([(x - r) * S, (y + r) * S, (x + r) * S, (y - r) * S], fill=color, width=4 * S)


def tick(d, x, y, r=13, color=OK):
    d.line([(x - r) * S, y * S, (x - r * 0.2) * S, (y + r * 0.8) * S], fill=color, width=4 * S)
    d.line([(x - r * 0.2) * S, (y + r * 0.8) * S, (x + r) * S, (y - r * 0.7) * S],
           fill=color, width=4 * S)


def tile(d, x, y, w, h, code, label, *, fill=BOX, border=BORDER, ic=ACCENT, tcolor=INK,
         size=19, isize=46, icon_dy=-16, label_dy=22, r=14, accent=False):
    """图标在上、标签在下的方块（图上的基本单元）；`code=None` 时改用 LLM 徽章。"""
    rbox(d, x, y, w, h, fill=ACCENT_SOFT if accent else fill,
         border=ACCENT if accent else border, r=r)
    if code is None:
        llm_badge(d, x + w / 2, y + h / 2 + icon_dy, size=isize * 0.78,
                  fill=ACCENT_DEEP if accent else ic)
    else:
        icon(d, x + w / 2, y + h / 2 + icon_dy, code, size=isize,
             color=ACCENT_DEEP if accent else ic)
    text(d, x + w / 2, y + h / 2 + label_dy, label, size=size, bold=True,
         color=ACCENT_DEEP if accent else tcolor, anchor="ma")


def iconrow(d, x, y, code, label, *, ic=ACCENT, size=19, isize=30, color=INK):
    """图标在左、标签在右的一行。"""
    icon(d, x, y, code, size=isize, color=ic, anchor="lm")
    text(d, x + isize + 16, y, label, size=size, bold=True, color=color, anchor="lm")


def frame(title, sub=None):
    img = Image.new("RGB", (W * S, H * S), BG)
    d = ImageDraw.Draw(img)
    text(d, 88, 56, title, size=38, bold=True)
    d.rounded_rectangle([88 * S, 112 * S, 186 * S, 118 * S], radius=3 * S, fill=ACCENT)
    if sub:
        text(d, 88, 138, sub, size=21, color=MUTED)
    return img, d


def save(img, name):
    out = Path(__file__).resolve().parent.parent / "docs" / "diagrams"
    out.mkdir(parents=True, exist_ok=True)
    path = out / f"{name}.png"
    img.resize((W, H), Image.LANCZOS).save(path, optimize=True)
    print(f"{path}  {path.stat().st_size // 1024} KB")


# ---------------------------------------------------------------- 图 0 总览
def fig_overview():
    img, d = frame("一条论文的旅程", "每一步都为下一步准备好东西")
    stages = [(I_DOC, "导入"), (I_LIST, "解析"), (I_GLOBE, "翻译"), (I_GRID, "分块"),
              (I_SERVER, "建索引"), (I_BULB, "精读"), (I_BOOK, "阅读 · 问答")]
    gap = 26
    bw = (1744 - gap * (len(stages) - 1)) / len(stages)
    x0, y0, bh = 88, 264, 150
    for i, (code, label) in enumerate(stages):
        x = x0 + i * (bw + gap)
        tile(d, x, y0, bw, bh, code, label, size=21, isize=54,
             icon_dy=-20, label_dy=30, accent=i in (1, 2, 3))
        if i:
            arrow(d, [(x - gap + 4, y0 + bh / 2), (x - 6, y0 + bh / 2)], w=3, size=12)

    # 反馈：分块的标记来自翻译
    x_tr = x0 + 1 * (bw + gap) + bw / 2
    x_fen = x0 + 3 * (bw + gap) + bw / 2
    y_fb = y0 + bh + 92
    line(d, (x_fen, y0 + bh), (x_fen, y_fb), color=WARN, w=3, dash=True)
    line(d, (x_fen, y_fb), (x_tr, y_fb), color=WARN, w=3, dash=True)
    arrow(d, [(x_tr, y_fb), (x_tr, y0 + bh + 4)], color=WARN, w=3, size=12)
    icon(d, (x_tr + x_fen) / 2 - 176, y_fb + 22, I_PIN, size=30, color=WARN)
    text(d, (x_tr + x_fen) / 2 - 150, y_fb + 22, "分块用的“可以断开”标记，翻译时就顺手标好了",
         size=19, color=WARN, anchor="lm")

    # 下半区：两条流程上下排（左右排会撞）
    y1 = 620
    rbox(d, 88, y1, 1744, 372, fill=PANEL, border=GRID, r=18)

    iconrow(d, 128, y1 + 44, I_GRID, "建索引（一次性）", ic=ACCENT_DEEP, size=20)
    items = [(I_GRID, "切成小段"), (I_CHART, "变成数字"), (I_SERVER, "存进向量库")]
    cx = 128
    for i, (code, label) in enumerate(items):
        tile(d, cx, y1 + 72, 340, 112, code, label, fill=BOX, size=19, isize=44,
             icon_dy=-14, label_dy=24)
        if i:
            arrow(d, [(cx - 50, y1 + 128), (cx - 8, y1 + 128)], w=3, size=12)
        cx += 386
    icon(d, cx + 12, y1 + 128, I_SEARCH, size=40, color=MUTED)
    text(d, cx + 42, y1 + 128, "之后每次提问都来这里找", size=19, color=MUTED, anchor="lm")

    iconrow(d, 128, y1 + 232, I_CHAT, "提问（每次）", ic=ACCENT_DEEP, size=20)
    ask = [(I_CHAT, "提问"), (I_SEARCH, "两边找"), (I_SWAP, "合排名"), (I_SORT, "重排"),
           (None, "作答"), (I_CHECK, "对答案")]
    cx = 128
    for i, (code, label) in enumerate(ask):
        tile(d, cx, y1 + 260, 240, 96, code, label, fill=BOX, size=18, isize=38,
             icon_dy=-14, label_dy=22)
        if i:
            arrow(d, [(cx - 34, y1 + 308), (cx - 6, y1 + 308)], w=3, size=11)
        cx += 274
    save(img, "00-总览")


# ---------------------------------------------------------------- 图 1 解析
def fig_parse():
    img, d = frame("第一步 · 让机器读懂 PDF", "PDF 只记录了“哪个字画在哪个坐标”")

    px, py, pw, ph = 88, 256, 330, 560
    rbox(d, px, py, pw, ph, fill=BOX, border=BORDER, r=10)
    icon(d, px + 30, py - 26, I_DOC, size=40, color=MUTED)
    text(d, px + 56, py - 26, "PDF：一张排好版的纸", size=19, bold=True, anchor="lm")
    col_w = 128
    for c in range(2):
        cx = px + 26 + c * (col_w + 22)
        yy = py + 34
        for i in range(17):
            wline = col_w - (34 if (i == 5 and c == 1) else 0)
            line(d, (cx, yy), (cx + wline, yy), color=(206, 213, 222), w=6)
            yy += 21
        if c == 1:
            rbox(d, cx, py + 148, col_w, 92, fill=(228, 233, 240), border=BORDER, r=6)
            icon(d, cx + col_w / 2, py + 194, I_PHOTO, size=34, color=MUTED)

    arrow(d, [(px + pw + 46, py + ph / 2), (px + pw + 190, py + ph / 2)], w=4, size=16)
    text(d, px + pw + 118, py + ph / 2 - 42, "重新拼装", size=20, bold=True,
         color=ACCENT_DEEP, anchor="ma")

    bx, by, bwid = 700, 296, 470
    blocks = [(I_LIST, "标题"), (I_LIST, "段落"), (I_CHART, "公式"),
              (I_PHOTO, "插图 + 图注"), (I_GRID, "表格"), (I_BOOK, "参考文献")]
    yy = by
    for code, t in blocks:
        rbox(d, bx, yy, bwid, 62, fill=BOX, border=BORDER, r=10)
        d.rounded_rectangle([bx * S, (yy + 14) * S, (bx + 6) * S, (yy + 48) * S],
                            radius=3 * S, fill=ACCENT)
        icon(d, bx + 38, yy + 31, code, size=34, color=ACCENT_DEEP)
        text(d, bx + 68, yy + 31, t, size=19, bold=True, anchor="lm")
        yy += 72
    text(d, bx, by - 40, "有类型的块 + 正确的阅读顺序", size=19, bold=True)

    nx = 1230
    rbox(d, nx, 296, 600, 468, fill=PANEL, border=GRID, r=16)
    text(d, nx + 34, 328, "难在哪", size=21, bold=True, color=ACCENT_DEEP)
    notes = [(I_LIST, "没有“段落”，只有坐标"), (I_SWAP, "双栏会串着读"),
             (I_CHART, "公式碎成一堆符号"), (I_SCAN, "扫描件一个字都抽不出")]
    yy = 392
    for code, t in notes:
        icon(d, nx + 44, yy, code, size=32, color=MUTED)
        text(d, nx + 74, yy, t, size=19, anchor="lm")
        yy += 60
    line(d, (nx + 34, yy + 6), (nx + 566, yy + 6), color=GRID, w=2)
    icon(d, nx + 44, yy + 56, I_SCAN, size=32, color=WARN)
    text(d, nx + 74, yy + 56, "扫描件改让模型看图转录", size=19, bold=True, color=WARN,
         anchor="lm")
    save(img, "01-解析")


# ---------------------------------------------------------------- 图 2 翻译
def fig_translate():
    img, d = frame("第二步 · 一次调用，两份产物", "一页交给大模型一次")

    # 固定背景
    rbox(d, 88, 250, 1744, 76, fill=ACCENT_SOFT, border=ACCENT, r=14)
    icon(d, 130, 288, I_PIN, size=34, color=ACCENT_DEEP)
    text(d, 162, 288, "固定背景（术语表 · 领域风格）—— 每页都一样，放在最前面",
         size=20, bold=True, color=ACCENT_DEEP, anchor="lm")

    # 输入
    ix, iw = 88, 330
    text(d, ix, 372, "输入", size=20, bold=True, color=ACCENT_DEEP)
    tile(d, ix, 406, iw, 130, I_PHOTO, "整页图片", size=20, isize=44)
    tile(d, ix, 556, iw, 130, I_NOTE, "页内文本", size=20, isize=44)
    arrow(d, [(ix + iw + 26, 471), (668, 471)], w=3, size=13)
    arrow(d, [(ix + iw + 26, 621), (668, 621)], w=3, size=13)
    arrow(d, [(860, 332), (860, 400)], w=3, size=13)

    # 模型
    mx, mw, my, mh = 690, 310, 406, 280
    rbox(d, mx, my, mw, mh, fill=BOX, border=ACCENT, r=18)
    llm_badge(d, mx + mw / 2, my + 62, size=68)
    text(d, mx + mw / 2, my + 128, "大模型", size=25, bold=True, color=ACCENT_DEEP,
         anchor="ma")
    text(d, mx + mw / 2, my + 166, "一次调用", size=19, color=MUTED, anchor="ma")
    icon(d, mx + mw / 2 - 74, my + 224, I_PIN, size=26, color=WARN)
    text(d, mx + mw / 2 - 54, my + 224, "第 3 章再加一项：断开标记", size=16,
         color=WARN, anchor="lm")

    # 输出
    ox, ow = 1130, 560
    arrow(d, [(mx + mw + 20, 470), (ox - 24, 470)], w=4, size=15)
    tile(d, ox, 406, ow, 130, I_DOC, "英文原文（照抄）", size=20, isize=44,
         icon_dy=-16, label_dy=26)
    tile(d, ox, 556, ow, 130, I_DOC, "中文译文", size=20, isize=44, fill=OK_SOFT,
         border=OK, ic=OK, tcolor=(11, 92, 48), icon_dy=-16, label_dy=26)
    line(d, (ox + ow - 90, 552), (ox + ow - 90, 528), color=ACCENT, w=3)
    head(d, (ox + ow - 90, 552), (ox + ow - 90, 528), color=ACCENT, size=11)
    head(d, (ox + ow - 90, 528), (ox + ow - 90, 552), color=ACCENT, size=11)
    text(d, ox + ow - 74, 540, "段落一一对应", size=18, bold=True, color=ACCENT_DEEP,
         anchor="lm")

    rbox(d, 88, 740, 1744, 116, fill=PANEL, border=GRID, r=16)
    icon(d, 130, 798, I_LINK, size=36, color=ACCENT_DEEP)
    text(d, 166, 798, "两份出自同一次生成 → 对照天然对齐，不用写对齐算法",
         size=21, bold=True, color=ACCENT_DEEP, anchor="lm")
    save(img, "02-翻译")


# ---------------------------------------------------------------- 图 3 分块
def fig_chunk():
    img, d = frame("第三步 · 分块", "要让机器能检索，就得先切成一小段一小段")

    # 左：模型标边界
    tile(d, 88, 300, 400, 190, None, "翻译时顺手标出", size=20, isize=60)
    text(d, 88 + 200, 300 + 148, "“这里可以断开”", size=19, bold=True, color=WARN,
         anchor="ma")
    arrow(d, [(496, 395), (580, 395)], w=3, size=13)

    # 中：代码拼装
    mx, my, mw, mh = 600, 264, 520, 468
    rbox(d, mx, my, mw, mh, fill=BOX, border=ACCENT, r=18)
    icon(d, mx + mw / 2 - 60, my + 44, I_GEAR, size=40, color=ACCENT_DEEP)
    text(d, mx + mw / 2 - 32, my + 44, "代码来拼", size=22, bold=True, color=ACCENT_DEEP,
         anchor="lm")
    line(d, (mx + 30, my + 82), (mx + mw - 30, my + 82), color=GRID, w=2)
    steps = ["一行算最小单位，不许切开", "挑最近的断开位置", "拼到合适大小",
             "检查：没漏行、没跨章节", "不合格就退回去"]
    yy = my + 124
    for i, t in enumerate(steps, start=1):
        d.ellipse([(mx + 30) * S, (yy - 14) * S, (mx + 56) * S, (yy + 12) * S], fill=ACCENT)
        text(d, mx + 43, yy - 1, str(i), size=15, bold=True, color=(255, 255, 255),
             anchor="mm")
        text(d, mx + 74, yy, t, size=19, anchor="lm")
        yy += 74
    arrow(d, [(1124, 395), (1210, 395)], w=3, size=13)

    # 右：结果
    tile(d, 1230, 300, 600, 190, I_GRID, "一小段 = 一个检索单位", size=22, isize=60)
    rbox(d, 1230, 522, 600, 210, fill=WARN_SOFT, border=WARN, r=16)
    icon(d, 1270, 566, I_LOCK, size=34, color=WARN)
    text(d, 1302, 566, "公式和表格切不坏", size=20, bold=True, color=WARN, anchor="lm")
    text(d, 1270, 618, "因为“行”是最小单位，", size=18)
    text(d, 1270, 650, "公式、表格、图注各自都是完整的行。", size=18)

    # 底部对比
    by = 810
    rbox(d, 88, by, 830, 132, fill=BAD_SOFT, border=(214, 122, 122), r=16)
    cross(d, 132, by + 44, r=13)
    text(d, 162, by + 44, "按字数硬切", size=20, bold=True, color=BAD, anchor="lm")
    text(d, 132, by + 82, "一句话、一段论证被拦腰截断", size=18)
    rbox(d, 946, by, 886, 132, fill=OK_SOFT, border=OK, r=16)
    tick(d, 990, by + 44, r=13)
    text(d, 1020, by + 44, "模型标边界 + 代码拼装", size=20, bold=True, color=(11, 92, 48),
         anchor="lm")
    text(d, 990, by + 82, "模型管语义，代码管结构不破", size=18)
    save(img, "03-分块")


# ---------------------------------------------------------------- 图 4 RAG
def fig_rag():
    img, d = frame("第四步 · 建索引与检索问答（RAG）", "先把小段变成数字存起来，提问时再把最相关的找出来")

    # 建索引
    rbox(d, 88, 224, 1744, 210, fill=PANEL, border=GRID, r=18)
    iconrow(d, 128, 272, I_GRID, "建索引（一次性）", ic=ACCENT_DEEP, size=20)
    seq = [(I_GRID, "切成小段"), (I_CHART, "变成数字"), (I_SERVER, "存进向量库")]
    cx = 128
    for i, (code, label) in enumerate(seq):
        tile(d, cx, 306, 400, 106, code, label, fill=BOX, size=20, isize=44,
             icon_dy=-14, label_dy=24)
        if i:
            arrow(d, [(cx - 56, 359), (cx - 10, 359)], w=3, size=12)
        cx += 456
    icon(d, cx + 8, 359, I_SEARCH, size=40, color=MUTED)
    text(d, cx + 36, 359, "之后每次提问\n都来这里找", size=18, color=MUTED, anchor="lm",
         spacing=1.6)

    # 提问
    iconrow(d, 88, 486, I_CHAT, "提问（每次）", ic=ACCENT_DEEP, size=20)
    steps = [(I_CHAT, "提问"), (I_SEARCH, "两边找"), (I_SWAP, "合排名"), (I_SORT, "重排"),
             (I_LIST, "拼材料"), (None, "作答"), (I_CHECK, "对答案"), (I_LINK, "带引用")]
    gap = 22
    bw = (1744 - gap * (len(steps) - 1)) / len(steps)
    y0, bh = 526, 210
    for i, (code, label) in enumerate(steps):
        x = 88 + i * (bw + gap)
        accent = label in ("重排", "对答案")
        tile(d, x, y0, bw, bh, code, label, size=19, isize=52, icon_dy=-18, label_dy=28,
             accent=accent)
        if i:
            arrow(d, [(x - gap + 3, y0 + bh / 2), (x - 4, y0 + bh / 2)], w=3, size=11)

    # 两路找 / 重排 的注解
    x_find = 88 + 1 * (bw + gap) + bw / 2
    x_sort = 88 + 3 * (bw + gap) + bw / 2
    y_note = y0 + bh + 62
    line(d, (x_find, y0 + bh), (x_find, y_note), color=ACCENT, w=2, dash=True)
    line(d, (x_find, y_note), (x_sort, y_note), color=ACCENT, w=2, dash=True)
    text(d, (x_find + x_sort) / 2, y_note + 16,
         "两边找 = 意思相近的 + 关键词命中的；重排 = 逐条再看一遍，谁真的相关",
         size=19, color=ACCENT_DEEP, anchor="ma")

    rbox(d, 88, 900, 1744, 96, fill=OK_SOFT, border=OK, r=16)
    icon(d, 130, 948, I_CHECK, size=36, color=OK)
    text(d, 166, 948, "答案必须对得上找出来的原文；对不上就说“依据不足”，不编",
         size=20, bold=True, color=(11, 92, 48), anchor="lm")
    save(img, "04-RAG")


# ---------------------------------------------------------------- 图 5 Agent
def fig_agent():
    img, d = frame("第五步 · 问答 Agent", "它能干什么，由这份工具清单说了算")

    # 主链路
    tile(d, 88, 300, 236, 150, I_GLOBE, "阅读器", size=19, isize=44)
    arrow(d, [(332, 375), (388, 375)], w=3, size=13)
    tile(d, 396, 300, 260, 150, I_SERVER, "主后端", size=19, isize=44)
    arrow(d, [(664, 375), (720, 375)], w=3, size=13)
    tile(d, 728, 300, 300, 150, I_ROBOT, "Agent 服务", size=19, isize=44, accent=True)
    text(d, 878, 474, "它只会调工具", size=17, color=MUTED, anchor="ma")

    # 工具清单
    tx, ty, tw = 1096, 262, 736
    rbox(d, tx, ty, tw, 584, fill=PANEL, border=GRID, r=16)
    icon(d, tx + 40, ty + 40, I_LOCK, size=32, color=ACCENT_DEEP)
    text(d, tx + 68, ty + 40, "只能用这几个工具", size=20, bold=True, color=ACCENT_DEEP,
         anchor="lm")
    tools = [(I_SEARCH, "在本篇论文里找段落"), (I_NOTE, "读某几页的正文"),
             (I_PHOTO, "把整页变图给模型看"), (I_GRID, "看图 / 表的裁图"),
             (I_GLOBE, "在整个库里找"), (I_BOOK, "写高亮 / 笔记")]
    yy = ty + 82
    for code, t in tools:
        rbox(d, tx + 30, yy, tw - 60, 68, fill=BOX, border=BORDER, r=10)
        icon(d, tx + 66, yy + 34, code, size=34, color=ACCENT_DEEP)
        text(d, tx + 100, yy + 34, t, size=19, bold=True, anchor="lm")
        yy += 80
    arrow(d, [(1032, 375), (1084, 375)], w=3, size=13)

    # 它怎么干活
    hx, hy, hw, hh = 88, 520, 960, 326
    rbox(d, hx, hy, hw, hh, fill=PANEL, border=GRID, r=16)
    icon(d, hx + 40, hy + 40, I_CHAT, size=32, color=ACCENT_DEEP)
    text(d, hx + 68, hy + 40, "它怎么干活", size=20, bold=True, color=ACCENT_DEEP,
         anchor="lm")
    flow = [(I_CHAT, "你去问"), (I_SERVER, "主后端转发（鉴权 · 存会话）"),
            (I_ROBOT, "Agent 决定调哪个工具"), (I_SEARCH, "工具回主后端取数据"),
            (I_LINK, "答完带引用，能点回原文")]
    yy = hy + 92
    for code, t in flow:
        icon(d, hx + 52, yy, code, size=34, color=ACCENT_DEEP)
        text(d, hx + 86, yy, t, size=19, bold=True, anchor="lm")
        yy += 50

    # 被挡住的两条
    by = 880
    rbox(d, 88, by, 960, 118, fill=BAD_SOFT, border=(214, 122, 122), r=16)
    cross(d, 132, by + 59, r=13)
    text(d, 164, by + 44, "不能直接碰数据库和文件", size=20, bold=True, color=BAD)
    text(d, 164, by + 76, "想加能力就加一个工具，不把库敞开", size=17, color=MUTED)
    rbox(d, 1072, by, 760, 118, fill=BAD_SOFT, border=(214, 122, 122), r=16)
    cross(d, 1116, by + 59, r=13)
    text(d, 1148, by + 44, "不持有模型密钥", size=20, bold=True, color=BAD)
    text(d, 1148, by + 76, "密钥只在设置页里配", size=17, color=MUTED)
    save(img, "05-Agent")


def main():
    fig_overview()
    fig_parse()
    fig_translate()
    fig_chunk()
    fig_rag()
    fig_agent()


if __name__ == "__main__":
    main()
