"""插图区域：**按图注/标题锚定**收紧解析阶段给出的插图 bbox，并决定裁图的真实边界。

为什么需要它
------------
解析阶段检测插图区域时会"合并"：TurboQuant p8 的 Figure 3 记录 bbox 从 y=95.8 一直到
y=535.0，把**上面的 Figure 2 和中间的小标题**都吃了进来，裁出来就是"同一张图出现两次、
图里夹着正文"，完全不像论文里的插图。

规则（论文自己的排法）
----------------------
- 图注在图的**下方**（Figure）→ 下边界 = 图注顶边；上边界取以下三者的最大值：
  图块自身 y0、同页上一张图的已用下边界、图注上方最近的**标题/图注**的底边。
  中间夹着的标题（如 ``2.2 NEEDLE-IN-A-HAYSTACK``）因此被排除在图外。
- 题注在表的**上方**（Table）→ 上边界 = 题注底边；下边界取表块 y1 与下方最近标题/图注的顶边。
- 收紧后若高度过小（< 24pt），退回原始 bbox（宁可多裁一点也不要裁空）。

在此之上还有三道"多截/少截"的闸门（用户报过"一张图被切掉标题或图注"）：

1. **贴边闸门**（``_snap_edges``）：排版软件给文字块的 bbox 比字形墨迹宽出 1~2pt，
   解析阶段给的插图 bbox 又是"位图 + 矢量"的并集 —— 两个边界差一点点的结果，就是
   图注顶部被切掉一条、或者图体底部一小条留在裁图外。这一步把四条边统一贴到相邻文字的
   边上（既不让边界从字里穿过，也不留会被切掉的白缝）。
2. **墨迹裁白闸门**（``render_crop_png(ink_trim=True)``，见 ``parsing.extract.render``）：
   按 bbox 裁出来四周常有一圈白边（attention p3 右侧近 40% 是空白），先低倍渲染量出真实
   墨迹范围，再按墨迹范围出图 —— 白边被裁掉，图在栏里也更大更清楚。
3. **一份不够就切两份**（``_parts_of`` + 跨页续图）：一张图覆盖了别的图注（TurboQuant p8 的
   Figure 3 框住了 Figure 2 的图注）或跨到页脚（图体延续到下一页）时，硬裁成一张必然
   既多截又少截。此时把它拆成**同页两段**或**跨页两段**，每段各自贴边收紧；两段共享同一个
   region 下标，用 ``part`` / ``parts`` 区分，接口 URL 与落盘缓存都按 part 命名。

裁图按需渲染（600dpi，只渲染 clip 区域）并落盘缓存；缓存文件名带 bbox 哈希，
重解析后 bbox 变了会重新裁。
"""

from __future__ import annotations

import hashlib
import re
import threading
from pathlib import Path
from typing import Any

from backend.core.errors import NotFoundError
from backend.core.logging import get_logger
from backend.core.utils import json_loads
from backend.parsing.models import Rect

log = get_logger(__name__)

NUMBER_RE = re.compile(r"(?:Figure|Fig\.?|Table|图|表)\s*(\d{1,3})", re.IGNORECASE)
STRUCT_TYPES = ("caption", "heading")     # 结构分隔元素：图注与标题
SEP_PAD = 6.0        # 分隔元素（标题/图注）要往外让开一点：字形墨迹会超出文本框
TEXT_PAD = 1.2       # 边界与文字的"安全距离"：小于它就会被出图余量切到字形
TOL = 1.0            # 边界"正好贴着文字"的容差
MIN_VISIBLE = 1.5    # 区域至少要有这么宽，否则放弃贴边
SNAP_MAX = 16.0      # 一次最多把边界挪多少 pt（文字带高度量级）——防止"一挪就挪掉半个图"
MIN_HEIGHT = 24.0    # 区域至少要有这么高，否则放弃贴边
EDGE_MARGIN = 0.02   # 图体贴到页脚/页首这个比例以内 → 认为它跨到了下一页
CROP_DPI = 600
#: 裁图**算法版本**，进区域哈希（见 `entry()` 里的 hash）。
#: 改了裁图/图注/拼接逻辑就 +1 —— 否则磁盘上的旧卡片与浏览器里 7 天缓存都会继续被用
#: （实测事故：修好"卡片图注多了一句正文"之后，用户重解析仍看到旧图，就是哈希没变）。
#: 2 = 表格区域取表体 + 同图号去重 + 卡片图注只取挨着的那一串（2026-09-27）。
#: 3 = 图注续行加"句末即停 / 不得越出本栏宽"（DAMMFND 图 1 吞了整段摘要）。
#: 4 = 图体按图注分段（一页多图互不吞并，见 `parsing.structure.figure_cut` 文件头）。
#: 5 = 图体上边界不再被**墨迹带内**的"标题"（其实是图内标注）顶下来（9984-boomerang 图 9 只剩半截）。
#: 6 = 图注锚点只取"本栏那些行/半边（span 段）"（两栏的行被并成同一个文本块甚至同一条 line 时，
#:     整块 bbox 会横跨整页 → 图体裁出"图 + 另一栏整片正文"）；跨页同图号去重。
#: 7 = 多段图**每段都带图号**（第 1 段曾是无图号区域，去重看不见它 → LMF 假图的第二段活了下来）。
#: 8 = 卡片图注条只取"与图注第一段同栏"的矩形（两栏同基线的文字被并成一个块时，
#:     图注条右边会挂着另一栏的参考文献）。
#: 9 = 图注条按**图注锚点 ∪ 图体**的横向带裁回本栏（8 只按"同栏"判会失效：合并块的 rect
#:     本身就是整页宽，只能裁不能丢）。
#: 10 = 图体下边界硬闸：图注永远不进图体（DeepSeek 图 1 卡片底部挂着半个图注行）。
#: 11 = 新增「图片是否包含图注」开关（导入时选、解析时固化）：关掉时 `card=1` 也不再拼图注。
#:      必须 +1 —— 不然浏览器里 `?v=<hash>` 那张"带图注的卡片"还会被用 7 天。
#: 12 = 解析层的图体 bbox 变了（正文行内公式的分数线不再算图体墨迹 → turboquant 图 1
#:      不再把上面三条 bullet 一起裁进来；同时紧贴图体上沿的子图标题被收进图体）。
#:      必须 +1 的原因同上：bbox 虽然进了哈希，但**卡片**那几条（`card=1`）的 URL 也带版本串。
#: 13 = 解析层的图体 bbox 又变了（跨栏居中题注也算分段界标 → KAT 图 3 不再吞掉图 2；
#:      压在题注上的整幅位图不再被漏掉 → 2409 的图 8 能出来了）。
#: 14 = 区域边界新增"精确来源"这一档（在线解析 MinerU 给的版面 bbox 直接用，不再收紧/贴边）。
#:      ⚠️ **故意不 +1 到 14**：这一档只对 `audit_json["source"] == "online"` 的图生效，
#:      存量论文（本地解析：source 为空；扫描件：`scan`）的区域与哈希一字不变 ——
#:      bump 只会让全库插图在下次访问时重裁一遍 600dpi，纯属浪费。

REGION_CACHE_VERSION = 13
INK_PAD = 1.5        # 墨迹裁白后四周留的呼吸位（pt）

_lock = threading.Lock()
# {paper_id: (ir_mtime, {page: [regions]})}
_cache: dict[str, tuple[float, dict[int, list[dict]]]] = {}
# {paper_id: (ir_mtime, options)} —— 解析时固化的插图开关（见 `parse_options`）
_opts_cache: dict[str, tuple[float, dict]] = {}

#: 解析时固化的解析开关（见 `parse_options`）。
#:
#: - ``figure_caption`` / ``deep_figure_search``：两个插图开关（bool）；
#: - ``engine``：**这篇论文是用哪条引擎解析的**（``local`` / ``mineru``）。它不是裁图
#:   参数，但必须和它们放在一起：用户点「重新解析」时不传引擎就该沿用上次那条
#:   （本地解析出来的好结果，不该因为面板换了默认值就被在线解析覆盖一遍）。
DEFAULT_PARSE_OPTIONS: dict[str, Any] = {
    "figure_caption": False,        # 裁出的插图是否带图注
    "deep_figure_search": False,    # 无图注页是否兜底裁图（界面文案「深度图片搜索」）
    "engine": "local",              # 解析引擎：local（PyMuPDF 自研版面）/ mineru（在线）
}
#: 每条开关的取值类型（读 IR 时按它归一，避免"字符串 'false' 被当 True"）
_OPTION_CASTERS = {
    "figure_caption": bool,
    "deep_figure_search": bool,
    "engine": str,
}


def parse_options(paper_id: str) -> dict:
    """该论文**解析时固化**的插图开关（IR ``paper.stats_json`` → ``parse_options``）。

    为什么从 IR 读：裁图是**按需渲染**的（600dpi，请求来了才裁），"要不要拼图注"这个
    决定必须随解析产物一起留下来，否则服务层无从知道用户当初选了什么。IR 本来就是
    "这篇论文是怎么解析出来的"的留痕，放这里既不用新开文件、也不用改库表；按 IR mtime
    缓存，一次解析只解析一遍那段 JSON。

    读不到（从未解析过 / 旧 IR 没这个字段 / 文件损坏）一律按默认值：两个插图开关**全关**，
     引擎按 `local` —— 旧论文都是本地解析出来的，这个回退正是它们的真实身份。
    """
    from backend.services.paper import paper_service

    ir_path: Path = paper_service.paper_paths(paper_id)["ir"]
    try:
        mtime = ir_path.stat().st_mtime
    except OSError:
        return dict(DEFAULT_PARSE_OPTIONS)
    with _lock:
        cached = _opts_cache.get(paper_id)
        if cached and abs(cached[0] - mtime) < 1e-6:
            return cached[1]
    opts = dict(DEFAULT_PARSE_OPTIONS)
    try:
        from backend.parsing.ir.ir_reader import IRReader

        reader = IRReader(ir_path)
        stats = json_loads(reader.paper().get("stats_json")) or {}
        raw = stats.get("parse_options") if isinstance(stats, dict) else None
        if isinstance(raw, dict):
            from backend.services.paper import mineru_service

            # ⚠️ 不能对每个键一律 `bool(...)`：`engine` 是字符串，`bool("mineru")` 恒真、
            # `bool("")` 恒假 —— 取值会被压平，前端再也读不到"这篇是在线解析的"，
            # "重新解析"也会因此切回本地引擎。所以按 `_OPTION_CASTERS` 分型读。
            for k, caster in _OPTION_CASTERS.items():
                if k not in raw:
                    continue
                value = raw[k]
                # 空值 = 这个键没写（老 IR / 手写 JSON 里可能是 null 或 ""）：保留默认，
                # 不能让 `str(None)` 变成引擎名 "None"
                if value is None or (isinstance(value, str) and not value.strip()):
                    continue
                if caster is bool:
                    opts[k] = _as_bool(value)
                else:
                    text = str(value).strip().lower()
                    # 引擎名是**闭集合**：不认识的取值一律回退默认，避免把脏数据当成
                    # 一个"以后也许会支持"的引擎名传下去（分派端会静默按本地解析跑）
                    opts[k] = text if text in mineru_service.ENGINES \
                        else DEFAULT_PARSE_OPTIONS[k]
    except Exception as exc:  # noqa: BLE001 —— 读不到就按默认值，绝不拦住出图
        log.warning("读取论文 %s 的解析开关失败（按默认值处理）：%s", paper_id, exc)
    with _lock:
        _opts_cache[paper_id] = (mtime, opts)
    return opts


def _as_bool(value: Any) -> bool:
    """字符串形式的布尔也要认（脚本/手写 JSON 里常见 ``"false"``）。"""
    if isinstance(value, str):
        return value.strip().lower() in ("1", "true", "yes", "on")
    return bool(value)


def caption_in_crop(paper_id: str) -> bool:
    """裁出的插图是否拼图注（「图片是否包含图注」开关，解析时固化）。"""
    return bool(parse_options(paper_id).get("figure_caption"))


def region_version(region: dict, paper_id: str) -> str:
    """URL 上的版本串：区域哈希 + 图注开关（**不是**给缓存文件名用的）。

    ⚠️ 光有区域哈希不够：图注开关**不改 bbox**，所以开关一变哈希不变，而响应头是
    `Cache-Control: max-age=604800` —— 浏览器会把"带图注的旧卡片"再摆 7 天
    （用户"重解析了还是老图"的老事故就是这么来的）。把开关也编进版本串。
    """
    h = str(region.get("hash") or "")
    return f"{h}c{1 if caption_in_crop(paper_id) else 0}" if h else ""



def _num(value) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return 0.0


def _bbox(row: dict) -> list[float]:
    return [_num(row.get("x0")), _num(row.get("y0")),
            _num(row.get("x1")), _num(row.get("y1"))]


def _meta(row: dict) -> dict:
    return json_loads(row.get("meta_json"), {}) or {}


def _page_bbox(row: dict) -> list[float] | None:
    """块在**页面上的真实位置**（只有影印页才有）。

    扫描页的块坐标是"等分整页的虚拟横带"（只为稳定阅读顺序），拿来当版面用会把
    区域算到十万八千里外。解析阶段量准了图注位置时写在 ``meta["page_bbox"]``。
    """
    box = _meta(row).get("page_bbox")
    if isinstance(box, (list, tuple)) and len(box) == 4:
        try:
            return [float(v) for v in box]
        except (TypeError, ValueError):
            return None
    return None


def _is_scan_region(row: dict) -> bool:
    """是不是扫描件那条路产出的插图（``audit_json["source"] == "scan"``）。

    历史口径，保留名字给既有调用点/单测用；新代码请用 :func:`_has_precise_bbox`。
    """
    return _region_source(row) == "scan"


#: 边界**已经准了**的来源：不该再被收紧/贴边/切分。
#:
#: - ``scan``：影印页的边界由页图像素墨迹定死（下面的三条收紧依赖"块 rect 就是版面位置、
#:   图内文字不进文本层"，扫描件上两条都不成立）；
#: - ``online``：MinerU 给的 bbox 来自版面模型，是"图体本身"的框 —— 我们的文本块也是
#:   它给的（没有本地那种"块边界比墨迹宽"的问题），再去贴边只会把准的框挪歪。
PRECISE_BBOX_SOURCES = ("scan", "online")


def _region_source(row: dict) -> str:
    audit = json_loads(row.get("audit_json"), {}) or {}
    return str(audit.get("source") or "")


def _has_precise_bbox(row: dict) -> bool:
    """该插图的 bbox 是否可直接采信（见 :data:`PRECISE_BBOX_SOURCES`）。"""
    return _region_source(row) in PRECISE_BBOX_SOURCES


def _number_of(text: str) -> str:
    m = NUMBER_RE.search(text or "")
    return m.group(1) if m else ""


#: 一条图注被拆成多块时，块与块之间的最大间隔（pt）—— 超过它就不是"这一串图注"了
CAPTION_CHAIN_GAP = 18.0
#: 图注的续行只能这样起头（小写字母 / 数字 / 标点）。大写开头的是另一段正文
_CONTINUES_CAPTION = re.compile(r"^[a-z0-9(\[\-—,;:.]")
#: 另一张图/表的题注（续行到它为止）
_CAPTION_HEAD = re.compile(r"^\s*(?:\*\*)?\s*(?:Figure|Fig\.?|Table|图|表)\s*\d",
                           re.IGNORECASE)


def _ends_sentence(text: str) -> bool:
    """这句话已经收尾了吗（图注的续行不会以句末标点结尾）。"""
    s = (text or "").strip()
    return bool(s) and s[-1] in ".。！？!?…"


def _caption_lines(block: dict | None) -> list[str]:
    """块文本按行切开、逐行压成单行空白（图的 alt 里不能带换行）。"""
    raw = str((block or {}).get("text_clean") or (block or {}).get("text") or "")
    return [ln for ln in (" ".join(x.split()) for x in raw.splitlines()) if ln]


def _caption_text(block_order: list[dict], row: dict, cap_block: dict | None) -> str:
    """图表记录的**完整**图注：记录里存的那条天生只有**首行**。

    ⚠️ 解析层把"以 Figure/Table N 开头的那一行"强制独立成块
    （见 parsing.extract.text_layer.group_lines_into_blocks 的 caption_break），
    而 FigureBlock.caption / TableBlock.caption 是在那一步**之前**按单个原始块取的
    （见 structure.figure_cut._caption_anchor）—— 续行因此进不了记录：实测 turboquant p9
    的表 1 只存下 "… compression methods on"，"Llama-3.1-8B-Instruct." 丢了，
    figure_list / 侧栏 / 卡片 alt 全是半句话。

    续行并没有丢：段落层把图注那几行又并回了**同一个块**（block.text 是多行的）。
    所以这里在读时把整段图注还原出来 —— 先取图注块自己到句末为止的那几行，再续接紧随
    其后、同栏、且以续行方式起头的普通块。**不回填 IR**：旧论文重解析才会有新产物，
    而这条逻辑每次读都按现有行集合拼，天然一致。
    """
    text = " ".join(str(row.get("caption") or "").split())
    lines = _caption_lines(cap_block)
    if lines and text and " ".join(lines).startswith(text):
        joined = ""
        for line in lines:                 # 块里的续行：遇到句末标点就停（别吞后面的正文）
            joined = f"{joined} {line}".strip()
            if len(joined) > len(text) and _ends_sentence(line):
                break
        if len(joined) > len(text):
            text = joined
    if not text or _ends_sentence(text) or cap_block is None:
        return text
    # 续行落在**后面的普通块**里（图注被切成 caption 行 + 若干 para 行时）
    col = _rect_of(cap_block)
    last = col.y1
    for b in block_order:
        rect = _rect_of(b)
        if rect.y0 <= last + 0.5 or str(b.get("id") or "") == str(cap_block.get("id") or ""):
            continue
        if (b.get("type") or "") in ("figure", "table", "formula", "heading"):
            break                          # 图体/表体/公式/标题都不是图注
        # 与 caption_rects 同口径：不比本栏宽多少、不越出本栏、间隔不过大
        if rect.width > col.width + 20.0:
            break
        if rect.x0 < col.x0 - 6.0 or rect.x1 > col.x1 + 6.0:
            break
        if rect.y0 - last > CAPTION_CHAIN_GAP:
            break
        lines = _caption_lines(b)
        # ⚠️ 判"是不是续行"而不是"离得近不近"：实测 DAMMFND p1 图 1 的图注后面紧跟着
        # 整段摘要（"In this section, …"），只按距离续会挂 10 块正文上去。
        if not lines or _CAPTION_HEAD.match(lines[0]) \
                or not _CONTINUES_CAPTION.match(lines[0]):
            break
        for line in lines:
            text = f"{text} {line}".strip()
            if _ends_sentence(line):
                break
        else:
            last = rect.y1
            continue
        break
    return text


def _tighten(bbox: list[float], caption: dict | None, structs: list[dict],
             page_bottom: float, *, below: bool) -> list[float]:
    """把插图 bbox 收到"图注"边上（图）或"题注 + 结构分隔"之间（表）。

    - **图**（``below=True``）：只收下边界（= 图注顶边）。上边界交给解析层的墨迹带
      （见下面那段注释：带内的"标题"多半是图内标注，拿它当上边界会切掉图的上半截）。
    - **表**（``below=False``）：上边界 = 题注底边，下边界 = 下方最近的标题/图注顶边。

    ⚠️ 分隔块必须**与本图横向重叠**（`_h_overlap`）。实测事故（3360-mmdfnd p5 图 3）：
    那幅图的 raw bbox 只覆盖左半页（x 51.5–295.7，右半页的矢量图没被图体检测吃到），
    而**右半页**有个 `heading`（x 318–558，y 85.4–109.3）被当成了"图中间的标题"——
    顶边于是收到 115.27，把图顶部那一排位图（y 84–98.6，就是"Output A/B"那一行）整条切掉。
    用户看到的就是"图片顶部被裁剪"。
    """
    x0, y0, x1, y1 = bbox
    if caption is None:
        return bbox
    cy0, cy1 = _num(caption.get("y0")), _num(caption.get("y1"))

    if below:      # 图注在图下方：下边界 = 图注顶边
        # ⚠️ **不再**用"图注上方最近的标题/图注"当上边界（2026-09-27 删掉）。
        # 解析层的 raw bbox 现在是**墨迹带**（同栏上一张图注的底边 → 本图注顶边），
        # 上一张图根本进不来；而带内的标题/图注十有八九是**图内标注** ——
        # 实测 9984-boomerang p23 图 9 的 `19 (Every 2nd layer)`（x 437~500, y 172~180）被判成
        # heading，旧口径拿它当上边界 → 图体从 78.9 收到 185.9，裁出来只剩坐标轴标签 + 图例。
        # 真正需要"排除夹在中间的标题"的排版（turboquant p8 的 Figure 3 框住了 Figure 2）
        # 已经在解析层由分段解决；这里保留的只有下边界（= 图注顶边）。
        boundary = cy0 if cy0 - y0 >= MIN_HEIGHT else y1
        if boundary - y0 < MIN_HEIGHT:
            return bbox
        return [x0, y0, x1, boundary]

    # 题注在表上方：上边界 = 题注底边；下边界 = 下方最近的标题/图注顶边
    boundary = cy1 if y1 - cy1 >= MIN_HEIGHT else y0
    top = max(y0, boundary)
    seps = [_num(s.get("y0")) - SEP_PAD for s in structs
            if _num(s.get("y0")) >= cy1 + TOL and _h_overlap(s, x0, x1)]
    bottom = min([y1] + seps[:1])
    if bottom - top < MIN_HEIGHT:
        return bbox
    return [x0, top, x1, bottom]


def _h_overlap(row: dict, x0: float, x1: float, *, tol: float = 1.0) -> bool:
    """这个块的横向范围与 (x0, x1) 有实质重叠吗（跨栏内容不算本图的上下文）。

    ⚠️ **没有横向信息时按"重叠"处理**（老产物 / 单测夹具里只有 y）：宁可回到旧行为
    （可能会收一点），也不要因为缺字段就把分隔块全部忽略。
    """
    bx0, bx1 = _num(row.get("x0")), _num(row.get("x1"))
    if bx1 <= 0.0 and bx0 <= 0.0:
        return True
    return bx1 > x0 + tol and bx0 < x1 - tol


def _text_rects(blocks_by_page: dict[int, list[dict]], page_no: int) -> list[Rect]:
    """该页**文字层**块的矩形（插图区域不该把文字圈进来）。"""
    out: list[Rect] = []
    for b in blocks_by_page.get(int(page_no), []):
        if (_num(b.get("char_count")) or 0) <= 0 and not str(b.get("text") or "").strip():
            continue
        out.append(_rect_of(b))
    return out


def _rect_key(r: Rect) -> tuple:
    """Rect → 可比较的键（`Rect` 之间要按**值**比对：图注块与文字层里的同一个块是两份对象）。"""
    return (round(r.x0, 1), round(r.y0, 1), round(r.x1, 1), round(r.y1, 1))


def _rect_of(row: dict) -> Rect:
    """块的矩形：**优先用真实页面位置**（``meta["page_bbox"]``）。

    扫描页的 ``x0/y0/x1/y1`` 是等分整页的虚拟横带，只用来稳定阅读顺序，
    当版面坐标用会把边界算错（实测：图注被推到下一页的 y 上）。
    """
    real = _page_bbox(row)
    if real:
        return Rect(*real)
    return Rect(_num(row.get("x0")), _num(row.get("y0")),
                _num(row.get("x1")), _num(row.get("y1")))


#: "边界从字里穿过"时允许被**吸进裁图**的文字块高度上限（pt）：
#: 只有单行的图内小标注 / 坐标轴刻度 / 子图标题行才这么矮，正文段落是多行的、比它高，
#: 不能被当成"图的一部分"。（宁可少裁一点点，也不要重新引入"多裁"——用户两个方向都报过。）
#: ⚠️ 判据只能用**高度**（行数），不能用宽度：实测 attention p4 图 2 的子图标题行是
#: `Scaled Dot-Product Attention Multi-Head Attention`（宽 302pt、高 10pt），
#: turboquant p21 图 5 的刻度/子图标题行更宽（370pt）—— 按宽度卡（比如 ≤200pt）
#: 会把这两个真实的少裁案例漏掉。
LABEL_MAX_HEIGHT = 22.0
#: "往外让开"的**幅度上限**（当前区域长度的比例）：只有"切得不深"才收。
#: 实测事故（3360-mmdfnd p5 图 3）：左栏图 x 51.5–295.7，而一个**跨栏合并的宽行**
#: （x 108.5–558.2，高 12.9pt —— 看上去和"单行标签"一模一样）把区域一路撑到 559.4，
#: 把右栏正文也裁了进来。它的外扩量是区域宽度的 108%，而真标签的案例都在 9% 以内
#: （attention p4 左 8.5%、turboquant p21 上 8.7%、p8 图 2 上 6.7%），所以按比例卡是安全的。
GROW_MAX_RATIO = 0.4
#: "把被切的那块整个收进来"的**第二个闸门**（只用于 `_snap_x` 的横向贴边）：
#: 露在区域外的部分不得超过块自身的 25%。
#:
#: 为什么只卡横向：横向贴边是**沿着文字行方向**挪，一次能挪走一整行的宽度 ——
#: 相邻那一栏的正文行因此在"逐块累积"里被吞进来（见下面的实测事故）；而纵向贴边
#: 只会挪一个行高，而且"边界从一行字里穿过 → 把这行字整个收进来"正是它的既有设计
#: （turboquant p8 图 2 的子图标题被切掉 7.8pt 就是靠它修的：那行字有 85% 在区域上方，
#: 若按"露在外面 ≤25%"卡就会被挡回去 —— 所以纵向不能加这条）。
#:
#: ⚠️ 实测事故（2026-09-28 KAT p3 图 3）：右栏正文被并成两块（x 306.6~452.9 与 452.9~557.6），
#: 边界先被前一块收到 454、再被后一块收到 558.8 —— **每步都在 `GROW_MAX_RATIO` 以内**，
#: 于是"逐块累积"绕过了上限：区域右边界从 286.7 一路飞到整页宽，裁图里混进一栏正文
#: （用户："图 3 裁错了"）。历史那几例真标签（attention p4 左 8.5%、turboquant p21 8.7%、
#: p8 图 2 6.7%）露在外面的都不到块宽的 10%，这条对它们是恒真的。
SNAP_INSIDE_MAX = 0.25


def _snap_y(edge: float, blocks: list[Rect], *, top: bool,
            lo: float, hi: float, avoid: set | None = None) -> float:
    """把一条水平边界推到相邻文字的边上（不动就返回原值）。

    三种情形要分开处理，**只判"离得近不近"是不够的**（实测事故，2026-09-27）：

    - **是图注/题注块**（``avoid`` 里的那个 Rect）：它是**论文的文字**，不该被裁进图里 ——
      一律按老规矩"推开"（上边界 → ``t.y1 + TEXT_PAD``，下边界 → ``t.y0 - TEXT_PAD``），
      哪怕边界正从它里面穿过。
    - **边界从字里穿过**（``t.y0 < edge < t.y1``，且不是图注）：这个字被切掉一截 →
      把边界**往外让开**，让整个字进到区域里（上边界 → ``t.y0 - TEXT_PAD``；下边界 →
      ``t.y1 + TEXT_PAD``）。分两级放宽：**单行块**（高度 ≤ :data:`LABEL_MAX_HEIGHT`，
      即图内小标注/刻度/子图标题）一律收；**多行块**只在"切得不深"（移动量 ≤ ``SNAP_MAX``）
      时收 —— 正文段落通常是多行且要移动一大截，于是被挡在门外。
    - **边界紧贴字块外沿**（不到 ``TEXT_PAD``）：把边界**再推开一点**，别把字收进图里。

    为什么必须补上第二种：旧实现只判 "edge 是否落在 ``t.y1 ± TEXT_PAD``" 这个窄带，
    **边界从字里穿过时反而落进"离得远、不用管"那一支被跳过**。实测 turboquant p8 图 2 的
    ``(a) Inner Product Error`` / ``(b) MSE`` / ``(c) Speedup`` 三个子图标题，区域顶边 95.80
    与字块底边 97.02 只差 **0.02pt**，于是被判成"已经贴住了"，结果标题被切掉 7.8pt
    （600dpi 逐行墨迹剖面：该 x 切片 **99.6% 的墨迹没进裁图**）。
    """
    for t in blocks:
        # ⚠️ 判"边界从字里穿过"**不能**在近侧再留 TOL 死带：实测 turboquant p21 图 5 的
        # `(a) TURBOQUANTprod` 块 bbox 下沿 97.00、区域顶边 96.22（只差 0.78pt < TOL=1.0），
        # 旧写法把它判成"没穿过"，于是 `prod` 的下半截被切掉（`near` 那支反过来把顶边
        # **往下推**到 98.18，越推越糟）。近侧只留远侧的 TOL 防抖。
        is_caption = bool(avoid) and _rect_key(t) in avoid
        inside = (not is_caption) and t.y0 + TOL < edge < t.y1
        short = (t.y1 - t.y0) <= LABEL_MAX_HEIGHT      # 单行 = 标签；多行 = 段落
        grow_cap = max(SNAP_MAX, GROW_MAX_RATIO * max(hi - lo, 1.0))
        if top:
            # 单行标签一律收进来；**多行块只在"切得不深"时收**（移动量 ≤ SNAP_MAX）
            if inside and (short or (edge - (t.y0 - TEXT_PAD)) <= SNAP_MAX):
                new = t.y0 - TEXT_PAD          # 把被切的那行字整个收进来
                if (edge - new) > grow_cap or (hi - new) < MIN_HEIGHT:
                    continue
            elif edge > t.y1 - TEXT_PAD and edge < t.y1 + TOL:
                new = t.y1 + TEXT_PAD          # 贴得太近（或就是图注）→ 推开，别把字收进图里
                if abs(new - edge) > SNAP_MAX or (hi - new) < MIN_HEIGHT:
                    continue
            else:
                continue
        else:
            # 下边界对称：近侧（贴着字块上沿那一带）同样不留死带
            inside_b = (not is_caption) and t.y0 < edge < t.y1 - TOL
            if inside_b and (short or ((t.y1 + TEXT_PAD) - edge) <= SNAP_MAX):
                new = t.y1 + TEXT_PAD
                if (new - edge) > grow_cap or (new - lo) < MIN_HEIGHT:
                    continue
            elif edge > t.y0 - TOL and edge < t.y0 + TEXT_PAD:
                new = t.y0 - TEXT_PAD
                if abs(new - edge) > SNAP_MAX or (new - lo) < MIN_HEIGHT:
                    continue
            else:
                continue
        edge = new
    return edge


# ⚠️ `_snap_x` 定义在 `_snap_edges` **之后**不是笔误：模块级重定义以后一份为准，
# 原先这里还留着一份判据不同的旧实现（`edge >= t.x1 + TOL`），**永远跑不到**。
# P9 已删旧的那一份，只留下面这一份生效的。
def _snap_edges(rect: list[float], texts: list[Rect], page_rect: Rect,
                avoid: list[Rect] | None = None) -> list[float]:
    """把区域的四条边**贴到相邻文字的边上**：既不从文字中间穿过，也不留一条会被切掉的白缝。

    为什么必须有这一步：排版软件给文字块的 bbox 比字形墨迹宽出 1~2pt，解析阶段的插图
    bbox 又是"位图 + 矢量"的并集，两个边界几乎不可能正好落在同一处。差一点点的结果就是
    **图注顶部被切掉一条**（多截）或**图体底部一小条留在裁图外**（少截）—— 用户报的
    "多截和少截"就是它。

    迭代四轮：一次贴边可能让另一条边落进新的危险带（尤其是上下都紧邻文字时）。
    """
    x0, y0, x1, y1 = rect
    order = sorted(texts, key=lambda t: t.y0)
    avoid_keys = {_rect_key(a) for a in (avoid or [])}
    for _ in range(4):
        before = (x0, y0, x1, y1)
        col = [t for t in order if t.x1 > x0 + TOL and t.x0 < x1 - TOL]
        y0 = _snap_y(y0, col, top=True, lo=y0, hi=y1, avoid=avoid_keys)
        y1 = _snap_y(y1, col, top=False, lo=y0, hi=y1, avoid=avoid_keys)
        row = [t for t in order if t.y1 > y0 + TOL and t.y0 < y1 - TOL]
        x0 = _snap_x(x0, row, left=True, lo=x0, hi=x1, avoid=avoid_keys)
        x1 = _snap_x(x1, row, left=False, lo=x0, hi=x1, avoid=avoid_keys)
        if (x0, y0, x1, y1) == before:
            break
    if (y1 - y0) < MIN_HEIGHT or (x1 - x0) < MIN_VISIBLE:
        return rect                                       # 收得太过 → 保留原样，宁可多裁
    return [max(x0, page_rect.x0), max(y0, page_rect.y0),
            min(x1, page_rect.x1), min(y1, page_rect.y1)]


def _snap_x(edge: float, blocks: list[Rect], *, left: bool, lo: float, hi: float,
            avoid: set | None = None) -> float:
    """把一条垂直边界贴到相邻文字的边上（与 :func:`_snap_y` 同构）。

    "边界从字里穿过"与"边界紧贴字块外沿"同样要分开（理由见 `_snap_y`）：
    前者把边界往外让开、让整个字进到区域里，后者把边界再推开一点、别把字收进来；
    图注块（``avoid``）一律按后者处理。
    旧实现在"穿过"这一支上会把边界**继续往里推**（left 推到 ``t.x1 + TEXT_PAD``），
    实测 attention p4 图 2 的副标题 ``Scaled`` 因此被切掉左端 24.2pt。
    """
    for t in blocks:
        is_caption = bool(avoid) and _rect_key(t) in avoid
        inside_l = (not is_caption) and t.x0 + TOL < edge < t.x1
        inside_r = (not is_caption) and t.x0 < edge < t.x1 - TOL
        short = (t.y1 - t.y0) <= LABEL_MAX_HEIGHT      # 同上：按**高度**判单行
        grow_cap = max(SNAP_MAX, GROW_MAX_RATIO * max(hi - lo, 1.0))
        if left:
            if (inside_l and (short or (edge - (t.x0 - TEXT_PAD)) <= SNAP_MAX)
                    and (edge - t.x0) <= SNAP_INSIDE_MAX * max(t.width, 1.0)):
                new = t.x0 - TEXT_PAD          # 把被切的那个字整个收进来
                if (edge - new) > grow_cap or (hi - new) < MIN_VISIBLE:
                    continue
            elif edge >= t.x0 - TEXT_PAD and edge <= t.x1 + TEXT_PAD:
                new = t.x1 + TEXT_PAD          # 推开，别把字收进图里
                if abs(new - edge) > SNAP_MAX or (hi - new) < MIN_VISIBLE:
                    continue
            else:
                continue
        else:
            if (inside_r and (short or ((t.x1 + TEXT_PAD) - edge) <= SNAP_MAX)
                    and (t.x1 - edge) <= SNAP_INSIDE_MAX * max(t.width, 1.0)):
                new = t.x1 + TEXT_PAD
                if (new - edge) > grow_cap or (new - lo) < MIN_VISIBLE:
                    continue
            elif edge >= t.x0 - TEXT_PAD and edge <= t.x1 + TEXT_PAD:
                new = t.x0 - TEXT_PAD
                if abs(new - edge) > SNAP_MAX or (new - lo) < MIN_VISIBLE:
                    continue
            else:
                continue
        edge = new
    return edge


def _normalise(rect: list[float], page_rect: Rect) -> list[float]:
    """保证矩形有效且落在页面内。"""
    x0, y0, x1, y1 = rect
    x0, x1 = min(x0, x1), max(x0, x1)
    y0, y1 = min(y0, y1), max(y0, y1)
    return [max(x0, page_rect.x0), max(y0, page_rect.y0),
            min(x1, page_rect.x1), min(y1, page_rect.y1)]


def _clamp_bottom_above_caption(tight: list[float], cy0: float) -> list[float]:
    """把图体下边界收到**题注顶边**之上（题注永远不进图体）。

    ⚠️ `cy0` 必须是**图体下方**那条题注的顶边。图体是位图、题注没被分型成 caption 时，
    解析层会给这张图补一个**孤儿块**（rect = 图体本身、文本 = 题注）—— 拿它的 y0 当题注顶边，
    就会把区域砍成 `tight[1] + MIN_HEIGHT` 那么薄的一条（实测 KAT p8 图 6：102pt 高的图
    被砍成 24pt，用户截图里只剩 `Text: …` 那一行）。
    所以只认"确实在下方"（`cy0 > 图体顶边 + MIN_HEIGHT`）的题注；否则原样返回。

    顺带解释"为什么要收"：贴边 / 吸单行标签有时会把下边界推到图注第一行中间
    （实测 deepseek-v3-2 图 1：区域 y1=329.6 而图注从 319.7 开始 → 卡片底部挂着半个图注行、
    下面又拼一条完整图注，"图注看起来重复/被切"）。
    """
    if not cy0 or cy0 <= _num(tight[1]) + MIN_HEIGHT or tight[3] <= cy0:
        return tight
    return [tight[0], tight[1], tight[2],
            max(cy0 - TEXT_PAD, tight[1] + MIN_HEIGHT)]


def _parts_of(rect: list[float], inner: list[dict], page_rect: Rect
              ) -> list[tuple[list[float], dict | None]]:
    """把区域切成若干份：内部夹着别的图注、或图体跨到页脚时，一份裁不干净。

    ``inner`` 是**严格落在区域内**的其他图注（y0/y1 都在区域内、且高度不大）。
    切分点取图注的上下边（各让开 ``TEXT_PAD``），保证两段都不含图注文字：
    图注本身在正文里有自己的行，重复裁进图里就会"同一句话出现两次"。
    """
    x0, y0, x1, y1 = rect
    cuts: list[tuple[float, float]] = []
    for cap in inner:
        cy0, cy1 = _num(cap.get("y0")), _num(cap.get("y1"))
        if not (y0 + MIN_HEIGHT <= cy0 and cy1 <= y1 - MIN_HEIGHT):
            continue
        if cy1 - cy0 > 0.2 * max(1.0, y1 - y0):        # 太"厚"的不是图注，是正文
            continue
        cuts.append((cy0, cy1))
    cuts = sorted(cuts)

    pieces: list[tuple[list[float], dict | None]] = []
    cursor = y0
    for cy0, cy1 in cuts:
        if cy0 - cursor >= MIN_HEIGHT:
            pieces.append(([x0, cursor, x1, cy0 - TEXT_PAD], None))
        cursor = max(cursor, cy1 + TEXT_PAD)
    if y1 - cursor >= MIN_HEIGHT:
        pieces.append(([x0, cursor, x1, y1], None))

    # 图体压到页脚：最后一段其实是"下一页的续图"，这里先给出跨页标记
    if len(pieces) >= 1 and y1 >= page_rect.y1 - EDGE_MARGIN * page_rect.height:
        pieces[-1] = (pieces[-1][0], {"continues": True})
    if not pieces:                                      # 全被切没了 → 不切
        return [([x0, y0, x1, y1], None)]
    if len(pieces) == 1 and not pieces[0][1]:
        return [([x0, y0, x1, y1], None)]
    return pieces


def _looks_like_continuation(fig: dict, page_h: float) -> bool:
    """上一页那张图的 bbox 是否"被页脚切开"（图体一直画到页面下沿）。"""
    bottom = _num(fig.get("y1"))
    return bool(page_h) and bottom >= page_h - EDGE_MARGIN * page_h


def _continuation_page(paper_id: str, src_page: int, src: dict,
                       pages: dict[int, dict], ir) -> int | None:
    """图体跨页时，判断"下一页页首是否接着这张图"，是就返回下一页页码。

    判据很保守（宁可不切也不要乱切）：源图必须画到页脚，下一页页首必须有内容
    （文字块或矢量图形）与源图**横向重叠**。
    """
    from backend.parsing.extract.vector_layer import extract_drawings
    from backend.services.paper import paper_service

    nxt = src_page + 1
    page = pages.get(nxt)
    if page is None:
        return None
    page_h = _num(page.get("height")) or 792.0
    band = EDGE_MARGIN * page_h
    x0, x1 = _num(src.get("x0")), _num(src.get("x1"))

    def in_band(r: Rect) -> bool:
        return r.y0 <= band and not (r.x1 <= x0 or r.x0 >= x1)

    for b in ir.blocks():
        if int(b["page_no"]) != nxt:
            continue
        if (_num(b.get("char_count")) or 0) <= 0 and not str(b.get("text") or "").strip():
            continue
        if in_band(_rect_of(b)):
            return nxt
    try:
        import pymupdf as fitz

        with fitz.open(str(paper_service.resolve_pdf(paper_id))) as doc:
            for d in extract_drawings(doc[nxt - 1]):
                if d.rect.area > 25 and in_band(d.rect):
                    return nxt
    except Exception as exc:                            # noqa: BLE001
        log.warning("跨页插图探测失败（p%d）：%s", nxt, exc)
    return None


def _merge(regions: list[dict], extra: dict) -> list[dict]:
    """把续图并进同页列表，并按文档顺序（y0, y1）重排，保证下标稳定。"""
    out = regions + [extra]
    out.sort(key=lambda r: (_num(r.get("y0")), _num(r.get("y1"))))
    return out


#: 算法块标题行：Algorithm 1 TURBOQUANT: optimized for MSE（可带 ** 与 $公式$）
_ALGO_HEAD = re.compile(r"^\s*(?:\*\*)?\s*Algorithm\s*(\d{1,3})\b", re.IGNORECASE)


def _overlaps(one: list[float], two: list[float]) -> bool:
    """两个框是否明显重叠（含一半以上）—— 判断这块地盘是不是已经归图/表了。"""
    try:
        ax0, ay0, ax1, ay1 = (float(v) for v in one[:4])
        bx0, by0, bx1, by1 = (float(v) for v in two[:4])
    except (TypeError, ValueError):
        return False
    ix = min(ax1, bx1) - max(ax0, bx0)
    iy = min(ay1, by1) - max(ay0, by0)
    if ix <= 0 or iy <= 0:
        return False
    inter = ix * iy
    smaller = min((ax1 - ax0) * (ay1 - ay0), (bx1 - bx0) * (by1 - by0))
    return smaller > 0 and inter / smaller > 0.5


def _algorithm_regions(blocks: list[dict], existing: list[dict], entry, page: Rect) -> list[dict]:
    """算法块（伪代码）也当成可裁图的区域（用户口径 2026-12-07）。

    背景：page_regions 只认解析阶段的插图 / 表格区域，而算法块是 caption + 一串 para/formula，
    不在那份清单里 —— 于是 figure_list(5) 回空，想调算法 1 的裁图会落空。

    做法：从 Algorithm N 标题块起往下收同栏、紧挨着的非结构块（heading / caption 即停、
    间隔 > 40pt 即停、换栏即停），得到一条 kind=algorithm 的区域；裁图接口按索引取，
    自动就能取到。与图/表区域重叠超过一半的不要（那是它们的地盘）。
    """
    out: list[dict] = []
    ordered = sorted(blocks, key=lambda b: (_num(b.get("y0")), _num(b.get("x0"))))
    for i, block in enumerate(ordered):
        text = str(block.get("text_clean") or block.get("text") or "")
        hit = _ALGO_HEAD.match(text)
        if not hit:
            continue
        x0, y0 = _num(block.get("x0")), _num(block.get("y0"))
        x1, bottom = _num(block.get("x1")), _num(block.get("y1"))
        for nxt in ordered[i + 1:i + 40]:
            if str(nxt.get("type") or "") in STRUCT_TYPES:
                break
            if _num(nxt.get("y0")) > bottom + 40:
                break
            if _num(nxt.get("x1")) <= x0 or _num(nxt.get("x0")) >= x1:
                break
            x0 = min(x0, _num(nxt.get("x0")))
            x1 = max(x1, _num(nxt.get("x1")))
            bottom = max(bottom, _num(nxt.get("y1")))
        box = [x0, y0, x1, bottom]
        if any(_overlaps(box, r.get("raw_bbox") or r["bbox"]) for r in existing):
            continue
        caption = re.sub(r"^\s*\*\*|\*\*\s*$", "", text).strip()
        out.append(entry(int(block.get("page_no")), "algorithm", box, box,
                         caption, hit.group(1), block.get("id"), source="text"))
    return out


def _compute_all(paper_id: str) -> dict[int, list[dict]]:
    """一次读库算完整篇（按页分组，顺序 = 文档顺序）。"""
    from backend.parsing.ir.ir_reader import IRReader
    from backend.services.paper import paper_service

    ir = IRReader(paper_service.paper_paths(paper_id)["ir"])
    if not ir.exists:
        return {}
    pages = {int(p["page_no"]): p for p in ir.pages()}
    blocks_by_page: dict[int, list[dict]] = {}
    for b in ir.blocks():
        blocks_by_page.setdefault(int(b["page_no"]), []).append(b)
    figures_by_page: dict[int, list[dict]] = {}
    for f in ir.figures():
        figures_by_page.setdefault(int(f["page_number"]), []).append({**f, "_kind": "figure"})
    tables_by_page: dict[int, list[dict]] = {}
    for t in ir.tables():
        tables_by_page.setdefault(int(t["page_number"]), []).append({**t, "_kind": "table"})

    text_cache: dict[int, list[Rect]] = {}
    out: dict[int, list[dict]] = {}

    def page_rect(page_no: int) -> Rect:
        p = pages.get(page_no) or {}
        return Rect(0.0, 0.0, _num(p.get("width")) or 612.0,
                    _num(p.get("height")) or 792.0)

    def entry(page_no: int, kind: str, rect: list[float], raw: list[float],
              caption: str, number: str, caption_block_id: str | None,
              part: dict | None = None, source: str = "") -> dict:
        tight = [round(v, 2) for v in _normalise(rect, page_rect(page_no))]
        row = {
            "page": page_no, "kind": kind,
            "number": number, "caption": caption, "caption_block_id": caption_block_id,
            "bbox": tight, "raw_bbox": [round(v, 2) for v in raw],
            "source": source or "text",
            # ⚠️ 哈希 = **算法版本 + bbox**。只算 bbox 会踩两个坑（2026-09-27 实测）：
            # ① 磁盘上的旧卡片文件名字不变 → `ensure_region_crop` 直接复用旧图；
            # ② 前端 URL 的 `?v=<hash>` 不变 → 浏览器按 `Cache-Control: max-age=604800`
            #    把 7 天前的旧卡片拿出来（用户"重解析了还是老图"就是这么来的）。
            # 改了裁图/图注算法就把 `REGION_CACHE_VERSION` +1。
            "hash": hashlib.sha256(
                f"{REGION_CACHE_VERSION}|"
                f"{[round(v, 1) for v in tight]}".encode()).hexdigest()[:8],
        }
        if part:
            row.update(part)
        return row

    for page_no in pages:
        blocks = blocks_by_page.get(page_no, [])
        if not blocks:
            continue
        rect = page_rect(page_no)
        block_order = sorted(blocks, key=lambda b: (_num(b.get("y0")), _num(b.get("x0"))))
        by_id = {b["id"]: b for b in blocks}
        structs = sorted([b for b in blocks
                          if (b.get("type") or "") in STRUCT_TYPES],
                         key=lambda b: _num(b.get("y0")))
        texts = text_cache.get(page_no)
        if texts is None:
            texts = text_cache[page_no] = _text_rects(blocks_by_page, page_no)
        rows = sorted(figures_by_page.get(page_no, []) + tables_by_page.get(page_no, []),
                      key=lambda r: _num(r.get("y0")))
        regions: list[dict] = []
        prev_bottom: float | None = None
        for row in rows:
            caption = by_id.get(row.get("caption_block_id") or "")
            if caption is None and row.get("caption"):
                head = str(row["caption"])[:40]
                caption = next((b for b in blocks
                                if str(b.get("text_clean") or b.get("text") or "")
                                .strip().startswith(head)), None)
            raw = _bbox(row)
            below = row["_kind"] == "figure"
            # 记录里的 caption 只有首行（见 _caption_text）：读时按行集合补回续行
            cap_text = _caption_text(block_order, row, caption)
            # 图号仍只从**记录里那条**（首行）取：续行里出现的 "Figure 2" 是正文引用，
            # 拿它当本区域的图号会让页内/跨页去重认错人。
            number = _number_of(str(row.get("caption") or cap_text))
            cap_id = row.get("caption_block_id") or (caption or {}).get("id") or None
            scan = _has_precise_bbox(row)
            if scan:
                # 精确来源（扫描件 / 在线解析）：边界已由像素墨迹或版面模型定死，
                # 这里只做"落在页面内"的归一化。收紧/贴边/切分三段都依赖"块 rect 就是
                # 版面位置"这个前提，它们不成立（实测会把相邻图吃掉、把本图收成细条）。
                tight = [round(v, 2) for v in _normalise(raw, rect)]
                pieces: list[tuple[list[float], dict | None]] = [(tight, None)]
            else:
                seps = list(structs)
                if below and prev_bottom is not None:
                    seps = seps + [{"y0": prev_bottom, "y1": prev_bottom}]
                tight = _tighten(raw, caption,
                                 sorted(seps, key=lambda b: _num(b.get("y0"))),
                                 rect.y1, below=below)
                tight = _snap_edges(tight, texts, rect,
                                    avoid=[_rect_of(caption)] if caption else None)
                # 图注**绝不能**落在图体里（贴边/吸单行标签有时会把下边界推到图注第一行中间）。
                # ⚠️ 认题注前要先判"它确实在图体下方"，否则解析层的**孤儿块**（rect = 图体本身、
                # 文本 = 题注）会把图砍成 24pt 一条 —— 见 `_clamp_bottom_above_caption`。
                if below and caption is not None:
                    tight = _clamp_bottom_above_caption(tight, _num(caption.get("y0")))
                inner = [b for b in structs
                         if (b.get("type") or "") == "caption" and b.get("id") != cap_id
                         and _num(b.get("y0")) > tight[1] + MIN_HEIGHT
                         and _num(b.get("y1")) < tight[3] - MIN_HEIGHT]
                pieces = _parts_of(tight, inner, rect)
            for i, (piece, part) in enumerate(pieces):
                # 同一张图被切成两段时给个看得懂的标签（前端在侧栏里显示）
                label = "" if len(pieces) == 1 else ("上段" if i == 0 else "下段")
                regions.append(entry(
                    page_no, row["_kind"], piece, raw,
                    # ⚠️ 图号**每一段都要带**：以前只给第 0 段，于是 `_parts_of` 切出来的
                    # 第 1 段成了"无图号区域"——页内/跨页的去重都看不见它（实测 LMF p4 那条假图
                    # 第二段就是这么活下来的，用户数出来"4 张图截成 6 张"）。
                    cap_text, number, cap_id,
                    ({"part": i, "parts": len(pieces), "part_label": label}
                     if label else None),
                    # ⚠️ 这里要写**真实来源**（`scan` / `online`）：它决定后面"跨页续图"
                    # 那一步跳不跳（见下面 `reg.get("source") in PRECISE_BBOX_SOURCES`）。
                    # 曾经一律写 "scan"，于是"在线解析的图"会被当成扫描件而语义错位。
                    source=_region_source(row),
                ))
            if below and not scan:
                prev_bottom = max(_num(regions[-1]["bbox"][3]) - 3.0, 0.0)
        # 算法块（伪代码）也算可裁图区域 —— 必须在这里加：`blocks` / `rect` 是**本页**的；
        # 放到后面那个去重循环里会借到"最后一页"的 blocks/rect（实测 2026-12-07）。
        regions.extend(_algorithm_regions(blocks, regions, entry, rect))
        regions.sort(key=lambda r: (_num(r["bbox"][1]), _num(r["bbox"][0])))
        out[page_no] = regions

    # ---- 跨页续图：图体画到页脚、下一页页首接着它 → 下一页补一条"续图"条目 ----
    # 精确来源（扫描件 / 在线解析）不参与：判据依赖**本地解析**那套文本层真实坐标与
    # 矢量图形，影印件上两者都没有；MinerU 的结果里"跨页图"本来就以两个 item 给出，
    # 它自己已经处理过这种情形。
    for page_no in sorted(out):
        for reg in list(out[page_no]):
            if reg.get("kind") != "figure" or reg.get("part", 0) != 0:
                continue
            if reg.get("source") in PRECISE_BBOX_SOURCES:
                continue
            if not _looks_like_continuation(reg, page_rect(page_no).height):
                continue
            nxt = _continuation_page(paper_id, page_no, reg, pages, ir)
            if nxt is None:
                continue
            rect = page_rect(nxt)
            top = next((_num(b.get("y0")) for b in blocks_by_page.get(nxt, [])
                        if (_num(b.get("char_count")) or 0) > 0
                        and _num(b.get("y0")) > rect.y0 + MIN_HEIGHT), None)
            body = [reg["bbox"][0], rect.y0 + 1.0, reg["bbox"][2],
                    (top - TEXT_PAD) if top else rect.y0 + 0.5 * rect.height]
            body = _normalise(body, rect)
            if body[3] - body[1] < MIN_HEIGHT:
                continue
            out.setdefault(nxt, [])
            out[nxt] = _merge(out[nxt], entry(
                nxt, "figure", body, body, reg.get("caption") or "", "",
                None, {"part": 1, "parts": 2, "part_label": "跨页续图",
                       "continues_from": page_no}))

    # ---- 同一图号只留一条区域（用户 2026-09-27 报的"多截了一张图、图注像被拼接"）----
    #
    # 解析器会把**正文里的引用句**也判成 caption —— 实测 DTDBD p11：
    #   真图注 `dualteacherd-p11-b07`（type=caption，y 215.6，紧贴图体下沿 6.9pt）
    #   假图注 `dualteacherd-p11-b13`（type=caption，y 371.1："Figure 3. From Case 1, it can be
    #   seen that when traditional" —— 正文句子，上文里本来写的是"…as shown in Figure 3."）
    # 两条都指向同一张图（图体 y 49~208），于是同一图号出现两条区域：真那条对，
    # 另一条会裁到**正文**、并把那句正文当图注挂上去（看着就是图注被拼接）。
    #
    # 判据：**图注块到图体的距离** —— 真图注必然贴着图体（个位数 pt），正文引用离得远（这里 162pt）。
    for page_no, regs in list(out.items()):
        by_id = {b["id"]: b for b in blocks_by_page.get(page_no, [])}
        groups: dict[tuple[str, str], list[dict]] = {}
        keep: list[dict] = []
        for reg in regs:
            # 多段图（part）本来就共享图号，跳过
            if not reg.get("number") or int(reg.get("parts") or 1) > 1:
                keep.append(reg)
                continue
            groups.setdefault((reg["kind"], str(reg["number"])), []).append(reg)
        for (kind, number), items in groups.items():
            if len(items) == 1:
                keep.extend(items)
                continue

            def _cap_gap(reg: dict, _by_id=by_id) -> float:
                cap = _by_id.get(reg.get("caption_block_id") or "") or {}
                if not cap:
                    return 1e9
                y0, y1 = _num(cap.get("y0")), _num(cap.get("y1"))
                # ⚠️ 必须用**解析器给的图体 raw bbox**，不能用收紧后的 `bbox`：
                # 假那条的 bbox 恰好是它自己那句"图注"**上方**的正文（到那句话只有 1.2pt），
                # 用它反而会赢下比较。raw 是图体（49~208），真图注离它 6.9pt、假的离它 162pt。
                box = reg.get("raw_bbox") or reg["bbox"]
                b0, b1 = _num(box[1]), _num(box[3])
                return min(abs(y0 - b1), abs(b0 - y1))

            items = sorted(items, key=lambda r: (_cap_gap(r), _num(r["bbox"][1])))
            keep.append(items[0])
            log.info("同一%s号 %s 有 %d 条区域，只留离图体最近的那条（其余当成正文）",
                     "图" if kind == "figure" else "表", number, len(items))
        keep.sort(key=lambda r: (_num(r["bbox"][1]), _num(r["bbox"][0])))
        out[page_no] = keep

    # ---- 同一图号出现在**不同页**时：正文里的引用句那条要丢掉（2026-09-27）----
    # 实测 efficient-low-rank-multimodal-fusion（用户："只有 4 张图，却截取了 6 张"）：
    #   p4 有一条正文引用句 `Figure 3. …`（那个块里根本没有图）被判成图注 → 多出一条区域；
    #   p5 才是真正的 Figure 3。页内去重管不到（两条不在同一页），于是库里有 6 条图。
    # 判据与页内同源（图注块到图体 raw bbox 的距离），但**只在差距悬殊时才丢**
    # （> 4 倍且 > 24pt）：正文引用那条往往连图注块都没有（1e9），真图注是个位数 pt。
    # 附录里重新编号的 `Figure 1`（两条都有真图注、都贴着图体）不会被误伤。
    #
    # ⚠️ 一条区域可能被 `_parts_of` 切成好几段（part 0/1）：这些段共享同一个 `raw_bbox`，
    # 于是**同进同出**（实测 LMF p4 的假图被切成两段，漏掉其中一段就会又数出 6 张图）。
    cross: dict[str, list[dict]] = {}
    for regs in out.values():
        for reg in regs:
            if reg.get("kind") != "figure" or not reg.get("number"):
                continue
            if reg.get("continues_from"):      # 跨页续图：与上一页本来就是同一张图，不能互删
                continue
            cross.setdefault(str(reg["number"]), []).append(reg)
    drop_ids: set[int] = set()
    for number, items in cross.items():
        if len({int(r["page"]) for r in items}) < 2:
            continue
        gaps: list[tuple[dict, float]] = []
        for reg in items:
            box = reg.get("raw_bbox") or reg["bbox"]
            by_id = {b["id"]: b for b in blocks_by_page.get(int(reg["page"]), [])}
            cap = by_id.get(reg.get("caption_block_id") or "") or {}
            if not cap:
                gaps.append((reg, 1e9))
            else:
                gaps.append((reg, min(abs(_num(cap.get("y0")) - _num(box[3])),
                                      abs(_num(box[1]) - _num(cap.get("y1"))))))
        best = min(g for _r, g in gaps)
        if best > 24.0:
            continue                     # 没有一条紧贴图体 → 不敢乱丢（宁可多留）
        for reg, gap in gaps:
            if gap > best * 4 and gap > 24.0:
                drop_ids.add(id(reg))
                log.info("图号 %s 在 p%s 多出一条不贴图体的区域（图注距图体 %.0fpt），删掉",
                         number, reg.get("page"), gap)
    if drop_ids:
        for page_no in list(out):
            out[page_no] = [r for r in out[page_no] if id(r) not in drop_ids]

    return out


def all_regions(paper_id: str, *, force: bool = False) -> dict[int, list[dict]]:
    """全篇插图区域（按 IR 文件 mtime 缓存，避免每个请求反复读库）。"""
    from backend.services.paper import paper_service

    ir_path: Path = paper_service.paper_paths(paper_id)["ir"]
    if not ir_path.is_file():
        return {}
    mtime = ir_path.stat().st_mtime
    with _lock:
        cached = _cache.get(paper_id)
        if cached and not force and abs(cached[0] - mtime) < 1e-6:
            return cached[1]
    data = _compute_all(paper_id)
    prune_crops(paper_id, data)
    with _lock:
        _cache[paper_id] = (mtime, data)
    return data


def prune_crops(paper_id: str, regions: dict[int, list[dict]] | None = None) -> int:
    """删掉不再被任何区域引用的裁图缓存（重解析/换算法后旧文件不会一直堆在磁盘上）。

    文件名自带 bbox 哈希，所以每次算法调整都会产生一批新文件；这里按"当前该有的文件名
    集合"反选。表格裁图与公式裁图不归本模块管，只清理 ``region-p*.png``。
    """
    from backend.services.paper import paper_service

    try:
        figures_dir: Path = paper_service.paper_paths(paper_id)["figures"]
    except Exception:                                   # noqa: BLE001
        return 0
    if not figures_dir.is_dir():
        return 0
    data = regions if regions is not None else all_regions(paper_id)
    keep = {f"region-p{int(page):04d}-{i:02d}-p{int(r.get('part', 0))}-{r['hash']}.png"
            for page, rows in data.items() for i, r in enumerate(rows)}
    if caption_in_crop(paper_id):
        keep |= {name[:-4] + "-card.png" for name in keep}     # 图表卡片缓存一起保留
    # 关掉「图片是否包含图注」的论文不再用卡片：这类缓存一并清掉，别让磁盘上留着
    # 一张"和当前口径不一致"的图（下次开回来会按需重裁）。
    removed = 0
    for path in figures_dir.glob("region-p*.png"):
        if path.name in keep:
            continue
        try:
            path.unlink()
            removed += 1
        except OSError as exc:                          # noqa: PERF203
            log.warning("旧裁图删除失败：%s（%s）", path.name, exc)
    if removed:
        log.info("清理旧插图裁图 %d 个：%s", removed, paper_id)
    return removed


def page_regions(paper_id: str, page_no: int) -> list[dict]:
    """该页插图区域（按文档顺序；表格也返回，kind=table）。"""
    return all_regions(paper_id).get(int(page_no), [])


def invalidate_cache(paper_id: str | None = None) -> None:
    with _lock:
        if paper_id is None:
            _cache.clear()
            _opts_cache.clear()
        else:
            _cache.pop(paper_id, None)
            _opts_cache.pop(paper_id, None)


def region_crop_path(paper_id: str, page_no: int, index: int, part: int = 0,
                     *, card: bool = False) -> Path:
    """缓存文件路径：``figures/region-p0008-00-p1-ab12cd34[-card].png``（含 bbox 哈希）。

    ``-card`` 后缀 = 图 + 图注合成的"图表卡片"（与纯图裁图分开缓存）。
    """
    from backend.services.paper import paper_service

    regions = page_regions(paper_id, page_no)
    if index < 0 or index >= len(regions):
        raise NotFoundError(f"插图区域不存在：p{page_no} #{index}")
    figures_dir: Path = paper_service.paper_paths(paper_id)["figures"]
    suffix = "-card" if card else ""
    return figures_dir / (f"region-p{int(page_no):04d}-{index:02d}-p{int(part)}"
                          f"-{regions[index]['hash']}{suffix}.png")


def _resolve_part(paper_id: str, page_no: int, index: int, part: int | None) -> tuple[dict, int]:
    """定位到具体那一段：``part=None`` 取 entry 自己声明的 part（默认 0）。"""
    regions = page_regions(paper_id, page_no)
    if index < 0 or index >= len(regions):
        raise NotFoundError(f"插图区域不存在：p{page_no} #{index}")
    region = regions[index]
    want = int(region.get("part", 0) if part is None else part)
    if want == int(region.get("part", 0)):
        return region, want
    # 兼容旧链接：同一张图被切成多段时，允许只给 index（取第一段）
    return region, int(region.get("part", 0))


def caption_rects(paper_id: str, page_no: int, region: dict) -> list[Rect]:
    """该插图对应的图注矩形（可能跨多块，比如通栏长图注被拆成两段）。

    用于"图 + 图注"卡片（放大/导出）。**扫描件不拼图注**，原因有二（都是实测的）：

    1. 扫描件的图注位置只能靠视觉模型给的粗框（差十几个点、宽度也偏窄），按它裁出来的
       图注条会**把两侧的字母切掉**；
    2. 图注常常紧贴图体（p8 只隔 1.6pt），图体窗口本身就可能已经含了图注第一行 ——
       再把整段图注拼到下面，同一句话就会出现两次，"像拼接的一样"。

    图注在阅读器里有自己的一行、在导出 Markdown 里紧跟图片下方、放大弹窗的标题也是它，
    所以这里不拼不会丢信息。文本层论文的图注框是排版软件给的精确坐标，照旧拼。
    """
    from backend.parsing.ir.ir_reader import IRReader
    from backend.services.paper import paper_service

    if str(region.get("source") or "") == "scan":
        return []
    ir = IRReader(paper_service.paper_paths(paper_id)["ir"])
    if not ir.exists:
        return []
    number = str(region.get("number") or "")
    if not number:
        return []

    def matches(text: str) -> bool:
        head = re.match(r"^\s*(?:\*\*)?\s*(?:Figure|Fig\.?|Table|图|表)\s*(\d{1,3})",
                        text or "", re.IGNORECASE)
        return bool(head) and head.group(1) == number

    cand: list[tuple[str, Rect, str]] = []
    for b in ir.blocks():
        if int(b["page_no"]) != int(page_no) or (b.get("type") or "") != "caption":
            continue
        text = str(b.get("text_clean") or b.get("text") or "")
        if not matches(text):
            continue
        rect = _rect_of(b)
        if rect.width > 2 and rect.height > 2:
            cand.append((str(b.get("id") or ""), rect, text))
    if not cand:
        return []
    cand.sort(key=lambda it: (it[1].y0, it[1].x0))
    # ⚠️ 只取**挨着的那一串**图注块。解析器会把正文里的引用句也判成 caption
    # （实测 DTDBD p11 的 `Figure 3. From Case 1, it can be seen that when traditional`，
    # 离真图注 145pt），一起拼到卡片下面就是用户看到的"底部图注对不上 / 像拼接"。
    want_id = str(region.get("caption_block_id") or "")
    start = next((i for i, (bid, _r, _t) in enumerate(cand) if bid == want_id), None)
    if start is None:                      # 没有明确的图注块 → 取离图体最近的那块当起点
        b0, b1 = _num(region["bbox"][1]), _num(region["bbox"][3])
        start = min(range(len(cand)),
                    key=lambda i: min(abs(cand[i][1].y0 - b1), abs(b0 - cand[i][1].y1)))
    chain = [cand[start][1]]
    chain_text = [cand[start][2]]
    if not _ends_sentence(chain_text[0]):  # 图注已经收尾就不用再续了（见下面 _ends_sentence 的说明）
        for step in (1, -1):               # 向下、向上各延伸，遇到明显空档/句末就停
            i = start + step
            while 0 <= i < len(cand):
                prev = chain[-1] if step > 0 else chain[0]
                rect = cand[i][1]
                gap = (rect.y0 - prev.y1) if step > 0 else (prev.y0 - rect.y1)
                if gap > CAPTION_CHAIN_GAP:
                    break
                txt = cand[i][2]
                if step > 0:
                    chain.append(rect)
                    chain_text.append(txt)
                else:
                    chain.insert(0, rect)
                    chain_text.insert(0, txt)
                if _ends_sentence(txt):
                    break
                i += step
    # 再往下续接**紧挨着**的普通块：图注往往是"多行的一大段"，而解析器只把第一行判成 caption
    # （实测 DTDBD p11 第二行 `the probabilistic prediction results …` 是 para）—— 只取 caption
    # 块会把图注截掉一半。间隔 ≤ CAPTION_CHAIN_GAP、同列、且不是另一张图/表的题注才续。
    #
    # ⚠️ **句末即停**：图注的续行不会以句末标点结尾。不加这条判据会一路吞下去 ——
    # 实测 DAMMFND p1：图 1 的图注（以 `…within this dataset.` 结尾）后面紧跟着整段摘要，
    # 于是一张 t-SNE 图下面挂了 10 块正文（用户看到的"拼接图片"）。
    cap_head = re.compile(r"^\s*(?:\*\*)?\s*(?:Figure|Fig\.?|Table|图|表)\s*\d", re.IGNORECASE)
    blocks_sorted = sorted(
        [b for b in ir.blocks() if int(b["page_no"]) == int(page_no)],
        key=lambda b: (_num(b.get("y0")), _num(b.get("x0"))))
    if _ends_sentence(chain_text[-1]):
        return sorted(chain, key=lambda r: (r.y0, r.x0))
    col = cand[start][1]                   # 图注自己那一栏：续行**必须落在这一栏内**
    last = max(chain, key=lambda r: r.y0)
    for b in blocks_sorted:
        rect = _rect_of(b)
        if rect.y0 <= last.y1 + 0.5:
            continue
        if (b.get("type") or "") in ("figure", "formula"):
            break
        # ⚠️ 判据是"**和本栏一样宽**"，不是"有重叠"：实测 DAMMFND p1 的正文被解析器并成了
        # **通栏块**（x 64~558，宽 494pt），而图注那一栏只有 238pt —— 只判重叠会顺着通栏块
        # 一路吞下整段摘要（图 1 的图注挂了 10 块正文 → 用户看到的"拼接图片"）。
        # 图注的续行必然不比本栏宽多少；比本栏宽 20pt 以上就不是它。
        if rect.width > col.width + 20.0:
            break
        if rect.x0 < col.x0 - 6.0 or rect.x1 > col.x1 + 6.0:
            break                          # 越出本栏（换栏）→ 图注到此为止
        if rect.y0 - last.y1 > CAPTION_CHAIN_GAP:
            break
        text = str(b.get("text_clean") or b.get("text") or "")
        if cap_head.match(text):
            break                          # 另一张图/表的题注
        chain.append(rect)
        last = rect
        if _ends_sentence(text):
            break                          # 这句收尾了 → 图注到此为止
    # ⚠️ 把图注条裁回**本栏**：有的 PDF（转换器产出）会把左右两栏同一基线的文字并成一个块
    # （实测 LMF p8 的图注块 x 72~527：左边是图注、右边是参考文献）。裁切用的带子 = 图注锚点
    # （解析层写在 block.meta["caption_anchor"]）∪ 图体自身横向范围 —— 两者都够不着的地方
    # 一定是另一栏的内容。普通论文里这个带子就是整栏，等于不裁（图注比图宽也不会被切）。
    x0b, x1b = _num(region["bbox"][0]), _num(region["bbox"][2])
    cap_row = next((b for b in ir.blocks()
                    if str(b.get("id") or "") == str(region.get("caption_block_id") or "")), None)
    if cap_row:
        a = _meta(cap_row).get("caption_anchor")
        if isinstance(a, (list, tuple)) and len(a) == 4:
            x0b, x1b = min(x0b, _num(a[0])), max(x1b, _num(a[2]))
    clipped = [Rect(max(r.x0, x0b), r.y0, min(r.x1, x1b), r.y1)
               for r in chain if min(r.x1, x1b) - max(r.x0, x0b) > 1.0]
    return sorted(clipped or chain, key=lambda r: (r.y0, r.x0))


def ensure_region_crop(paper_id: str, page_no: int, index: int,
                       part: int | None = None, *, card: bool = False) -> Path:
    """按需渲染并缓存该区域的裁图（只渲染 clip 区域，600dpi，先墨迹裁白）。

    注意这里是 ``padding=0``：余量由"墨迹裁白"在**量出真实墨迹之后**再加，
    不能先用 padding 把裁剪框撑大 —— 撑出去的正是旁边图注/图题的半行字
    （用户报的"图注被切掉一条"就是这么来的）。

    ``card=True`` 出的是**图表卡片**（图 + 图注合成一张，见 ``figure_card_png``），
    用于放大看大图与导出；正文里的插图仍是纯图（图注在正文里有自己的一行）。

    ⚠️ 导入时把「图片是否包含图注」关掉（**默认**）的论文，``card=True`` 会退化成**纯图**：
    关掉的含义就是"裁出的插图不含图注"，而正文与"点开看大图"必须是**同一张**
    （这条以前专门统一过，见 ``media_of_page`` / `ReaderPageRows` 的返工记录）。
    """
    from backend.core.utils import write_bytes_atomic
    from backend.parsing.extract.render import figure_card_png, render_crop_png
    from backend.services.paper import paper_service

    region, part_no = _resolve_part(paper_id, page_no, index, part)
    want_card = card and caption_in_crop(paper_id)
    target = region_crop_path(paper_id, page_no, index, part_no, card=want_card)
    if target.is_file() and target.stat().st_size > 0:
        return target
    x0, y0, x1, y1 = region["bbox"]
    caps = caption_rects(paper_id, page_no, region) if want_card else []
    pdf = paper_service.resolve_pdf(paper_id)
    with _lock:
        if target.is_file() and target.stat().st_size > 0:
            return target
        import pymupdf as fitz

        with fitz.open(str(pdf)) as doc:
            if want_card:
                data = figure_card_png(doc, int(page_no), Rect(x0, y0, x1, y1), caps,
                                       dpi=CROP_DPI)
            else:
                data = render_crop_png(doc, int(page_no), Rect(x0, y0, x1, y1),
                                       dpi=CROP_DPI, padding=INK_PAD, ink_trim=True)
        write_bytes_atomic(target, data)
    log.info("插图裁图已生成：p%d #%d part%d%s（%s %s）%d bytes",
             page_no, index, part_no, "·卡片" if want_card else "",
             region["kind"], region["number"], target.stat().st_size)
    return target


#: 图注行首（可带 `**` 加粗）：`Figure 1:` / `Table 1:` / `图 1` / `表 1`
_MD_CAPTION_HEAD = re.compile(
    r"^\s*(?:\*\*)?\s*(?:Figure|Fig\.?|Table|图|表)\s*(\d{1,3})\b", re.IGNORECASE)


def _plain_caption(text: str) -> str:
    """去掉图注行两侧的 Markdown 加粗记号（`**Figure 1: …**` → `Figure 1: …`）。"""
    return re.sub(r"^\s*\*\*|\*\*\s*$", "", str(text or "")).strip()


#: 算法图注行首：`**Algorithm 1 TURBOQUANT$_{\\text{mse}}$: optimized for MSE**`
_MD_ALGO_HEAD = re.compile(r"^\s*(?:\*\*)?\s*Algorithm\s*(\d{1,3})\b", re.IGNORECASE)


def markdown_caption(paper_id: str, page_no: int, number: str, *,
                     kind: str = "figure") -> str:
    """这一页「图号 = number」在**译文那一面**的完整图注（含 `$…$`）。

    为什么需要（实测 2026-12-07）：IR 里 caption 块的 `preview` 在**第一个 `$` 处就断了**
    （`Figure 1: … for TURBOQUANT and TURBOQUANT`，后面 `$_{\\text{prod}}$` 整段丢了），
    于是含公式的图注一律被截半句；不含 `$` 的（p8 图 2/3、p9 表 1）反而完整 —— 症状很整齐。
    转录产物那一行是完整的，读时补回来即可（不改 IR：旧论文重解析才会有新产物）。
    """
    want = str(number or "").strip()
    if not want:
        return ""
    # 算法块与图/表共用序号（都有 “1”），所以图注行首的正则要跟着 kind 走
    head_re = _MD_ALGO_HEAD if kind == "algorithm" else _MD_CAPTION_HEAD
    from backend.services.paper import reader_service

    try:
        rows = reader_service._rows_of_page(paper_id, int(page_no)) or []
    except Exception as exc:  # noqa: BLE001 —— 没有页产物 / 路径不全时**不许**拖垮插图列表
        log.debug("取第 %s 页行失败（图注只用 IR 那份）：%s", page_no, exc)
        return ""
    for row in rows:
        text = _plain_caption(row.get("original") or "")
        if not text:
            continue
        hit = head_re.match(text)
        if not hit:
            continue
        if hit.group(1) == want:
            return text
    return ""

def media_of_page(paper_id: str, page_no: int, *, figures_only: bool = True) -> list[dict]:
    """阅读器/侧栏用的插图条目（url 指向按需裁图接口）。

    被切成多段（同页夹着别的图注 / 跨页续图）时，每段是一条条目：
    ``part`` 是段号、``parts`` 是总段数、``part_label`` 给前端显示"续"。
    """
    out: list[dict] = []
    for i, r in enumerate(page_regions(paper_id, page_no)):
        if figures_only and r["kind"] != "figure":
            continue
        part = int(r.get("part", 0))
        base = (f"/api/v1/papers/{paper_id}/figures/region/{int(page_no)}/{i}"
                + (f"/{part}" if part else ""))
        # ⚠️ `v=<区域哈希>` 不能省：裁图接口的响应头是 `Cache-Control: public, max-age=604800`，
        # 而 URL 只由"页 + 序号"决定 —— 区域算法/边界一变，**浏览器还会拿缓存里那张旧图**
        # 摆 7 天（实测事故：修好"图 3 顶部被裁"之后用户重启服务、重新导入都还是旧图，
        # 后端抓下来明明是新的）。带上哈希，URL 随区域变，缓存自然失效；
        # 缓存文件本身的名字里也带哈希，两处口径一致。
        # 版本串里还有**图注开关**（`_ver`）：开关不改 bbox，光靠哈希挡不住旧卡片。
        ver = region_version(r, paper_id)
        q = f"v={ver}" if ver else ""
        # 图注：IR 的 preview 会截在第一个 `$` 处，转录那一行才是完整的 —— 补回来（不改产物）。
        caption = r["caption"]
        full = markdown_caption(paper_id, int(page_no), str(r["number"] or ""),
                                kind=str(r["kind"] or "figure"))
        if full and len(full) > len(caption) and full[:40].lower() == caption[:40].lower():
            caption = full
        out.append({
            "kind": r["kind"], "number": r["number"], "caption": caption,
            "bbox": r["bbox"],
            "part": part, "parts": int(r.get("parts", 1)),
            "part_label": r.get("part_label") or "",
            "url": f"{base}?{q}" if q else base,
            "card_url": f"{base}?card=1&{q}" if q else f"{base}?card=1",
            "status": "ok",
        })
    return out


# --------------------------------------------------------------------------
# Markdown 图片引用：让插图**出现在 Markdown 本体里**
# --------------------------------------------------------------------------

# 图注行：整行就是图注（可带 ** 加粗）
_MD_CAPTION = re.compile(
    r"^\s*(?:\*\*)?\s*(?:Figure|Fig\.?|Table|图|表)\s*(\d{1,3})\b", re.IGNORECASE)


def markdown_refs(paper_id: str, page_no: int, *, prefix: str) -> dict[str, str]:
    """该页每个图号对应的 Markdown 图片行，并**确保裁图已落盘**。

    ``prefix`` 决定引用形式：
    - 以 ``/`` 开头（如 ``/api/v1``）→ ``/api/v1/papers/<id>/figures/region/<page>/<i>``，
      应用内渲染用（浏览器能取到）；
    - 其它（如 ``../figures``）→ 相对路径，落盘 Markdown 用（在 md/ 目录下打开可用）。
    """
    refs: dict[str, str] = {}
    for i, r in enumerate(page_regions(paper_id, page_no)):
        if r["kind"] != "figure" or not r["number"]:
            continue
        try:
            path = ensure_region_crop(paper_id, page_no, i)
        except Exception as exc:  # noqa: BLE001
            log.warning("裁图渲染失败，Markdown 里跳过该图：p%d #%d %s", page_no, i, exc)
            continue
        if prefix.startswith("/"):
            # 同样要带 `v=<哈希>`：这条 URL 会被写进应用内的 Markdown 视图
            # （`/markdown/translated?write=false` 那份），浏览器同样会缓存 7 天。
            ver = region_version(r, paper_id)
            base = (f"{prefix.rstrip('/')}/papers/{paper_id}/figures/region/{int(page_no)}/{i}"
                    + (f"?v={ver}" if ver else ""))
        else:
            base = f"{prefix.rstrip('/')}/{path.name}"
        # 兜底写图号：没有图注的插图很常见，别让这一行把整页 Markdown 导出打崩。
        # `number` 本身已经是 "Figure 2" / "Table 1" 形态，别再补一个 "Figure " 前缀。
        label = r["caption"][:80] or r["number"]
        refs[r["number"]] = f"![{label}]({base})"
    return refs


def inject_images(md: str, refs: dict[str, str]) -> str:
    """把图片行插到**对应图号的图注行上方**（论文排法：图在上、图注在下）。

    已经带 ``![`` 的行不再重复插入；匹配不到的图放到末尾，绝不丢图。
    """
    text = (md or "").strip()
    if not text or not refs:
        return text
    if "![" in text:                    # 模型自己给了图片引用就尊重它
        return text
    blocks = re.split(r"\n\s*\n", text)
    used: set[str] = set()
    out: list[str] = []
    for block in blocks:
        m = _MD_CAPTION.match(block.strip())
        if m and m.group(1) in refs and m.group(1) not in used:
            used.add(m.group(1))
            out.append(refs[m.group(1)])      # 图在上
        out.append(block)                     # 图注在下
    for number, ref in refs.items():
        if number not in used:
            out.append(ref)
    return "\n\n".join(out).strip()
