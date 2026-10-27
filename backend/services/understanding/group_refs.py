"""解读正文的**引用标号分组**：一处连续材料只写**一个**标号。

为什么要有这个模块（用户 2026-12 口径，与对话侧 backend/agent/cite_groups.py 同一套规则）：
解读原先的提示词写的是"一句话依据多条材料就**并排写**：[A023][A031]"—— 于是一句话后面
跟着一串方括号。前端把每个方括号各算一枚按钮、各占一个号，读者看到的是一排编号，
点一枚只闪一处；而其中相当一部分（实测两篇：36 / 44 对相邻码里 24 / 23 对同页）
本来就是**同一处材料被切成几块**，本该一枚。

用户定稿的两种形态（**与前端是一份契约，改一边另一端就碎**）：

- [A012]：单块材料，一枚按钮；
- [A012,A013]：一处由几块连续材料组成，**一个方括号 + 半角逗号、无空格**，一枚按钮，
  闪组内全部行。

[A012][A013] 是**两个标号**，不是一处 —— 这是本模块要归一掉的唯一形态。

一个组 = 下面任一条成立的几块（契约第 2 条）：

1. 同一页上**行号相邻**（相邻两块的接缝：|前一块末行 - 后一块首行| <= 1）；
2. pids 有交集（同一块被切成跨页两半）。

两条都不满足 —— 尤其**认不出几何信息**时 —— **不许并进同一个方括号**，原样留着各写各的
（宁可多一枚按钮，也不许把两处不相邻的材料假装成一处）。

refs_json 结构不变：组不是新 code，codes 里照样列出组内每个短码；前端按 code 查位置、
把组内所有 code 的 row_ids / pids 取并集来闪。

本模块是**纯函数**（不碰 DB / 网络 / 文件），几何信息由调用方用 locate 注入，所以能单测。
"""

from __future__ import annotations

import re
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass
from typing import Any

#: 短码形状：一个字母前缀 + 2~4 位数字（A012；老数据里还有 B 路的 B023）
CODE = r"[A-Za-z]\d{2,4}"
#: 一个方括号 = 单码，或半角逗号并列的一组（输入宽容空格与全角逗号，输出一律半角无空格）。
#: 不许跨行、长度有界 —— **认不出的方括号完全不匹配**，原样留在正文里（论文自己的
#: [12, Eq.3] / 页码 / 年份，吞掉一个字都是坏正文）。
_MARK_RE = re.compile(r"\[(" + CODE + r"(?:\s*[,，]\s*" + CODE + r")*)\]")
#: 行内代码（`…`）里的标号是**示例**，不是引用，跳过
_INLINE_RE = re.compile(r"`[^`\n]*`")
#: row_id（p005-02 = 页 + 页内行号）；r-xxxx 那种 pid 不含几何信息，不参与解析
_ROW_ID_RE = re.compile(r"[pP]\s*(\d{1,4})\s*[-_]\s*(\d{1,4})")
#: 围栏代码块的开启行（三反引号 / 三波浪号）
_FENCES = ("```", "~~~")


@dataclass(frozen=True)
class CodeMark:
    """正文里的一个**短码方括号**：位置 + 它并列了哪几个短码（保持书写顺序）。"""

    start: int
    end: int
    raw: str
    codes: tuple[str, ...]


@dataclass(frozen=True)
class Geo:
    """一条材料的几何身份（调用方注入，取不到的键一律 None / 空）。

    page / row 是**首行**；end_page / row_last 是**末行**（可选，缺了就退回首行）。
    判"相邻的几块"看的是**接缝**：前一块的末行 ↔ 后一块的首行。
    """

    page: int | None = None
    row: int | None = None
    end_page: int | None = None
    row_last: int | None = None
    pids: tuple[str, ...] = ()


# --------------------------------------------------------------------------
# 扫描：哪些方括号是标号，哪些只是正文
# --------------------------------------------------------------------------


def _blank(text: str, spans: Iterable[tuple[int, int]]) -> str:
    """把若干区间涂成空格（**等长**，所以扫描结果的下标就是原文的下标）。"""
    out = list(text)
    for start, end in spans:
        for i in range(max(0, int(start)), min(len(out), int(end))):
            if out[i] != "\n":
                out[i] = " "
    return "".join(out)


def _code_spans(md: str) -> list[tuple[int, int]]:
    """代码区间：围栏块（三反引号 / 三波浪号，到闭合围栏或文末）与行内代码。"""
    spans: list[tuple[int, int]] = []
    opened: int | None = None
    pos = 0
    for line in md.splitlines(keepends=True):
        head = line.lstrip()
        is_fence = any(head.startswith(mark) for mark in _FENCES)
        if opened is None:
            if is_fence:
                opened = pos
        elif is_fence:
            spans.append((opened, pos + len(line)))
            opened = None
        pos += len(line)
    if opened is not None:
        spans.append((opened, len(md)))
    # 行内代码在**涂掉围栏之后**再找：围栏里的反引号已经不在了，不会被算成行内代码
    blanked = _blank(md, spans)
    spans.extend(m.span() for m in _INLINE_RE.finditer(blanked))
    return spans


def parse_code_brackets(md: str) -> list[CodeMark]:
    """扫出正文里**认得出**的标号方括号：[A012] 与 [A012,A013]。

    代码块（围栏代码块 / 反引号行内代码）里的一律跳过 —— 那里出现的是**示例**，
    归一化会把示例改成另一副样子。认不出的方括号不返回（原样留在正文里）。
    """
    text = md or ""
    masked = _blank(text, _code_spans(text))
    out: list[CodeMark] = []
    for match in _MARK_RE.finditer(masked):
        codes = tuple(part.strip() for part in re.split(r"[,，]", match.group(1))
                      if part.strip())
        if codes:
            out.append(CodeMark(start=match.start(), end=match.end(),
                                raw=match.group(0), codes=codes))
    return out


def split_runs(md: str, marks: Sequence[CodeMark] | None = None) -> list[list[CodeMark]]:
    """把标号切成**极大连续段**：段内两个方括号之间**只有空白**。

    为什么按 run：[A012][A013] / [A012] [A013] 是"一处材料被写成两枚"的形态；
    中间隔着标点或正文（[A012]，[A013] / [A012] 正文 [A013]）本来就是两处引用。
    """
    text = md or ""
    items = list(parse_code_brackets(text) if marks is None else marks)
    runs: list[list[CodeMark]] = []
    cur: list[CodeMark] = []
    for mark in items:
        if cur and text[cur[-1].end:mark.start].strip():
            runs.append(cur)
            cur = []
        cur.append(mark)
    if cur:
        runs.append(cur)
    return runs


# --------------------------------------------------------------------------
# 几何：两块材料是不是"同一处的上下两块"
# --------------------------------------------------------------------------


def _field(raw: Any, key: str) -> Any:
    """字典 / 对象都能读（调用方注进来的 locate 两种都可能）。"""
    if isinstance(raw, Mapping):
        return raw.get(key)
    return getattr(raw, key, None)


def _int(value: Any) -> int | None:
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def row_pair(row_id: str) -> tuple[int, int] | None:
    """p005-02 → (页 5, 页内行号 2)；认不出就是 None。"""
    match = _ROW_ID_RE.search(str(row_id or ""))
    if not match:
        return None
    return int(match.group(1)), int(match.group(2))


def _geometry(locate: Callable[[str], Any] | None, code: str) -> Geo | None:
    """一条短码的几何身份；**认不出就是 None**（调用方据此拒绝合并，绝不猜）。

    注进来的 locate 收到的是**大写**短码；它自己抛错也算认不出 —— 定位坏了不该让解读失败。
    """
    if locate is None:
        return None
    try:
        raw = locate(str(code or "").upper())
    except Exception:  # noqa: BLE001 —— 定位函数是外部注入的，坏了只退化成"不合并"
        return None
    if raw is None:
        return None
    pids = tuple(str(pid) for pid in (_field(raw, "pids") or ()) if str(pid).strip())
    geo = Geo(page=_int(_field(raw, "page")), row=_int(_field(raw, "row")),
              end_page=_int(_field(raw, "end_page")), row_last=_int(_field(raw, "row_last")),
              pids=pids)
    if geo.page is None and geo.row is None and not geo.pids:
        return None
    return geo


def _adjacent(left: Geo, right: Geo) -> bool:
    """right 是不是紧接在 left **后面**的那一块（判不出就 False）。"""
    page_l = left.end_page if left.end_page is not None else left.page
    row_l = left.row_last if left.row_last is not None else left.row
    if (page_l is not None and right.page is not None
            and row_l is not None and right.row is not None
            and page_l == right.page and abs(row_l - right.row) <= 1):
        return True
    # 同一块被切成跨页两半：几何上够不着，但行身份（pids）一样
    return bool(left.pids and right.pids and (set(left.pids) & set(right.pids)))


# --------------------------------------------------------------------------
# 归一
# --------------------------------------------------------------------------


def _stats(**kw: int) -> dict:
    base = {"groups": 0, "merged_codes": 0, "absorbed": 0,
            "marks_before": 0, "marks_after": 0, "skipped_runs": 0}
    base.update(kw)
    return base


def group_code_runs(md: str, locate: Callable[[str], Any] | None) -> tuple[str, dict]:
    """把**并排的方括号**归一成一个「连续组」标号：[A012][A013] → [A012,A013]。

    每个 run（只隔空白的极大方括号段）里：

    1. 组内**有一个短码认不出**就整段不合并（保持原样，前端逐 code 一枚按钮 —— 绝不吞号）；
    2. 按契约第 2 条切成**极大连续子序列**，每段一个标号；
    3. 段内短码**保持文档顺序**（不排序 —— 排序会打乱读者顺着原文看下去的上下文）。

    返回 (新正文, stats)；stats 只给日志与单测看：groups 合并出的组数、
    merged_codes 组内短码总数、absorbed 被吞掉的方括号数、marks_before / marks_after
    前后标号数、skipped_runs 因认不出几何信息而原样留着的 run 数。
    """
    text = md or ""
    marks = parse_code_brackets(text)
    stats = _stats(marks_before=len(marks), marks_after=len(marks))
    if not marks:
        return text, stats

    edits: list[tuple[int, int, str]] = []
    for run in split_runs(text, marks):
        geos: dict[str, Geo] | None = {}
        for mark in run:
            for code in mark.codes:
                geo = _geometry(locate, code)
                if geo is None:
                    geos = None
                    break
                geos[code.upper()] = geo
            if geos is None:
                break
        if geos is None:
            stats["skipped_runs"] += 1
            continue

        segments: list[list[CodeMark]] = []
        for mark in run:
            if not segments:
                segments.append([mark])
                continue
            prev = segments[-1][-1]
            if _adjacent(geos[prev.codes[-1].upper()], geos[mark.codes[0].upper()]):
                segments[-1].append(mark)
            else:
                segments.append([mark])

        for segment in segments:
            codes: list[str] = []
            seen: set[str] = set()
            for mark in segment:
                for code in mark.codes:
                    key = code.upper()
                    if key in seen:
                        continue
                    seen.add(key)
                    codes.append(code)
            if not codes:
                continue
            start, end = segment[0].start, segment[-1].end
            grouped = "[" + ",".join(codes) + "]"
            if grouped == text[start:end]:
                continue                       # 已经是这个形态（幂等的落点）
            edits.append((start, end, grouped))
            if len(codes) > 1:
                stats["groups"] += 1
                stats["merged_codes"] += len(codes)
            stats["absorbed"] += len(segment) - 1

    if not edits:
        return text, stats
    out = text
    for start, end, grouped in reversed(edits):
        out = out[:start] + grouped + out[end:]
    stats["marks_after"] = len(parse_code_brackets(out))
    return out, stats


def _locator_from_inventory(inventory: Sequence[Mapping] | None) -> Callable[[str], dict | None]:
    """材料清单 → locate(code)：{page, row, end_page, row_last, pids}。

    行号从清单条目的 row_ids（p005-02，与 pids 同序等长，见知识库建索引）解析首行与末行；
    条目没带行号（老数据的存量清单）就只剩 page / pids，那一侧的相邻判断自然退化成
    "判不出 → 不合并"。
    """
    geo: dict[str, dict] = {}
    for item in inventory or ():
        if not isinstance(item, Mapping):
            continue
        code = str(item.get("code") or "").strip().upper()
        if not code:
            continue
        page = _int(item.get("page"))
        rows = [pair for pair in (row_pair(r) for r in (item.get("row_ids") or ())) if pair]
        pids = [str(pid) for pid in (item.get("pids") or ()) if str(pid).strip()]
        geo.setdefault(code, {
            "page": rows[0][0] if rows else page,
            "row": rows[0][1] if rows else None,
            "end_page": rows[-1][0] if rows else page,
            "row_last": rows[-1][1] if rows else None,
            "pids": pids,
        })

    def locate(code: str) -> dict | None:
        return geo.get(str(code or "").strip().upper())

    return locate


def apply_ref_groups(md: str, inventory: Sequence[Mapping] | None,
                     locate: Callable[[str], Any] | None = None) -> tuple[str, dict]:
    """解读正文落库前的兜底归一：[A012][A013] → [A012,A013]。

    inventory 是这篇论文的材料清单（code / page / row_ids / pids）；默认就用它建 locate。
    locate 显式传进来时以它为准（单测与将来的读路径都用得着）。
    拿不到几何信息时**一律不合并**：宁可多一枚按钮，也不许把两处材料假装成一处。
    """
    locator = locate if locate is not None else _locator_from_inventory(inventory)
    return group_code_runs(md, locator)
