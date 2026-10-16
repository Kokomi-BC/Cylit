# -*- coding: utf-8 -*-
"""生成讲解视频用的示意图（PNG）。

用法：`python scripts/make_diagrams.py` → 输出到 `docs/diagrams/*.png`（1920×1080）。

只依赖 Pillow + 系统自带中文字体（微软雅黑）。所有图共用一套样式，
改配色/字号改本文件顶部的常量即可，不手改图片。
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
ACCENT = (0, 120, 212)          # WinUI 强调色，与 Cylit 界面一致
ACCENT_SOFT = (232, 241, 252)
ACCENT_DEEP = (0, 84, 148)
INK = (28, 32, 39)
MUTED = (98, 108, 122)
OK = (15, 118, 62)
OK_SOFT = (234, 247, 239)
WARN = (176, 86, 0)
WARN_SOFT = (255, 245, 234)
GRID = (226, 232, 240)
GRAY_FILL = (240, 243, 247)

FONT_DIR = Path(r"C:\Windows\Fonts")
_F_REG = FONT_DIR / "msyh.ttc"
_F_BOLD = FONT_DIR / "msyhbd.ttc"
_F_MONO = FONT_DIR / "consola.ttf"

_fonts: dict = {}


def font(size: int, bold: bool = False, mono: bool = False):
    key = (size, bold, mono)
    if key not in _fonts:
        path = _F_MONO if mono else (_F_BOLD if bold else _F_REG)
        _fonts[key] = ImageFont.truetype(str(path), int(size * S))
    return _fonts[key]


# ---------------------------------------------------------------- 基本图元
def text(d, x, y, s, *, size=20, bold=False, mono=False, fill=INK,
         anchor="la", spacing=1.5):
    """左上角对齐的多行文本（x/y 是最终坐标系，不需要乘 S）。"""
    f = font(size, bold, mono)
    for i, line in enumerate(s.split("\n")):
        d.text((x * S, (y + i * size * spacing) * S), line, font=f, fill=fill,
               anchor=anchor)


def wrap(d, s, size, maxw, *, bold=False, mono=False) -> list[str]:
    f = font(size, bold, mono)
    out: list[str] = []
    for para in s.split("\n"):
        line = ""
        for ch in para:
            if d.textlength(line + ch, font=f) / S > maxw and line:
                if " " in line.strip() and ch != " ":
                    head, _, tail = line.rpartition(" ")
                    out.append(head)
                    line = tail + ch
                else:
                    out.append(line)
                    line = ch
            else:
                line += ch
        out.append(line)
    return out


def para(d, x, y, s, *, size=19, maxw=760, fill=INK, bold=False, spacing=1.6):
    for i, line in enumerate(wrap(d, s, size, maxw, bold=bold)):
        text(d, x, y + i * size * spacing, line, size=size, bold=bold, fill=fill,
             spacing=1.0)
    return y + len(wrap(d, s, size, maxw, bold=bold)) * size * spacing


def rbox(d, x, y, w, h, *, fill=BOX, border=BORDER, r=14, bw=2):
    d.rounded_rectangle([x * S, y * S, (x + w) * S, (y + h) * S], radius=r * S,
                        fill=fill, outline=None if border is None else border,
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
    pts = []
    for delta in (math.radians(152), math.radians(-152)):
        pts.append(((p_to[0] + size * math.cos(ang + delta)) * S,
                    (p_to[1] + size * math.sin(ang + delta)) * S))
    d.polygon([(p_to[0] * S, p_to[1] * S), pts[0], pts[1]], fill=color)


def arrow(d, pts, *, color=ACCENT, w=3, size=13, dash=False, head_at_end=True):
    for i in range(len(pts) - 1):
        line(d, pts[i], pts[i + 1], color=color, w=w, dash=dash)
    if head_at_end:
        head(d, pts[-2], pts[-1], color=color, size=size)
    else:
        head(d, pts[1], pts[0], color=color, size=size)


def chip(d, x, y, w, h, label, *, sub=None, fill=BOX, border=BORDER,
         accent=False, size=19, sub_size=15, r=12):
    rbox(d, x, y, w, h, fill=ACCENT_SOFT if accent else fill,
         border=ACCENT if accent else border, r=r)
    if sub:
        text(d, x + w / 2, y + h / 2 - size * 0.95, label, size=size, bold=True,
             fill=ACCENT_DEEP if accent else INK, anchor="ma")
        text(d, x + w / 2, y + h / 2 + size * 0.35, sub, size=sub_size, fill=MUTED,
             anchor="ma")
    else:
        text(d, x + w / 2, y + h / 2, label, size=size, bold=True,
             fill=ACCENT_DEEP if accent else INK, anchor="mm")


def cross(d, x, y, r=13, color=(190, 40, 40)):
    d.line([(x - r) * S, (y - r) * S, (x + r) * S, (y + r) * S], fill=color, width=4 * S)
    d.line([(x - r) * S, (y + r) * S, (x + r) * S, (y - r) * S], fill=color, width=4 * S)


def tick(d, x, y, r=13, color=OK):
    d.line([(x - r) * S, y * S, (x - r * 0.2) * S, (y + r * 0.8) * S], fill=color, width=4 * S)
    d.line([(x - r * 0.2) * S, (y + r * 0.8) * S, (x + r) * S, (y - r * 0.7) * S],
           fill=color, width=4 * S)


def frame(title, sub=None, foot=None):
    img = Image.new("RGB", (W * S, H * S), BG)
    d = ImageDraw.Draw(img)
    text(d, 88, 50, title, size=37, bold=True)
    d.rounded_rectangle([88 * S, 104 * S, 186 * S, 110 * S], radius=3 * S, fill=ACCENT)
    if sub:
        text(d, 88, 128, sub, size=20, fill=MUTED)
    if foot:
        for i, ln in enumerate(wrap(d, foot, 18, 1744)):
            text(d, 88, 978 + i * 29, ln, size=18, fill=MUTED)
    return img, d


def save(img, name):
    out = Path(__file__).resolve().parent.parent / "docs" / "diagrams"
    out.mkdir(parents=True, exist_ok=True)
    path = out / f"{name}.png"
    img.resize((W, H), Image.LANCZOS).save(path, optimize=True)
    print(f"{path}  {path.stat().st_size // 1024} KB")


# ---------------------------------------------------------------- 图 0 总览
def fig_overview():
    img, d = frame(
        "一条论文的旅程",
        "每一步都为下一步准备好它需要的东西；分块用的“语义边界”是翻译那一次调用顺手带出来的。",
        "关键点：解析的产物是地基（后面全部功能都建在它上面）；翻译一次生成原文与译文两份产物；"
        "分块在需要检索时才有意义，而它的边界信息早在翻译时就已经拿到。",
    )
    stages = [
        ("导入 PDF", "原件 + 元数据"),
        ("解析", "有类型的块 + 阅读顺序"),
        ("翻译", "原文转录 + 中文译文"),
        ("分块", "语义完整的小段"),
        ("建索引", "向量库"),
        ("论文精读", "整篇解读"),
        ("阅读 · 问答", "界面"),
    ]
    n = len(stages)
    gap = 26
    bw = (1744 - gap * (n - 1)) / n
    x0, y0, bh = 88, 300, 116
    for i, (t, s) in enumerate(stages):
        x = x0 + i * (bw + gap)
        chip(d, x, y0, bw, bh, t, sub=s, accent=(i in (1, 2, 3)))
        if i:
            arrow(d, [(x - gap + 4, y0 + bh / 2), (x - 4, y0 + bh / 2)], w=3, size=12)

    # 反馈：分块的边界来自翻译
    i_fen = 2
    i_tr = 1
    x_tr = x0 + i_tr * (bw + gap) + bw / 2
    x_fen = x0 + i_fen * (bw + gap) + bw / 2
    y_fb = y0 + bh + 86
    line(d, (x_fen, y0 + bh), (x_fen, y_fb), color=WARN, w=3, dash=True)
    line(d, (x_fen, y_fb), (x_tr, y_fb), color=WARN, w=3, dash=True)
    arrow(d, [(x_tr, y_fb), (x_tr, y0 + bh + 4)], color=WARN, w=3, size=12)
    text(d, (x_tr + x_fen) / 2, y_fb + 14,
         "“哪里是语义边界”是在翻译那一次调用里一起产出的 → 分块不必重读一遍论文",
         size=18, fill=WARN, anchor="ma")

    # 下半区：索引与消费端
    y1 = 640
    rbox(d, 88, y1, 1744, 250, fill=PANEL, border=GRID, r=18)
    text(d, 128, y1 + 30, "建索引时（一次性）", size=21, bold=True, fill=ACCENT_DEEP)
    xs = [("每个小段\n（含图 / 表 / 公式）", 300), ("embedding 模型\n→ 向量", 320),
          ("向量库\n（可检索）", 280)]
    cx = 128
    for i, (t, w) in enumerate(xs):
        chip(d, cx, y1 + 78, w, 96, t.split("\n")[0], sub=t.split("\n")[1],
             fill=BOX, size=18, sub_size=15)
        if i:
            arrow(d, [(cx - 44 + 6, y1 + 126), (cx - 6, y1 + 126)], w=3, size=11)
        cx += w + 44
    text(d, 1260, y1 + 96, "提问时（每次）", size=21, bold=True, fill=ACCENT_DEEP)
    text(d, 1260, y1 + 136, "提问 → 向量检索 + 词法检索\n→ 按名次融合 → 重排模型精选\n→ 组装上下文 → 大模型生成\n→ 校验：答案里必须有出处",
         size=17, fill=MUTED, spacing=1.45)
    arrow(d, [(1010, y1 + 126), (1246, y1 + 126)], w=3, size=12)
    save(img, "00-总览")


# ---------------------------------------------------------------- 图 1 解析
def fig_parse():
    img, d = frame(
        "第一步 · 让机器读懂 PDF",
        "PDF 不是数据，它是一张“排好版的纸”：只记录“哪个字符画在哪个坐标”。",
        "这一章的结论：把“印刷品”变成“有类型的数据”，是后面所有功能的前提；"
        "扫描件（一个字符都抽不出来）改走视觉模型转录，之后环节完全一样。",
    )
    # 左：页面示意（双栏）
    px, py, pw, ph = 88, 232, 330, 560
    rbox(d, px, py, pw, ph, fill=BOX, border=BORDER, r=10)
    text(d, px + pw / 2, py - 34, "PDF：一张排好版的纸", size=19, bold=True, anchor="ma")
    col_w = 128
    for c in range(2):
        cx = px + 26 + c * (col_w + 22)
        yy = py + 34
        for i in range(16):
            wline = col_w - (34 if (i == 5 and c == 1) else 0)
            line(d, (cx, yy), (cx + wline, yy), color=(206, 213, 222), w=6, dash=False)
            yy += 21
        # 右栏塞一个小图
        if c == 1:
            rbox(d, cx, py + 148, col_w, 92, fill=(228, 233, 240), border=BORDER, r=6)
            text(d, cx + col_w / 2, py + 194, "图", size=22, bold=True, fill=MUTED, anchor="mm")
    text(d, px + pw / 2, py + ph + 16, "双栏：按坐标从上往下读会串行", size=16, fill=MUTED, anchor="ma")

    # 中：箭头
    arrow(d, [(px + pw + 40, py + ph / 2), (px + pw + 190, py + ph / 2)], w=4, size=16)
    text(d, px + pw + 115, py + ph / 2 - 44, "重新拼装", size=19, bold=True,
         fill=ACCENT_DEEP, anchor="ma")

    # 右：有类型的块
    bx, by, bwid = 700, 232, 470
    blocks = [("标题", "Introduction", ACCENT_SOFT),
              ("段落", "正文按阅读顺序排好", BOX),
              ("公式", "变成能渲染的数学公式", BOX),
              ("插图 + 图注", "图和它的说明配在一起", BOX),
              ("表格", "独立成块，不混进正文", BOX),
              ("参考文献", "单独归类", BOX)]
    yy = by
    text(d, bx, by - 34, "有类型的块 + 正确的阅读顺序", size=19, bold=True)
    for i, (t, sub, fill) in enumerate(blocks):
        rbox(d, bx, yy, bwid, 74, fill=fill, border=BORDER, r=10)
        d.rounded_rectangle([bx * S, (yy + 16) * S, (bx + 6) * S, (yy + 58) * S],
                            radius=3 * S, fill=ACCENT)
        text(d, bx + 26, yy + 16, t, size=18, bold=True)
        text(d, bx + 26, yy + 43, sub, size=15, fill=MUTED)
        yy += 86

    # 右栏：为什么
    nx = 1230
    rbox(d, nx, 232, 600, 560, fill=PANEL, border=GRID, r=16)
    text(d, nx + 34, 266, "为什么不能直接读", size=21, bold=True, fill=ACCENT_DEEP)
    notes = [
        "① PDF 里没有“段落”，只有一堆画在坐标上的字符；",
        "② 双栏排版按坐标从上往下读，会把左右两栏串在一起；",
        "③ 公式在 PDF 里可能只是一堆互不相干的符号碎片；",
        "④ 扫描件更极端：一个字符都抽不出来。",
    ]
    yy = 322
    for t in notes:
        yy = para(d, nx + 34, yy, t, size=18, maxw=528, fill=INK) + 14
    line(d, (nx + 34, yy + 4), (nx + 566, yy + 4), color=GRID, w=2)
    para(d, nx + 34, yy + 26,
         "所以：这几步不做好的话，后面翻译、检索、问答全部建在歪地基上。",
         size=18, maxw=528, fill=ACCENT_DEEP)
    save(img, "01-解析")


# ---------------------------------------------------------------- 图 2 翻译
def fig_translate():
    img, d = frame(
        "第二步 · 一次调用，两份产物",
        "一页交给大模型一次：给整页图片 + 页内文本，要它同时给出原文转录和中文译文。",
        "为什么一次要两份：两份出自同一次生成，段落划分天然一致，"
        "于是“原文和译文逐段对照”根本不需要对齐算法——这是整个项目最省事的一个决定。",
    )
    # 顶部：固定背景资料
    rbox(d, 88, 196, 1744, 82, fill=ACCENT_SOFT, border=ACCENT, r=14)
    text(d, 120, 224, "固定背景资料（论文领域 · 统一术语 · 语体风格）", size=20, bold=True,
         fill=ACCENT_DEEP)
    text(d, 120, 252, "每一页都一样，永远放在最前面 → 服务商能复用缓存，又快又便宜（所以里面绝不能混页码、时间这类每次都变的东西）",
         size=16, fill=MUTED)

    # 左：两个输入
    ix, iw = 88, 360
    text(d, ix, 330, "输入", size=21, bold=True, fill=ACCENT_DEEP)
    chip(d, ix, 372, iw, 118, "整页图片", sub="150dpi 渲染的整页", fill=BOX, size=20)
    chip(d, ix, 512, iw, 118, "页内文本", sub="解析产物（可能不准，只作参考）", fill=BOX,
         size=20, sub_size=14)
    arrow(d, [(ix + iw + 26, 431), (684, 431)], w=3, size=13)
    arrow(d, [(ix + iw + 26, 571), (684, 571)], w=3, size=13)
    arrow(d, [(860, 288), (860, 366)], w=3, size=13)
    text(d, 882, 306, "每一页都附上，放在最前面", size=16, fill=MUTED)

    # 中：模型
    mx, mw, my, mh = 700, 320, 372, 330
    rbox(d, mx, my, mw, mh, fill=BOX, border=ACCENT, r=18)
    text(d, mx + mw / 2, my + 44, "大模型", size=27, bold=True, fill=ACCENT_DEEP,
         anchor="ma")
    text(d, mx + mw / 2, my + 92, "一次调用", size=19, fill=MUTED, anchor="ma")
    line(d, (mx + 40, my + 136), (mx + mw - 40, my + 136), color=GRID, w=2)
    para(d, mx + 34, my + 160,
         "读图 + 读文本\n→ 输出一份结构化结果", size=18, maxw=mw - 68, fill=INK)
    text(d, mx + 34, my + 256, "第 3 章会在这里再加一项", size=15, fill=WARN)
    text(d, mx + 34, my + 281, "“语义边界标记”", size=15, bold=True, fill=WARN)

    # 右：两个输出
    ox, ow = 1130, 560
    arrow(d, [(mx + mw + 20, 440), (ox - 24, 440)], w=4, size=15)
    rbox(d, ox, 372, ow, 142, fill=BOX, border=BORDER, r=14)
    text(d, ox + 28, 396, "英文原文（忠实转录）", size=21, bold=True)
    text(d, ox + 28, 432, "公式、表格、图注照抄清楚", size=16, fill=MUTED)
    text(d, ox + 28, 462, "解析不准的地方由模型读图修正", size=16, fill=MUTED)
    rbox(d, ox, 560, ow, 142, fill=OK_SOFT, border=OK, r=14)
    text(d, ox + 28, 584, "中文译文", size=21, bold=True, fill=(11, 92, 48))
    text(d, ox + 28, 620, "逐段对应的中文", size=16, fill=MUTED)
    text(d, ox + 28, 650, "参考文献原样保留，不翻译", size=16, fill=MUTED)
    line(d, (ox + ow / 2, 522), (ox + ow / 2, 552), color=ACCENT, w=3)
    head(d, (ox + ow / 2, 522), (ox + ow / 2, 552), color=ACCENT, size=11)
    head(d, (ox + ow / 2, 552), (ox + ow / 2, 522), color=ACCENT, size=11)
    text(d, ox + ow / 2 + 26, 537, "两份产物 · 段落一一对应", size=17, bold=True,
         fill=ACCENT_DEEP, anchor="lm")

    rbox(d, 88, 700, 1744, 156, fill=PANEL, border=GRID, r=16)
    text(d, 120, 730, "所以“对齐”不是一个要解决的问题", size=22, bold=True, fill=ACCENT_DEEP)
    para(d, 120, 774,
         "两份内容在同一次生成里成对产出，段落切分天然一致：块数相同就按序号一一对应，"
         "根本用不着锚点、用不着序列对齐算法、更用不着翻译失败的回退补译。",
         size=19, maxw=1680, fill=INK)
    save(img, "02-翻译")


# ---------------------------------------------------------------- 图 3 分块
def fig_chunk():
    img, d = frame(
        "第三步 · 分块：RAG 逼出来的解法",
        "要做检索问答，就得把论文切成“语义完整的小段”——切得好不好，直接决定 RAG 的上限。",
        "分工是这一步的关键：模型只回答“这里是不是一个自然的语义边界”，"
        "切多大多小、结构会不会破，全由代码决定。",
    )
    # 左：两个边界来源
    text(d, 88, 208, "边界从哪来", size=21, bold=True, fill=ACCENT_DEEP)
    rbox(d, 88, 250, 520, 118, fill=BOX, border=BORDER, r=14)
    text(d, 116, 272, "论文目录（宏观边界）", size=19, bold=True)
    text(d, 116, 306, "小节之间不许因为“长度刚好合适”被合并", size=16, fill=MUTED)
    rbox(d, 88, 392, 520, 118, fill=ACCENT_SOFT, border=ACCENT, r=14)
    text(d, 116, 414, "大模型标出的语义边界（节内）", size=19, bold=True, fill=ACCENT_DEEP)
    text(d, 116, 448, "翻译那一次调用顺手产出，不用重读一遍", size=16, fill=MUTED)
    arrow(d, [(628, 309), (700, 309), (700, 470), (762, 470)], w=3, size=13)
    arrow(d, [(628, 451), (762, 451)], w=3, size=13)

    # 中：代码拼装
    mx, my, mw, mh = 790, 250, 520, 470
    rbox(d, mx, my, mw, mh, fill=BOX, border=ACCENT, r=18)
    text(d, mx + mw / 2, my + 30, "代码按行拼装", size=23, bold=True, fill=ACCENT_DEEP,
         anchor="ma")
    line(d, (mx + 30, my + 76), (mx + mw - 30, my + 76), color=GRID, w=2)
    steps = [
        ("1", "最小单位是“行”，一行永远不许被切开"),
        ("2", "从标出的边界里挑最近的合法位置"),
        ("3", "往上合并到合适大小（有软阈值和硬上限）"),
        ("4", "校验：覆盖完整 / 顺序不变 / 不跨章节 / 公式表格完整"),
        ("5", "校验不通过 → 退回行边界（宁可小，也绝不切坏）"),
    ]
    yy = my + 100
    for num, t in steps:
        d.ellipse([(mx + 30) * S, (yy + 2) * S, (mx + 58) * S, (yy + 30) * S],
                  fill=ACCENT)
        text(d, mx + 44, yy + 16, num, size=15, bold=True, fill=(255, 255, 255), anchor="mm")
        yy = para(d, mx + 74, yy + 1, t, size=17, maxw=mw - 110, fill=INK) + 12

    # 右：结果
    rx = 1360
    text(d, rx, 208, "结果", size=21, bold=True, fill=ACCENT_DEEP)
    rbox(d, rx, 250, 470, 200, fill=OK_SOFT, border=OK, r=14)
    text(d, rx + 28, 274, "一小段 = 一个检索单位", size=19, bold=True, fill=(11, 92, 48))
    para(d, rx + 28, 310,
         "内容由“行”重新拼装，绝不重新生成——所以它和原文逐字一致，能点回原文对应位置。",
         size=16, maxw=414, fill=INK, spacing=1.6)
    rbox(d, rx, 474, 470, 246, fill=WARN_SOFT, border=WARN, r=14)
    text(d, rx + 28, 498, "为什么不会切坏公式和表格", size=19, bold=True, fill=WARN)
    para(d, rx + 28, 534,
         "因为最小单位是“行”，而公式、表格、图注各自都是完整的行——"
         "它们从原理上就不可能被拦腰截断，不靠模型自觉。",
         size=16, maxw=414, fill=INK, spacing=1.6)

    # 底部：对比
    by = 766
    rbox(d, 88, by, 830, 150, fill=WARN_SOFT, border=WARN, r=16)
    cross(d, 132, by + 46, r=13)
    text(d, 160, by + 30, "最初的做法：按字数硬切 / 按自然段切", size=19, bold=True, fill=WARN)
    para(d, 132, by + 76, "一句话、一个公式、一段论证被拦腰截断；检索到半句话，答案必然是错的。",
         size=17, maxw=760, fill=INK)
    rbox(d, 946, by, 886, 150, fill=OK_SOFT, border=OK, r=16)
    tick(d, 990, by + 46, r=13)
    text(d, 1018, by + 30, "现在的做法：模型标边界 + 代码拼装", size=19, bold=True,
         fill=(11, 92, 48))
    para(d, 990, by + 76, "模型只管判断语义，结构完整性由代码保证；不通过校验就退回行边界。",
         size=17, maxw=812, fill=INK)
    save(img, "03-分块")


# ---------------------------------------------------------------- 图 4 RAG
def fig_rag():
    img, d = frame(
        "第四步 · 建索引与检索问答（RAG）",
        "把每个小段变成向量存起来；提问时两路召回、按名次融合、再用重排模型精选，最后才交给大模型作答。",
        "两条底线：① 重排模型没配时会自动降级成词法启发式，并且会明确标注“已降级”，"
        "不让它看起来像模型打分；② 答案必须能对上检索到的原文，对不上就明说依据不足。",
    )
    # 上半：建索引
    rbox(d, 88, 196, 1744, 208, fill=PANEL, border=GRID, r=16)
    text(d, 122, 222, "建索引时（一次性）", size=21, bold=True, fill=ACCENT_DEEP)
    seq = [("每个小段", "含图 / 表 / 公式"), ("embedding 模型", "把文字变成一串数字"),
           ("向量库", "相似的内容挨得近")]
    cx = 122
    for i, (t, s) in enumerate(seq):
        chip(d, cx, 272, 360, 104, t, sub=s, fill=BOX, size=19)
        if i:
            arrow(d, [(cx - 52, 324), (cx - 8, 324)], w=3, size=12)
        cx += 412
    text(d, cx - 6, 300, "之后每次提问\n都来这里找", size=17, fill=MUTED, spacing=1.5)

    # 下半：提问时
    text(d, 88, 444, "提问时（每次）", size=21, bold=True, fill=ACCENT_DEEP)
    y1, y2 = 500, 690
    chip(d, 88, y1 + 24, 176, 116, "提问", accent=True, size=21)
    # 两路召回
    arrow(d, [(272, y1 + 82), (330, y1 + 82), (330, y1 + 62), (372, y1 + 62)], w=3, size=12)
    arrow(d, [(330, y1 + 82), (330, y1 + 122), (372, y1 + 122)], w=3, size=12)
    chip(d, 380, y1 + 10, 372, 100, "向量检索", sub="把问题也变成向量，找最近的段", size=19,
         sub_size=14)
    chip(d, 380, y1 + 130, 372, 100, "词法检索", sub="关键词精确匹配，专名 / 数字更稳", size=19,
         sub_size=14)
    arrow(d, [(760, y1 + 60), (820, y1 + 60), (820, y1 + 120), (860, y1 + 120)], w=3, size=12)
    arrow(d, [(760, y1 + 180), (820, y1 + 180), (820, y1 + 120), (860, y1 + 120)], w=3, size=12)
    chip(d, 868, y1 + 70, 300, 100, "按名次融合", sub="只看名次，分数不可比", size=19, sub_size=14)
    arrow(d, [(1176, y1 + 120), (1240, y1 + 120)], w=3, size=13)

    chip(d, 1248, y1 + 20, 584, 200, "重排模型", sub="把候选逐条和问题放在一起看，重新排序，只留前几名",
         accent=True, size=23, sub_size=15)

    # 第二行
    chain = [("组装上下文", "把选中的段落拼成材料"), ("大模型生成", "只照着材料回答"),
             ("校验", "答案里的说法必须对得上材料")]
    cx = 88
    arrow(d, [(1540, y1 + 228), (1540, y2 + 50)], w=3, size=13)
    line(d, (1540, y2 + 50), (88 + 240, y2 + 50), color=ACCENT, w=3)
    arrow(d, [(88 + 240, y2 + 50), (88 + 240, y2 + 96)], w=3, size=13)
    for i, (t, s) in enumerate(chain):
        chip(d, cx, y2 + 104, 430, 116, t, sub=s, fill=BOX, size=20, sub_size=15)
        if i:
            arrow(d, [(cx - 46, y2 + 162), (cx - 6, y2 + 162)], w=3, size=12)
        cx += 470
    arrow(d, [(cx - 40, y2 + 162), (cx + 20, y2 + 162)], w=3, size=13)
    rbox(d, cx + 28, y2 + 76, 316, 172, fill=OK_SOFT, border=OK, r=16)
    text(d, cx + 52, y2 + 100, "答案", size=22, bold=True, fill=(11, 92, 48))
    text(d, cx + 52, y2 + 138, "+ 可以点回原文的引用", size=17, fill=INK)
    text(d, cx + 52, y2 + 172, "每条结论都能核对", size=16, fill=MUTED)
    save(img, "04-RAG")


# ---------------------------------------------------------------- 图 5 Agent
def fig_agent():
    img, d = frame(
        "第五步 · 问答 Agent：能力给足，边界锁死",
        "它不能直接碰数据库和文件，只能通过几个规定好的工具去查——所以“它能看见什么”是可控的。",
        "另外两条：它不持有模型密钥（密钥只在设置页里配，是唯一入口）；"
        "它和主程序是两个独立进程，跑飞了也不拖垮正在进行的解析与翻译任务。",
    )
    # 主链路
    rbox(d, 88, 430, 218, 150, fill=BOX, border=BORDER, r=14)
    text(d, 197, 482, "浏览器 · 阅读器", size=19, bold=True, anchor="ma")
    text(d, 197, 520, "提问就发生在这里", size=15, fill=MUTED, anchor="ma")
    arrow(d, [(308, 505), (372, 505)], w=3, size=13)
    text(d, 340, 468, "提问", size=16, fill=MUTED, anchor="ma")

    rbox(d, 380, 400, 300, 210, fill=ACCENT_SOFT, border=ACCENT, r=16)
    text(d, 530, 430, "主后端", size=22, bold=True, fill=ACCENT_DEEP, anchor="ma")
    text(d, 530, 470, "鉴权 · 会话读写", size=16, fill=INK, anchor="ma")
    text(d, 530, 498, "原样转发、不解释", size=16, fill=INK, anchor="ma")
    text(d, 530, 542, "（唯一的审批点在这里）", size=15, fill=MUTED, anchor="ma")

    arrow(d, [(686, 505), (752, 505)], w=3, size=13)
    text(d, 719, 468, "内部令牌", size=16, fill=MUTED, anchor="ma")

    rbox(d, 760, 380, 420, 250, fill=BOX, border=ACCENT, r=16)
    text(d, 970, 410, "Agent 服务（独立进程）", size=21, bold=True, fill=ACCENT_DEEP,
         anchor="ma")
    line(d, (792, 448), (1148, 448), color=GRID, w=2)
    text(d, 792, 470, "大模型 + 工具白名单", size=18, fill=INK)
    text(d, 792, 508, "它只能“调用工具”，", size=16, fill=MUTED)
    text(d, 792, 534, "不能自己跑去读库", size=16, fill=MUTED)
    text(d, 792, 574, "→ 工具再去问主后端", size=17, bold=True, fill=ACCENT_DEEP)

    # 右：工具清单
    tx, ty, tw = 1226, 300, 606
    rbox(d, tx, ty, tw, 470, fill=PANEL, border=GRID, r=16)
    text(d, tx + 30, ty + 26, "它能用的工具（就这几个）", size=20, bold=True, fill=ACCENT_DEEP)
    tools = [("在本篇论文里检索", "找相关段落"), ("读某几页的正文", "按页取"),
             ("把整页渲染成图给模型看", "看版面和图"), ("看某个图 / 表的裁图", "数字只能走这条"),
             ("在整个库里检索", "哪篇讲过 X"), ("写高亮 / 笔记", "唯一会改数据的工具")]
    yy = ty + 74
    for t, s in tools:
        rbox(d, tx + 30, yy, tw - 60, 58, fill=BOX, border=BORDER, r=10)
        text(d, tx + 48, yy + 10, t, size=17, bold=True)
        text(d, tx + tw - 78, yy + 12, s, size=15, fill=MUTED, anchor="ra")
        yy += 66

    # 底部：被挡住的两条（整条横幅，避免文字被右侧面板压住）
    by = 790
    rbox(d, 88, by, 1744, 168, fill=(255, 244, 244), border=(214, 122, 122), r=16)
    text(d, 124, by + 22, "被明确挡住的两条", size=20, bold=True, fill=(168, 36, 36))
    line(d, (970, 630), (970, by - 2), color=(200, 90, 90), w=3, dash=True)
    items = [(158, "不能直连数据库 / 向量库 / 文件"),
             (1040, "不持有模型密钥（密钥只在设置页里配）")]
    for x, t in items:
        cross(d, x, by + 92, r=14)
        text(d, x + 32, by + 76, t, size=19, bold=True, fill=(168, 36, 36))
    para(d, 158, by + 118,
         "所以“它能看见什么”只有一个审批点；想加能力就加一个工具，而不是把库整个敞开。",
         size=16, maxw=1620, fill=MUTED)
    save(img, "05-Agent边界")


def main():
    fig_overview()
    fig_parse()
    fig_translate()
    fig_chunk()
    fig_rag()
    fig_agent()


if __name__ == "__main__":
    main()
