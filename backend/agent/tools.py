"""P0 的六个**只读**工具（论文问答的全部手脚）。

设计口径（`docs/agent-接入规划.md` §7）：

1. **工具返回"候选位置"，不返回全文** —— 检索给页码 + 短片段，让模型自己决定读哪页。
   把全文塞进检索结果等于把"读哪页"的决定权收走，还会瞬间吃光上下文。
2. **返回值必须瘦**：不发 `retrieval` 元数据（`vector_hits/fused/rrf_k/rerank`）、不发
   `bbox`、不发内部 id。工具返回里的 `ref` 是**宿主分配的引用编号**，模型只写 `[ref]`。
3. **`paper_id` 由宿主注入**（deps），工具签名里没有这个参数 —— 模型没有机会传错论文。
4. **失败不是异常，是结果**：主后端报错时返回 ``{"ok": false, "error": ...}``，
   让模型知道"这条路走不通"并换一条，而不是让整个请求 500。
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import re
import time

from pydantic_ai import RunContext
from pydantic_ai.messages import BinaryContent, ToolReturn

from backend.agent.citations import Citation
from backend.agent.deps import Deps
from backend.agent.host import HostError
from backend.agent.trace import DIGEST_CHARS
from backend.agent import prompts
from backend.core.logging import get_logger

log = get_logger(__name__)

#: 一次 `paper_search` 最多返回几条（模型爱传 top_k=10，宿主必须自己封顶）
SEARCH_MAX = 12
#: `paper_search(top_k)` 数的是**不同块**：同一条命中的中英两路会撞成同一个 ref，
#: 所以向后端要原始命中时多要几倍（同一块最多也就两条：原文 + 译文）。
SEARCH_FETCH_FACTOR = 3
#: 服务层 `project.search` 自己的封顶：多要也不能超过它
SEARCH_FETCH_MAX = 20

#: `paper_search` 每条命中的"面"：`en` 原文 / `zh` 译文 / `formula` 公式。
#: 默认 `both` —— 跨语言召回本身有价值（英文论文里用中文概念问、译文里术语对不上原文都得看），
#: 把默认设成 `zh` 会**悄悄收窄**召回，用户还以为"论文里就这些"。
SEARCH_SIDES = ("both", "en", "zh", "formula")
#: 文本指纹兜底合并的**最短片段长度**：少于这么多字（「见图 3」「如上所述」这种）不凭指纹合并
#: —— 短模板句满篇都是，按指纹并会把**真的不同位置**合成一条。
TEXT_FP_MIN = 12
#: `formula_get` 一页给几条：默认 40 就是以前那个上限（向后兼容），上限 200。
#: 条目多时按 `offset` / `next_offset` 翻页 —— 以前只给「前 40 条 + 还差哪几页」，
#: 模型只能一页页 `formula_get(page=…)` 去补。
FORMULA_PAGE_DEFAULT = 40
FORMULA_PAGE_MAX = 200
#: 一次批量改写最多多少处（页 × 侧）。定在 60：一篇 30 页的论文「全文两侧」正好装得下，
#: 再多就要求分批（每次改动都会写进改动日志，几十页连改没人看得住）。
EDIT_BATCH_MAX = 60

#: 一次批量建高亮 / 笔记最多多少条
ANNOTATE_BATCH_MAX = 24

#: 一次 `block_get` 最多展开几个块（`refs=[…]`）
BATCH_REF_MAX = 12
#: 一次批量检索（paper_search / kb_search / notes_search / web_search）最多几个词（`queries=[…]`）
BATCH_QUERY_MAX = 8
#: 一次 `web_fetch` 最多抓几个网址（`urls=[…]`）
BATCH_URL_MAX = 6
#: 批量抓网页的并发数与单条超时（秒）：一次调用要抓 6 个，串行 + 无超时会把整个回合拖死
BATCH_URL_CONCURRENCY = 3
WEB_FETCH_TIMEOUT = 25.0
#: 一次 `figure_list` 最多看几页（`pages=[…]`）
BATCH_PAGE_MAX = 6
#: 一次 `figure_image_get` 最多取几个区域（`items=[{page, index}, …]`）
BATCH_FIGURE_MAX = 6
#: `notes_search` 的两个**闭集**参数。以前拼错会**静默降级**（scope 不认就当 paper、
#: kind 不认就当 all）：模型以为查了全库、其实只有本篇，然后自信地说"你库里没有别的论文讲过"。
NOTES_SCOPES = ("paper", "all")
NOTES_KINDS = ("all", "notes", "highlights", "annotations")

#: `library_list` 一次最多列几条（主后端 /agent/library 自己也封顶 30 条）
LIBRARY_LIST_MAX = 50
LIBRARY_LIST_DEFAULT = 30

#: 一次 `page_markdown_get` 最多读几页正文
TEXT_PAGES_MAX = 20
#: 片段长度上限：给全文就等于把"读哪页"的决定权收走
SNIPPET_CHARS = 200
#: **图注**的长度上限 —— 与 `backend/services/agent/project.py` 的 `CAPTION_CHARS` 同口径。
#: 为什么不能沿用 `SNIPPET_CHARS`：图注天生是"一整句结论"（"Speedup is measured relative to
#: the PyTorch einsum baseline."），200 字符必然截在半句 —— 实测第 8 页图 2（234 字符）被切成
#: "…Speedup is measured relat…"、图 3（313 字符）被切成 "…While some methods struggle with
#: recall, T…"，模型照着残缺图注描述图就丢了后半句。`SNIPPET_CHARS` 的默认值**不动**：
#: 正文片段几十处都靠它把"读哪页"的决定权留给模型。
CAPTION_CHARS = 800

#: 一页或一段区间（`4` / `4-7`）：分隔符与噪声词在 `_expand_pages` 里先洗掉。
_PAGES_ITEM_RE = re.compile(r"^(\d+)(?:-(\d+))?$")

#: 页码串里的前缀与词：`page 4` / `p4` / `pp. 4` / `第 4 页`
_PAGES_LABEL_RE = re.compile(r"(?i)\b(?:pages?|pp?)(?=[\s.0-9]|$)|第|页")
#: `4 to 7` → `4-7`；`4 and 7` → `4,7`
_PAGES_RANGE_WORD_RE = re.compile(r"(?i)\b(?:to|through|thru)\b|到|至")
_PAGES_LIST_WORD_RE = re.compile(r"(?i)\band\b|和|与|及")
#: 各种长短破折号、波浪、全角都当连字符
_PAGES_DASHES = "\u2010\u2011\u2012\u2013\u2014\u2015\u2212\uff0d\uff5e\u301c~"
_PAGES_FULLWIDTH = str.maketrans("０１２３４５６７８９，；、：～", "0123456789,;,:~")
_PAGES_ALL = {"all", "whole", "entire", "everything", "全部", "全文", "整篇", "*"}


# --------------------------------------------------------------------------
# 工具实现用的小工具
# --------------------------------------------------------------------------


def _expand_pages(spec: str, *, total: int = 0) -> list[int]:
    """把模型写出来的页码串摊成页码（**不判越界、不截断**）。

    实测 2026-12-07：模型想读第 4、7 页，写的是 ``"4,7"`` —— 老解析器只认单页或单区间，
    直接回「页码格式不对」，它只能再发一次，thinking 里能看见这一步白跑。模型顺手的
    ``"p4"`` / ``"pages 4-7"`` / ``"第 4 页"`` / ``"4 to 7"`` 同样没人认。
    所以这里先洗成 `数,数-数` 再摊平；认不出来才返回空，由调用方决定怎么报错。
    """
    text = (spec or "").strip().lower()
    if not text:
        return []
    text = text.translate(_PAGES_FULLWIDTH)
    for ch in _PAGES_DASHES:
        text = text.replace(ch, "-")
    text = _PAGES_RANGE_WORD_RE.sub("-", text)
    text = _PAGES_LIST_WORD_RE.sub(",", text)
    text = _PAGES_LABEL_RE.sub(" ", text)
    text = re.sub(r"\s*-\s*", "-", text)
    stripped = text.strip(" .")
    if stripped in _PAGES_ALL or (stripped and set(stripped) <= {"*"}):
        return list(range(1, max(total, 1) + 1))
    pages: list[int] = []
    for part in re.split(r"[\s,;:~]+", text):
        m = _PAGES_ITEM_RE.match(part.strip(" .*"))
        if not m:
            continue
        a = int(m.group(1))
        b = int(m.group(2)) if m.group(2) else a
        pages.extend(range(min(a, b), max(a, b) + 1))
    if not pages:  # 兜底：把串里剩下的整数当单页（`"4，7页"` 这种已经分好了，只捡残余）
        pages = [int(n) for n in re.findall(r"\d+", text)]
    seen: list[int] = []
    for p in pages:
        if p not in seen:
            seen.append(p)
    return seen


def parse_pages(spec: str, *, total: int, limit: int) -> tuple[list[int], bool]:
    """``"5"`` / ``"3-8"`` / ``"4,7"`` / ``"all"`` → 页码列表。返回 ``(页码, 是否被截断)``。"""
    pages = [p for p in _expand_pages(spec, total=total)
             if p >= 1 and (not total or p <= total)]
    return pages[:limit], len(pages) > limit


def pages_error(spec: str, *, total: int) -> str:
    """页码解析完是空时的**诊断**：区分「写法不认识」和「超出页码范围」。

    以前两种都回「页码格式不对」：模型写了 ``"99"`` 而全文只有 22 页，它以为是自己写法错了，
    换个写法再试一次 —— 白跑一步，错还在。
    """
    raw = _expand_pages(spec, total=total)
    if raw and total:
        lo, hi = min(raw), max(raw)
        where = f"第 {lo} 页" if lo == hi else f"第 {lo}-{hi} 页"
        return f"没有{where}（这篇共 {total} 页）。"
    return f'页码没读懂：「{_clip(spec, 20) if spec else "空"}」。可以写 "5" / "3-8" / "4,7" / "all"。'


def _cap_batch(rows: list, cap: int) -> tuple[list, int]:
    """截到上限，返回 (留下的, 被砍掉的条数)。

    **被砍的条数必须回给模型**：静默丢一半，它会以为那些条已经处理过了。
    """
    return rows[:cap], max(0, len(rows) - cap)


def _left_note(left: int, cap: int, what: str) -> str:
    """「还有 N 条没处理」那句话（没有截断就是空串，不占回执）。"""
    return f"还有 {left} {what}没处理（一次最多 {cap} {what}）：分批再来。" if left else ""


def _grouped_receipt(groups: list[dict], *, wanted: int, left: int, repeats: int,
                     cap: int, hint: str) -> dict:
    """把逐词的结果拼成**按 query 分组**的批量回执（一个词一组，绝不混成一锅）。

    为什么要分组：一次给 4 个检索词，如果命中混在一个 `items` 数组里，模型没法说清
    "哪几条是查 X 得到的"，引用也会张冠李戴。分组之后每组就是一次老调用的原样回执。

    顶层 `returned` 与组内 `returned` **同名不同义**（前者各组之和、后者该组条数）：
    组内再补一个 `group_returned` 把"这是本组的条数"写明。老字段名一个都不动。
    """
    failed = [g for g in groups if g.get("ok") is False]
    tips: list[str] = []
    if left:
        tips.append(_left_note(left, cap, "条"))
    if repeats:
        tips.append(f"重复的 {repeats} 条已合并。")
    if failed:
        tips.append("没查成的那几条单独带 ok=false 与原因，别的分组照常给出 —— 只补那几条，"
                    "不要整批重来。")
    # 每组另给一个 `group_returned`：顶层那个 returned 是各组之和，组内这个才是该组
    # 条数 —— 同名不同义最容易读错。拷一份再改，别动缓存里存着的那份。
    rows = [{**g, "group_returned": int(g.get("returned") or 0)} for g in groups]
    out = {"requested": wanted,
           "returned": sum(int(g.get("returned") or 0) for g in groups),
           "failed": len(failed), "unprocessed": left, "groups": rows}
    if tips:
        out["note"] = " ".join(tips)
    if hint:
        out["hint"] = hint
    return out


def _text_list(single: str, multi: list | None) -> tuple[list[str], int]:
    """把「单条参数 + 批量参数」摊成一份待办清单：单条在前、批量在后，去重（大小写不敏感）。

    返回 (清单, 因去重合并掉的条数)。两种都给的场景真实存在 —— 模型把老参数也顺手填了，
    合并去重比报错有用（老写法因此一个字都不用改）。
    """
    out: list[str] = []
    seen: set[str] = set()
    repeats = 0
    for raw in ([single] if str(single or "").strip() else []) + list(multi or []):
        text = str(raw or "").strip()
        if not text:
            continue
        key = " ".join(text.lower().split())
        if key in seen:
            repeats += 1
            continue
        seen.add(key)
        out.append(text)
    return out, repeats


def _int_list(single: object, multi: list | None) -> tuple[list[int], int]:
    """同上，但元素是整数（ref / 页码）：认不出数字的那一条丢掉，不整批报错。"""
    out: list[int] = []
    repeats = 0
    for raw in ([single] if single is not None else []) + list(multi or []):
        try:
            value = int(str(raw).strip())
        except (TypeError, ValueError):
            continue
        if value in out:
            repeats += 1
            continue
        out.append(value)
    return out, repeats


def _as_args(args: object) -> dict:
    """工具入参：框架给过 dict，也给过 **JSON 串** —— 两种都要认。

    实测 2026-12-07：过程行显示成 ``{"find": "\\frac{\\sqrt{3\\pi^2}...``，就是因为参数
    这时是 JSON **串**，老代码直接 `str(args)[:120]`。
    """
    if isinstance(args, dict):
        return args
    if isinstance(args, str) and args.strip().startswith("{"):
        try:
            got = json.loads(args)
        except json.JSONDecodeError:
            return {}
        return got if isinstance(got, dict) else {}
    return {}


def _brief_text(value: object, limit: int = 18) -> str:
    body = re.sub(r"\s+", " ", str(value or "")).strip()
    return body if len(body) <= limit else body[:limit] + "…"


def _quote(value: object, limit: int = 18) -> str:
    """把原文 / 关键词包成「…」；空的**不出空括号**（过程行里那就是一对空的「」）。"""
    body = _brief_text(value, limit)
    return f"「{body}」" if body else ""


def _line(*bits: object) -> str:
    """人话片段接成一行；空片段直接丢（否则会出现「第 5 页 ·  · 」这种空洞）。"""
    return " · ".join(str(one) for one in bits if str(one or "").strip())


def _anchor_label(anchor: object) -> str:
    """``md:p5`` / ``p5-3`` / ``p008-02`` → 「第 5 页」/「第 5 页第 3 行」（认不出就不带）。

    ``p008-02`` 是 `figure_list` 回执里那种**行 id**（页码-行号），行号常带前导零，
    所以按整数解一次；认不出的锚点宁可不写 —— 编一个上去只会让用户找不到高亮。
    """
    m = re.match(r"^(?:md:)?p(\d{1,5})(?:-0*(\d+))?$", str(anchor or "").strip())
    if not m:
        return ""
    row = f"第 {int(m.group(2))} 行" if m.group(2) else ""
    return f"第 {int(m.group(1))} 页{row}"


def _many_label(value: object, limit: int = 3) -> str:
    """一批整数（ref / 页码）压成一句人话：``[3, 5, 9]`` → 「3、5、9」，多的用 ``等 5 个`` 收尾。

    过程行只有一行宽，逐个数列出来会挤掉后面那截；所以只给前几个，剩下用总数交代。
    """
    rows = [str(int(one)) for one in (value or []) if str(one).strip().lstrip("-").isdigit()]
    if not rows:
        return ""
    head = "、".join(rows[:limit])
    return head if len(rows) <= limit else f"{head} 等 {len(rows)} 个"


def _page_label(value: object) -> str:
    """页码人话：单页、区间、一串、列表都收，多页只在**连着**时压成区间。

    [3, 4, 5] 是「第 3-5 页」，[4, 7] 仍写「第 4、7 页」—— 两页不相邻，写成区间
    等于说 4-7 页都读过。以前列表进来只会变成「第 [3, 4, 5] 页」这种原始 repr。
    """
    if isinstance(value, (list, tuple)):
        rows = [str(one).strip() for one in value if str(one).strip().lstrip("-").isdigit()]
        value = "、".join(rows)
    text = str(value if value is not None else "").strip()
    if text in ("", "0"):
        return ""
    if text.lower() in ("all", "*", "全部", "全文"):
        return "整篇"
    core = re.sub(r"^\s*(?:pages?|p)\.?\s*", "", text, flags=re.I)
    core = re.sub(r"^\s*第\s*", "", core)
    core = re.sub(r"\s*页\s*$", "", core)
    core = re.sub(r"\s+", "", core)
    if core and re.fullmatch(r"[\d,\-、]+", core):
        nums = [int(one) for one in re.findall(r"\d+", core)]
        if len(nums) >= 3 and nums == list(range(nums[0], nums[0] + len(nums))):
            return f"第 {nums[0]}-{nums[-1]} 页"
        if len(nums) == 1:
            return f"第 {nums[0]} 页"
        return f"第 {core} 页"
    return f"第 {_brief_text(text, 20)} 页"


def tool_brief(name: str, args: object) -> str:
    """工具调用的**一句话人话** —— 过程行 `修订产物 · 第 4 页 · …` 里 `·` 后面那截。

    为什么放在这一层而不是前端按工具名拼：只有工具自己知道参数叫什么、截到哪儿才不像乱码。
    中文**短语**（`修订产物` 这类）仍然留在前端（`TOOL_STEPS`），这里只出参数里的人话。
    用户口径 2026-12-07：「工具调用看不出在干什么」。2026-12-08 补齐 19 个工具的入参 ——
    以前 `note_manage` 只写「新增笔记」、`highlight_manage` 丢掉被标的原文、
    `paper_meta_write` 只写 venue，用户从那一行看不出这次到底动了什么。
    """
    a = _as_args(args)
    tool = str(name or "")
    if tool in ("reader_state", "paper_archive_get"):
        # 无入参：给空串让前端短语自己兜底；「None」「、」都只是噪音
        return ""
    pages, page = _page_label(a.get("pages")), _page_label(a.get("page"))
    where = page or pages
    query = _quote(a.get("query"), 20)
    brief = ""
    if tool in ("page_markdown_get", "translate_get", "page_image_get"):
        brief = pages or page
    elif tool == "block_get":
        many = _many_label(a.get("refs"))
        ref = a.get("ref")
        brief = f"引用 [{many}]" if many else (f"引用 [{ref}]" if ref not in (None, "") else "")
    elif tool in ("figure_list", "figure_image_get"):
        seen = _page_label([one.get("page") for one in a.get("items") or []
                            if isinstance(one, dict)])
        bits = [where or seen]
        if tool == "figure_image_get":
            index = a.get("index")
            if index not in (None, ""):
                bits.append(f"第 {_brief_text(index, 8)} 张")
            elif a.get("items"):
                bits.append(f"{len(a.get('items') or [])} 个区域")
        brief = _line(*bits)
    elif tool == "formula_get":
        # 只写页码等于说"取了公式"；用户要的是"在找哪个式子、要不要行内的"
        brief = _line(where, "找" + query if query else "",
                      "含行内" if a.get("include_inline") else "")
    elif tool in ("highlight_manage", "note_manage"):
        op = str(a.get("op") or "").strip().lower()
        is_hl = tool == "highlight_manage"
        kind = "高亮" if is_hl else "笔记"
        items = [one for one in a.get("items") or [] if isinstance(one, dict)]
        body = _quote(a.get("text") if is_hl else a.get("content"), 20)
        on = _anchor_label(a.get("anchor")) or where
        hang = _brief_text(a.get("highlight_id"), 12)
        if not on and not is_hl and hang:
            on = f"挂在 {hang}"
        if op in ("create", "add", "new"):
            brief = (f"新增 {len(items)} {'处' if is_hl else '条'}{kind}" if items
                     else _line(f"新增{kind}", on, body))
        elif op in ("update", "edit"):
            moved = is_hl and bool(_anchor_label(a.get("anchor")))
            brief = _line(f"挪{kind}" if moved else f"改{kind}", on, body)
        elif op in ("delete", "remove"):
            brief = f"删掉一条{kind}"
        elif op == "locate":
            brief = f"查这条{kind}"
        else:
            brief = _line(f"查{kind}", query)
    elif tool == "paper_meta_write":
        fields = [str(one).strip() for one in (a.get("fields") or []) if str(one).strip()]
        if not fields:
            # 入参也可能是各字段本身（模型直接给 venue=… / year=…）：照样数得出写了几项
            fields = sorted(k for k, v in a.items()
                            if k != "fields" and v not in (None, "", [], 0))
        head = f"{len(fields)} 项 · " + "、".join(fields[:3]) if fields else ""
        venue = _brief_text(a.get("venue"), 16)
        year = str(a.get("year") if a.get("year") is not None else "").strip()
        brief = _line(head, venue, f"{year} 年" if year not in ("", "0") else "")
    elif tool == "page_markdown_edit":
        op = str(a.get("op") or "").strip().lower()
        if op in ("undo", "revert"):
            brief = _line(where, "撤回上一处")
        elif op in ("view", "read", "show"):
            brief = where
        else:
            find, rep = _quote(a.get("find"), 16), _quote(a.get("replace"), 16)
            tail = f"{find}→{rep}" if find and rep else (f"删掉{find}" if find else "")
            brief = _line(where, tail)
    elif tool in ("paper_search", "kb_search", "library_list", "notes_search", "web_search"):
        batch = [str(one).strip() for one in (a.get("queries") or []) if str(one).strip()]
        if batch:
            head = "」「".join(_brief_text(one, 14) for one in batch[:2])
            base = f"「{head}」" + (f" 等 {len(batch)} 个" if len(batch) > 2 else "")
        else:
            base = query
        tail = ""
        if tool in ("paper_search", "kb_search"):
            # 8 是签名里的默认值，写上去只是噪音；改过才看得出"这次要多要几条"
            top_k = str(a.get("top_k") if a.get("top_k") is not None else 8).strip()
            tail = "" if top_k in ("", "0", "8") else f"{top_k} 条"
        elif tool == "web_search":
            count = str(a.get("count") if a.get("count") is not None else "").strip()
            tail = "" if count in ("", "0") else f"{count} 条"
        elif tool == "library_list":
            limit = str(a.get("limit") if a.get("limit") is not None else "").strip()
            tail = "" if limit in ("", "0") else f"{limit} 条"
        else:
            # scope / kind 是默认值（这一篇、全都要）时不写：写了等于没写
            scope = str(a.get("scope") or "").strip().lower()
            kind = {"notes": "笔记", "highlights": "高亮", "annotations": "批注"}.get(
                str(a.get("kind") or "").strip().lower(), "")
            tail = _line("全库" if scope in ("all", "library", "全库") else "", kind)
        brief = _line(base, tail)
    elif tool == "web_fetch":
        urls = [str(one).strip() for one in (a.get("urls") or []) if str(one).strip()]
        if urls:
            head = _brief_text(urls[0], 32)
            brief = f"{head} 等 {len(urls)} 篇" if len(urls) > 1 else head
        else:
            brief = _brief_text(a.get("url"), 32)
    else:
        # 没登记的新工具退回 k=v：总比整行空白强。登记过的工具不会走到这里
        brief = "、".join(f"{k}={_brief_text(v, 12)}" for k, v in list(a.items())[:3])
    return brief[:DIGEST_CHARS]


def _clip(text: str, limit: int = SNIPPET_CHARS) -> str:
    t = re.sub(r"\s+", " ", (text or "").strip())
    return t if len(t) <= limit else t[:limit - 1] + "…"


def _snippet_around(text: str, query: str, limit: int = SNIPPET_CHARS) -> str:
    """片段以**匹配处**为中心截（默认从块首截，命中点常常在片段之外）。

    实测 2026-12-07：搜「〔自检甲〕」命中第 5 页那块，可片段给的是「注意式 (3) 中的 …」——
    标记在下一段末尾，回执看起来像"没搜到"。块再长一点，命中处就彻底被截掉了。
    先按整串找，找不到再按词找；都不中才退回块首。
    """
    body = re.sub(r"\s+", " ", str(text or "")).strip()
    if not body:
        return ""
    at = -1
    for needle in [str(query or "").strip()] + [w for w in str(query or "").split() if len(w) >= 2]:
        if not needle:
            continue
        found = body.lower().find(needle.lower())
        if found >= 0:
            at = found
            break
    if at < 0 or len(body) <= limit:
        return _clip(body, limit)
    # 省略号也算字符预算：`SNIPPET_CHARS` 是"一条片段最多多长"，不是"正文内外加省略号"
    inner = max(1, limit - 2)
    start = max(0, min(at - inner // 3, len(body) - inner))
    end = min(len(body), start + inner)
    head, tail = ("…" if start else ""), ("…" if end < len(body) else "")
    return (head + body[start:end] + tail)[:limit]


def _latex_key(latex: str) -> str:
    """公式引用键用的短摘要（**不能只取前 N 个字符**）。

    实测 2026-12-07（第 4 页）：两条公式都以 `D_{\\text{prod}}(Q_{\\text{prod}})` 开头，前 24 字符
    一模一样 —— 键一撞，`RefRegistry.assign` 就把第二条当成"同一处证据"复用第一个 ref，
    于是回执里出现两个 ``[7]``：一条是上界、一条是数值收紧，模型引用时分不清指哪条。
    """
    return hashlib.sha1(" ".join(str(latex or "").split()).encode("utf-8")).hexdigest()[:12]


#: 公式**去重键**归一化时要剥掉的「只改字形」的包装宏：`\boldsymbol{y}` / `\mathbf{y}` /
#: `\bm{y}` —— 还有不带花括号的 `\boldsymbol y`（`_LATEX_WRAP_BARE_RE`）。
_LATEX_WRAP_RE = re.compile(r"\\(?:mathbf|boldsymbol|bm)(?![a-zA-Z])\s*\{([^{}]*)\}")
_LATEX_WRAP_BARE_RE = re.compile(r"\\(?:mathbf|boldsymbol|bm)(?![a-zA-Z])")
#: 只影响排版、不影响语义的东西：`\left` / `\right`、细间距、`\quad` 一类、`~`、所有空白
_LATEX_SHRINK_RE = re.compile(
    r"\\left(?![a-zA-Z])|\\right(?![a-zA-Z])|\\[,;:!>]"
    r"|\\(?:quad|qquad|thinspace|medspace)(?![a-zA-Z])|~|\s+")


def _latex_norm_key(latex: object) -> str:
    r"""公式去重键用的**归一化** LaTeX：剥掉只改字形的包装，**留下内容本体**。

    为什么：同一条式子会被重复陈述，而两处抄写常常差一层字重包装 —— 实测 TurboQuant
    正文第 7 页写 `\|y\|_2^2`、附录第 19 页写 `\|\boldsymbol{y}\|_2^2`（期望内积式同理），
    按原样比就是两条：`also_on` 一直是空的，"4 个不同位置"里同一处被数了两遍。
    所以剥 `\boldsymbol{…}` / `\mathbf{…}` / `\bm{…}`（含不带花括号的写法）、
    `\left` / `\right`、细间距与空白之后才比 —— 但**必须留下花括号里的本体**
    （`\boldsymbol{y}` → `y`），否则 `y` 与 `\boldsymbol{y}` 仍然不等，等于没剥。

    不剥：上下标、`\frac` / `\sum` 这类结构，以及 `\text{…}` 里的字 —— 那是**语义**；
    合并两条不同的式子比留一条重复糟得多。
    """
    text = str(latex or "")
    for _ in range(3):    # 手抄会出现 `\boldsymbol{\mathbf{y}}` 这种套娃，一轮只剥一层
        inner = _LATEX_WRAP_RE.sub(r"\1", text)
        if inner == text:
            break
        text = inner
    text = _LATEX_WRAP_BARE_RE.sub("", text)
    text = _LATEX_SHRINK_RE.sub("", text)
    return text.rstrip(".,;")


def _merge_formula_dups(items: list[dict]) -> tuple[list[dict], int]:
    """同一条公式在**不同页**重复陈述 → 只留首次出现那条，其余页码进 `also_on`。

    只在**跨页**时合并：同一页上的两行同名公式是这一页的两个排版位置（`also_on` 只记页码，
    给不出区分度），各自的引用要分别给号 —— 老行为不动。行内条目不参与：`$x$` 在几十页
    各有一份，那正是它的价值（`formula_get(page=…)` 靠它定位）。

    返回（合并后的列表, 合并掉的条数）：条数要用来把 `display_total` / `total` 一起减下去，
    否则回执里「列表 40 条、总数 42」自相矛盾。
    """
    by_key: dict[str, dict] = {}
    kept: list[dict] = []
    merged = 0
    for item in items:
        key = _latex_norm_key(item.get("latex")) if item.get("display") else ""
        first = by_key.get(key) if key else None
        here, there = int(item.get("page") or 0), int((first or {}).get("page") or 0)
        if first is not None and here and there and here != there:
            also = first.setdefault("also_on", [])
            if here not in also:
                also.append(here)
            merged += 1
            continue
        if key:
            by_key.setdefault(key, item)
        kept.append(item)
    return kept, merged

def _caption_rows(paper_id: str, page: int) -> dict[str, str]:
    """这一页「图号 → 图注行的行 id」：引用要带行锚点，点 `[n]` 才闪得到那一行。

    拿不到（还没翻译 / 扫描件）就回空表 —— 引用照样能跳页，只是没有行锚点。
    """
    try:
        from backend.services.paper import reader_service

        return reader_service.caption_row_ids(paper_id, int(page))
    except Exception as exc:  # noqa: BLE001 —— 锚点拿不到不该让工具失败
        log.debug("取图注行 id 失败（不影响看图）：%s", exc)
        return {}


def _locate(page: int | None, section: str | None, kind: str = "") -> str:
    bits = []
    if page:
        bits.append(f"第 {page} 页")
    if section:
        bits.append(str(section))
    if kind:
        bits.append(_KIND_ZH.get(kind, kind))
    return " · ".join(bits)


_KIND_ZH = {"unit": "正文单元", "text": "正文", "formula": "公式", "figure": "插图",
            "table": "表格", "page": "整页", "para": "段落", "heading": "标题"}


async def _guarded(ctx: RunContext[Deps], name: str, digest: dict, coro):
    """跑一个工具实现并记轨迹；主后端报错 → 变成给模型看的结果，不抛栈。"""
    started = time.perf_counter()
    try:
        data = await coro
        ctx.deps.note_tool(name, digest, "ok", started)
        return data
    except HostError as exc:
        ctx.deps.note_tool(name, digest, "error", started, exc.message)
        return {"ok": False, "error": f"取数失败：{exc.message}",
                "hint": "可以换一种方式再试（比如换检索词、换一页），或如实说明拿不到。"}
    except Exception as exc:  # noqa: BLE001
        ctx.deps.note_tool(name, digest, "error", started, str(exc))
        return {"ok": False, "error": f"工具内部错误：{type(exc).__name__}: {exc}"[:200]}


def _query_key(name: str, query: str, k: int) -> str:
    return f"{name}:{' '.join((query or '').lower().split())}:{k}"


def _search_mode_slot(mode: str | None) -> int:
    """`paper_search` 的检索方式三态编码（0 = 不传 → auto）。

    缓存键如果只有 (query, top_k)，"auto / rag / text"三次调用会互相命中 ——
    而它们的**命中集合本来就不一样**（词法命中精确词、向量命中近义说法）。串味之后
    模型会以为"换个方式也没搜到"，恰好抹掉这次改动要解决的问题。
    """
    return {"rag": 1, "text": 2}.get(str(mode or "").strip().lower(), 0)


def _search_side_slot(side: str) -> int:
    """`paper_search` 的 `side` 缓存分槽：`both`（默认）占 0，其余各占一格。

    缓存键里不带这一位，"先查 both、再查 zh"会直接命中前者，模型拿到的仍是全集 ——
    而它以为自己按面过滤过了，接着就会说"译文里没有这句"。
    """
    return SEARCH_SIDES.index(side) if side in SEARCH_SIDES else 0


def _hit_side(raw: dict) -> str:
    r"""一条检索命中落在哪一面：`en` 原文 / `zh` 译文 / `formula` 公式。

    以前回执里**没有**这个字段，模型只能靠"片段里有没有汉字"反推；纯公式片段
    （`$D_{\text{mse}} \leq …$`）里一个汉字都没有，于是被当成原文引了出去。而
    "这条是译文还是原文"决定它能不能当**原话**引用，所以必须显式给。

    公式先判：块级公式两侧的 LaTeX 是同一串（译文不改公式），`lang` 对它没有意义；
    图/表的 `lang` 是 `any`，按**原文面**算（它们没有译文孪生）。
    """
    if str(raw.get("chunk_type") or "") == "formula":
        return "formula"
    return "zh" if str(raw.get("lang") or "").strip().lower() == "zh" else "en"


def _cached(ctx: RunContext[Deps], name: str, query: str, k: int) -> dict | None:
    """同一条检索重复调用时直接回上一次的结果（省一次后端往返，也提醒模型别绕）。"""
    hit = ctx.deps.query_cache.get(_query_key(name, query, k))
    if hit is None:
        return None
    return {**hit, "note": "这条检索与上一次完全相同，宿主**没有重新请求后端**（直接复用了上次的"
                          "结果）。要确认内容改动带来的变化，就换一个能区分新旧的词重搜；"
                          "正文一改或撤回，这份缓存就会清空。"}


def _remember(ctx: RunContext[Deps], name: str, query: str, k: int, payload: dict) -> None:
    ctx.deps.query_cache[_query_key(name, query, k)] = payload


def _forget_searches(ctx: RunContext[Deps], why: str) -> None:
    """正文一改，缓存里的检索结果就带着旧文字作废了。

    缓存键只有 (工具名, query, k)，**不含内容版本**：同一轮里"搜一次 → 改正文 → 再搜同一个
    词"会直接命中缓存，回的是改动前那份片段。索引本身是干净的（实测撤销后 LanceDB 里既没有
    旧词也没有重复 id），所以只需要在写入时把缓存清掉。
    """
    if ctx.deps.query_cache:
        log.debug("正文改动（%s）：清空 %d 条检索缓存", why, len(ctx.deps.query_cache))
        ctx.deps.query_cache.clear()


def _stale_hint() -> str:
    """这一趟检索没有带来新依据 → 明确告诉模型"够了"，别把轮数耗在换词上。"""
    return ("这次检索没有带来**新的**依据（命中的都是已经拿到的那些片段）。"
            "请立刻基于已有材料作答；如果确实不够，就如实说材料不足。")


def _chunk_key(paper_id: str, block_id: object, chunk_id: str, snippet: str) -> str:
    """检索命中的引用去重键 = **落点 + 展示片段**，不是 chunk id。

    为什么不能用 chunk id：同一行在索引里有**两条命中** —— A 路英文 unit 与 B 路中文 unit。
    project.search 还会把中文预览配给 A 路那条（translation），于是两条命中的
    页码、block_id、片段**三者全一样**，只有 chunk id 不同。按 chunk id 去重就会发出
    两个引用号：正文里出现两枚标号、两张一模一样的速览卡（用户 2026-12-04 报的 12 / 13）。
    按「落点 + 片段」去重才能把它们合并成一条 —— 同一处证据只该有一个号。

    片段不同的两条（例如同一行里指向两条**不同公式**、或一条带 latex）仍然分开：
    它们确实指向不同的证据。
    """
    loc = str(block_id or chunk_id or "")
    sig = hashlib.sha1(snippet.encode("utf-8")).hexdigest()[:12]
    return f"chunk:{paper_id}:{loc}:{sig}"


def _hit_page(raw: dict) -> int | None:
    """一条检索命中**对外报的页码** —— 全篇只有一个入口，谁都别再自己读一遍字段。

    真相源是**行 id**（`p021-06` → 第 21 页）：那是阅读器正文的行号，`block_get`、
    高亮锚点、跳页全按它算。检索索引另带一条 `page_no`，实测会与行 id 差一页 ——
    两处各读各的，于是同一轮里 `paper_search` 说第 20 页、`block_get` 说第 21 页
    （用户 2026-12-07 实测）；连带把"同一块的中英两路"判成两页、拒绝合并。
    只有**拿不到行 id**（老投影只给块 id 与页码）时才退回索引的 `page_no` / `page`。

    `block_id` 也看一眼：写侧把行 id 塞进了这个字段（见 `project.anchor_ids`），
    老投影里它是**唯一**的行身份来源。解析复用 `_row_page`，不另写一套正则。
    """
    for cand in ([raw.get("row_id")]
                 + [rid for rid in (raw.get("row_ids") or [])]
                 + [raw.get("block_id")]):
        page = _row_page(cand)
        if page:
            return page
    return int(raw.get("page_no") or raw.get("page") or 0) or None


def _chunk_identity(ctx: RunContext[Deps], item: dict) -> dict:
    """一条检索命中的**引用身份**：去重键 + 落点 + 片段（不碰引用表）。

    `paper_search` 要按这个键**先去重再截断**（top_k 数的是不同块），`_register_chunk`
    要按同一个键登记引用号 —— 两处必须用同一份算法：各写一遍就会出现
    "去重说这是两个块、登记却说这是同一个号"的错配。

    页码一律走 `_hit_page`（行 id 优先），绝不在这里读索引的 `page_no`：对外报的页
    必须与 `block_get` 一致，去重比较的也必须是**这同一个数**。
    """
    chunk_id = str(item.get("chunk_id") or "")
    paper_id = str(item.get("paper_id") or ctx.deps.paper_id)
    # 片段优先用译文：给用户看的引用表是中文语境；原文留给模型去读
    snippet = _clip(item.get("translation") or item.get("content") or "")
    #: 这条命中覆盖的正文行：写侧把行 id 也塞进了 block_id，所以行身份从这里来
    row_ids = [str(rid) for rid in (item.get("row_ids") or []) if str(rid).strip()]
    row_id = str(item.get("row_id") or "") or (row_ids[0] if row_ids else "")
    # 兜底再判一次名字空间：即便上游把行 id 塞在 block_id 里（存量投影/别的调用方），
    # 也绝不让它进引用表的 block_id —— 那等于给 block_get 一个必然 404 的入参。
    raw_block = item.get("block_id")
    block_id = None if _row_id_of(raw_block) else raw_block
    return {
        # 去重键用"落点"：block_id 可能是 None（行 id 形态已被剥掉），这时用行 id 顶上 ——
        # 少了它，两个**片段一模一样但页码不同**的段落会并成一个引用号
        "key": _chunk_key(paper_id, block_id or row_id, chunk_id, snippet),
        "chunk_id": chunk_id, "paper_id": paper_id,
        "page": _hit_page(item),
        "section": item.get("section") or None,
        "block_id": block_id, "row_id": row_id, "row_ids": row_ids,
        "snippet": snippet,
    }


def _block_key(ctx: RunContext[Deps], item: dict) -> str:
    """**同一个块**的键：落点 + chunk 类型 + 它覆盖的行（`paper_search` 去重用）。

    为什么不能拿引用的去重键 `_chunk_key` 直接去重：那里面带了**展示片段**，而同一块的
    两路命中拿到的片段不一定逐字相同 —— 英文那条的"译文预览"来自 chunk 的
    content_preview，中文那条给的是它自己的 content。实测（2026-12-07）同一块
    `p003-13…15` 的两条因此拿到**两个** ref：`top_k=4` 还是只覆盖 3 个不同位置。
    块的身份只看落点与覆盖面，与"哪一面、片段截哪儿"无关。

    反过来，同一行里指向**两条不同证据**的命中（`_chunk_key` 的注释里那个 12/13 的例子）
    行覆盖面或 chunk 类型不同 → 仍然分开，不会被并成一条。
    """
    ident = _chunk_identity(ctx, item)
    return "block:{}:{}:{}:{}".format(
        ident["paper_id"], item.get("chunk_type") or "",
        ident["block_id"] or ident["row_id"] or ident["chunk_id"],
        ",".join(ident["row_ids"]))


def _md_plain(text: object) -> str:
    """片段文本的**指纹式**归一化：去掉 Markdown 记号、空白与大小写差异。

    为什么不能直接比原文：同一处证据的中英两路常常落到**两个不同的 block**
    （`block_id` 不同，落点去重兜不住），文本只差排版记号 —— 实测 ref 3 / ref 4 的块文本
    逐字相同，却各占一个位置，于是"4 个不同位置"实际只有 3 处。
    """
    body = re.sub(r"[#*_`>\[\]]+", "", str(text or ""))
    return re.sub(r"\s+", "", body).lower()


def _text_fp(text: object) -> str:
    """片段文本的指纹（`""` = 太短，不凭指纹合并，交给落点去重就行）。

    少于 `TEXT_FP_MIN` 字的命中（「见图 3」「如上所述」）满篇都是，按指纹并会把**真的
    不同位置**合成一条 —— 用户宁可多看一条重复，也不愿少一个位置。
    """
    plain = _md_plain(text)
    if len(plain) < TEXT_FP_MIN:
        return ""
    return hashlib.sha1(plain.encode("utf-8")).hexdigest()[:12]


def _hit_text_fps(raw: dict) -> list[str]:
    """一条命中的文本指纹：原文与译文**各算一个**（另一面拿到的可能正是这一面）。"""
    return [fp for fp in {_text_fp(raw.get("content")), _text_fp(raw.get("translation"))} if fp]


def _same_text_row(index: dict[str, dict], raw: dict, page: int | None) -> dict | None:
    """按**文本指纹**找回已经收下的那条命中（**只认同页**）。

    为什么把页码带进来：论文里"模板句"（页眉、每节开头那句固定说法）会隔几页重复一次，
    只按指纹合并会把**真的不同位置**并掉，所以指纹里带了页码。为什么不放宽到相邻页：
    相邻页文本完全相同的两条实测就是**两处不同位置**（第 5 页与第 6 页那对中英命中），
    而真正的跨页续排是"后半句接前半句"、文本本来就不同（落点去重照样兜得住）。
    页码拿不到时不合并 —— 没法证明是同处证据。

    页码**由调用方按 `_hit_page` 解析好传进来**，这里绝不自己再读一遍 `page_no`：
    同一份"这是第几页"的逻辑写两遍，正是中英两路被拆成两处位置的那次不一致的温床。
    """
    if not page:
        return None
    for fp in _hit_text_fps(raw):
        row = index.get(fp)
        if row is not None and row.get("page") == page:
            return row
    return None


def _register_chunk(ctx: RunContext[Deps], item: dict) -> Citation:
    """把一条检索命中登记进引用表，返回 citation。"""
    ident = _chunk_identity(ctx, item)
    if ident["row_id"] and len(ident["row_ids"]) > 1:
        # Citation 只存首行（前端契约不动）；整段跨度留给 block_get 拼
        ctx.deps.row_spans[ident["row_id"]] = ident["row_ids"]
    return ctx.deps.registry.assign(
        key=ident["key"], kind="chunk",
        page=ident["page"], section=ident["section"], block_id=ident["block_id"],
        row_id=ident["row_id"] or None,
        snippet=ident["snippet"],
        label=_locate(ident["page"], ident["section"], item.get("chunk_type") or ""),
        ids=(ident["chunk_id"],) if ident["chunk_id"] else (),
        # 行身份（阶段 2b）：检索命中带过来的 pid，复用编号时由 assign 合并（不覆盖）。
        pids=list(item.get("pids") or []))


# --------------------------------------------------------------------------
# ① 检索：找位置，不读内容
# --------------------------------------------------------------------------


async def paper_search(ctx: RunContext[Deps], query: str = "", top_k: int = 8,
                       mode: str | None = None,
                       queries: list[str] | None = None,
                       side: str = "both") -> dict:
    """在本论文内检索，返回**候选位置**（页码、章节、短片段），不返回全文。

    什么时候用：任何"这篇论文里怎么说的 / 讲了什么 / 为什么这么做"的问题，
    先用它定位；拿到结果里的 ref 之后，**先用 `block_get(refs=[…])` 一次展开最相关的那几块**
    （那就是检索到的相似内容本体），块里上下文不够时再 page_markdown_get 读它所在的页，
    要看版面才用 page_image_get。

    检索方式怎么选（`mode`）：
      - `text`  —— 词法（BM25）：**精确词**才靠它 —— 术语、指标名、引文编号、
        `MSE` / `TurboQuant`、`Algorithm 2` 这种"必须字面出现"的东西；
      - `rag`   —— 向量（语义）：用户用**同义说法或概念描述**时靠它，
        例如"它是怎么避免梯度消失的"（论文里可能写的是"缓解深层退化"）；
      - `auto`  —— 不传就是它：两路召回 → 融合 → 重排，覆盖最全。
      一次没命中别只换词：可以**换一种方式**再试（精确词没中就换 `text`，
      概念没中就换 `rag`）。

    **一次可以查几个词**（别一个词一来回）：

        paper_search(queries=["random rotation", "quantization error"])

    回执**按 query 分组**（`groups: [{query, returned, group_returned, items}, …]`），
    顺序与你给的词一致 —— 不同词的命中**不会**混成一个数组，所以引用时说得清
    "哪几条是查哪个词得到的"。
    ⚠️ `returned` 在这个回执里**同名不同义**：顶层那个是各组之和（这一趟一共给了几条），
    组内那个是**该组**的条数。要看某一组几条就读组内的 `group_returned`（与那组的
    `returned` 同值，名字不带歧义）；要总数就读顶层的 `returned`。
    某一条查失败只在那一条上给 `ok=false` + 原因，别的照常返回。
    一次最多 8 个词；超了会截断，回执里写明「还有 N 条没处理」。

    参数：
      query   —— 单个检索词。用**论文里的说法**（术语、方法名）比用口语描述召回好。
      queries —— 一批检索词。与 query 一起给时按「query 在前、queries 在后」合并去重。
      top_k   —— 每个词最多几条**不同块**，默认 8，上限 12。同一条命中的中英两路算**一块**
                 （合并，只占一个名额），另一面记在该条的 `also_sides` 里。
      mode    —— `text` / `rag` / `auto`；不传用服务端默认（auto，两路融合）。
      side    —— 只看哪一面：`both`（**默认**，原文 + 译文 + 公式都收）/ `en` 原文 /
                 `zh` 译文 / `formula` 公式。每条命中都带 `side`，所以拿不准就别传 ——
                 英文论文用中文问时，译文行的术语往往和原文对不上，只查一面会少一半素材。
    """
    async def _one(q: str, want: str) -> dict:
        k = max(1, min(int(top_k or 8), SEARCH_MAX))
        # 向后端**多要一点**原始命中：去重之后条数会少于 k（同一块的中英两条撞成一个 ref），
        # 实测 top_k=4 只回来 3 个不同位置。多要的这部分只在去重前用，回执仍按 k 截。
        fetch = min(k * SEARCH_FETCH_FACTOR, SEARCH_FETCH_MAX)
        key = (k * 3 + _search_mode_slot(mode)) * 4 + _search_side_slot(want)
        cached = _cached(ctx, "paper_search", q, key)
        if cached is not None:
            return cached
        ctx.deps.count_search()
        before = len(ctx.deps.registry)
        data = await ctx.deps.host.search(ctx.deps.paper_id, q, top_k=fetch,
                                          mode=mode)
        raw_items = list(data.get("items") or [])
        total = int(data.get("page_total") or ctx.deps.page_total or 0)
        ctx.deps.page_total = ctx.deps.page_total or total
        # ① 按**块身份**（落点 + chunk 类型 + 覆盖的行，见 `_block_key`）去重：同一块的中英
        #    两路会各占一行 —— top_k 的预算被同一处位置吃掉，用户还看不出它们是同一条
        #    （实测：top_k=4 回 4 条，实际只有 3 个不同位置）。
        #    只留先到的那条（相关度更高），另一条的**面**记进 `also_sides`（信息不丢）。
        merged = 0
        uniq: list[dict] = []
        by_key: dict[str, dict] = {}
        #: 文本指纹兜底：中英两路落到**两个不同 block**（落点去重兜不住）但文本逐字相同时，
        #: 仍然算同一处证据。见 `_same_text_row`（只认同页，且至少 `TEXT_FP_MIN` 字）。
        by_text: dict[str, dict] = {}
        for raw in raw_items:
            block = _block_key(ctx, raw)
            hit_side = _hit_side(raw)
            #: 页码只解析一次（`_hit_page` = 行 id 优先），兜底去重拿它比 —— 索引那条
            #: `page_no` 与行 id 差一页时，同一块的中英两路会被当成两处（实测 A/B）。
            page = _hit_page(raw)
            prev = by_key.get(block) or _same_text_row(by_text, raw, page)
            if prev is not None:
                merged += 1
                if hit_side != prev["side"] and hit_side not in prev["also_sides"]:
                    prev["also_sides"].append(hit_side)
                # 同一处的后续命中也要把落点登记上：后面再来一条同块的，别又开一个新位置
                by_key.setdefault(block, prev)
                continue
            row = {"raw": raw, "side": hit_side, "also_sides": [], "page": page}
            by_key[block] = row
            for fp in _hit_text_fps(raw):
                by_text.setdefault(fp, row)
            uniq.append(row)
        # ② `side` 是**召回口径**（不改变排序）：先在去重后的集合上筛，再截断 ——
        #    否则"筛完只剩 2 条"和"筛之前那 4 条"会混成一个数，模型没法判断是不是搜窄了。
        if want != "both":
            uniq = [row for row in uniq
                    if row["side"] == want or want in row["also_sides"]]
        # ③ 截到 k **个不同块**（老语义不动：returned 是条数，顺序仍是相关度）
        items = []
        for row in uniq[:k]:
            raw = row["raw"]
            cit = _register_chunk(ctx, raw)
            item = {
                "ref": cit.ref,
                "page": cit.page,
                "section": cit.section,
                "type": raw.get("chunk_type"),
                "side": row["side"],
                "snippet": _snippet_around(raw.get("content") or raw.get("translation") or "",
                                           str(q or "")),
            }
            if row["also_sides"]:
                item["also_sides"] = row["also_sides"]
            items.append(item)
        out = {
            "query": q, "page_total": total or None, "returned": len(items),
            "items": items,
        }
        if merged:
            out["deduped"] = merged
        notes = []
        if merged:
            # 措辞只说**数得出来**的事实：并掉的是同一块的中英两路，剩下每条各是一处位置。
            # 以前写"这 N 条是 N 个不同位置"—— 同一块被数两遍时它就是在替错误背书
            # （用户 2026-12-07 实测：说 4 个位置，实际只有 3 处）。
            notes.append(f"同一处的多路命中（同一块的中英两路，或去掉 markdown 记号后逐字"
                         f"相同的片段）已按 ref 合并 {merged} 条"
                         f"（另一面记在那条的 also_sides 里）；合并后这 {len(items)} 条"
                         f"各是一处位置，不再把同一块数两遍。")
        if not items:
            base = ("本论文内没有检索到相关内容。可以换一个更贴近论文用词的说法；"
                    "仍无结果就如实说明这篇论文里找不到依据。")
            if want != "both" and raw_items:
                base = (f"`side={want}` 一条都没留下（命中都在另一面）。"
                        f"改回 `side='both'`（默认）再查一次。")
            out["hint"] = " ".join([base] + notes)
        elif len(ctx.deps.registry) == before:
            out["hint"] = " ".join([_stale_hint()] + notes)
        else:
            out["hint"] = " ".join(
                ["这些只是片段：先用 `block_get(refs=[…])` 一次展开最相关的 2~4 块；"
                 "块里上下文不够时再 `page_markdown_get` 读它所在的页。"] + notes)
        _remember(ctx, "paper_search", q, key, out)
        return out

    async def _impl() -> dict:
        want = str(side or "both").strip().lower()
        if want not in SEARCH_SIDES:
            # 闭集校验：拼错就静默降级成 both（或 en）会让模型以为"按面筛过了"，
            # 接着断言"译文里没这句" —— 比直接报错坏得多（notes_search 同一教训）。
            return {"ok": False,
                    "error": (f"side={side!r} 不是合法取值：只能是 "
                              f"{' / '.join(SEARCH_SIDES)}。"
                              "不传就是 both（原文 + 译文 + 公式都收）。"),
                    "effective_side": "both", "returned": 0, "items": []}
        wanted, repeats = _text_list(query, queries)
        if not wanted:
            return {"ok": False,
                    "error": "要给 query 或 queries：queries=[\"词一\", \"词二\"] 一次查多个。"}
        kept, left = _cap_batch(wanted, BATCH_QUERY_MAX)
        if queries is None and len(wanted) == 1:
            return await _one(kept[0], want)   # 老用法：单个词的回执结构一个字都不变
        groups: list[dict] = []
        for q in kept:
            try:
                groups.append(await _one(q, want))
            except HostError as exc:
                groups.append({"query": q, "ok": False, "returned": 0, "items": [],
                               "error": f"取数失败：{exc.message}"})
        return _grouped_receipt(
            groups, wanted=len(wanted), left=left, repeats=repeats,
            cap=BATCH_QUERY_MAX,
            hint=("每组各查自己的词：引用时按分组说清是哪个词的命中。展开片段仍用 "
                  "block_get(refs=[…])，一次给一批。"))

    return await _guarded(ctx, "paper_search",
                          {"query": _clip(query, 60), "queries": list(queries or []),
                           "top_k": top_k, "mode": mode or "auto",
                           "side": side or "both"}, _impl())


# --------------------------------------------------------------------------
# ② 读内容：块级正文（先读后写那条约束只对写工具有效，这里纯读）
# --------------------------------------------------------------------------


async def page_markdown_get(ctx: RunContext[Deps], pages: str) -> dict:
    """读某几页的**块级正文**（原文；有译文时一并给出）。

    什么时候用：检索定位到页码之后读内容，或用户直接指定"第 5 页"。
    返回里每个块都有自己的 ref，回答时引用那个 ref。

    **内容优先来自页级产物（英文转录 / 中文翻译）**：那是阅读器与索引共用的同一份读模型，
    表格在转录里是规规矩矩的 Markdown 表格；只有**没转录过**的页才退回解析产物
    （那里表格的单元格可能是粘连的）。

    参数：
      pages —— 单页 ``"5"``、区间 ``"3-8"``、多页 ``"4,7"``、整篇 ``"all"``。
               只有确实需要通读时才用；一次最多 20 页，超了会被截断（返回 truncated=true）。
               页码写得随意也认：``"p4"`` / ``"pages 4-7"`` / ``"第 4 页"`` 都行。
    """
    async def _impl() -> dict:
        wanted, clipped = parse_pages(pages, total=ctx.deps.page_total,
                                      limit=TEXT_PAGES_MAX)
        if not wanted:
            return {"ok": False, "error": pages_error(pages, total=ctx.deps.page_total)}
        data = await ctx.deps.host.pages(ctx.deps.paper_id, ",".join(map(str, wanted)))
        total = int(data.get("page_total") or ctx.deps.page_total or 0)
        ctx.deps.page_total = ctx.deps.page_total or total
        budget = ctx.deps.cfg.evidence_chars
        out_pages: list[dict] = []
        truncated = clipped
        for page in data.get("pages") or []:
            page_no = int(page.get("page") or 0)
            blocks: list[dict] = []
            for block in page.get("blocks") or []:
                text = str(block.get("text") or "").strip()
                if not text:
                    continue
                if budget - len(text) < 0:
                    truncated = True
                    break
                budget -= len(text)
                unit_id = str(block.get("id") or "")
                # 页级产物（转录/翻译）的**行**没有 block_id：它的身份是行 id（阅读器正文的
                # `data-row-id`），`block_get` 走"按行展开"那条路；解析块才有 block_id。
                is_row = block.get("source") == "transcript"
                cit = ctx.deps.registry.assign(
                    key=f"unit:{unit_id}", kind="block", page=page_no,
                    section=block.get("section") or None,
                    block_id=None if is_row else unit_id,
                    row_id=(block.get("row_id") or unit_id) if is_row else block.get("row_id"),
                    snippet=_clip(text), label=_locate(page_no, block.get("section"),
                                                       block.get("type") or ""),
                    ids=(unit_id,))
                entry = {"ref": cit.ref, "type": block.get("type"), "text": text}
                if block.get("translation"):
                    entry["translation"] = str(block["translation"]).strip()
                if block.get("formula"):
                    entry["formula"] = block["formula"]
                blocks.append(entry)
            if blocks:
                out_pages.append({"page": page_no, "blocks": blocks})
                ctx.deps.pages_read.append(page_no)
            if budget <= 0:
                truncated = True
                break
        if not out_pages:
            return {"ok": False,
                    "error": f"第 {pages} 页没有可读正文（可能还没解析，或页码超出范围）。"}
        return {"page_total": total or None, "truncated": truncated, "pages": out_pages}

    return await _guarded(ctx, "page_markdown_get", {"pages": _clip(pages, 40)}, _impl())


#: 一个 chunk 最多拼几行。段落型 chunk 通常 1~3 行；上限是防"一次展开 30 行"把
#: 上下文与后端往返都撑爆（超出的那部分让 page_markdown_get 去读）。
SPAN_ROWS_MAX = 8

#: 页级**行 id**（`p017-02`）与公式锚点的 `row-` 前缀形态。与 `project.as_row_id` 同一条
#: 规则，**故意各写一份**：agent 进程不能 import 主后端的服务层（见 `agent/config.py` 顶部）。
_ROW_ID_RE = re.compile(r"^(?:row-)?p(?P<page>\d{3,})-(?P<idx>\d{2})$")


def _row_id_of(raw: object) -> str:
    """`p017-02` / `row-p017-02` → `p017-02`；不像行 id 就回空串（= 那是真的块 id）。"""
    text = str(raw or "").strip()
    match = _ROW_ID_RE.match(text)
    return f"p{match.group('page')}-{match.group('idx')}" if match else ""


def _row_page(row_id: str) -> int:
    match = _ROW_ID_RE.match(str(row_id or ""))
    return int(match.group("page")) if match else 0


async def block_get(ctx: RunContext[Deps], ref: int | None = None,
                    refs: list[int] | None = None) -> dict:
    """按 ref 读块/段的完整原文（含译文与公式 LaTeX）。**一次可以给好几个 ref。**

    什么时候用：读页结果里某段被截断、或某条检索片段看起来关键，想把这一段完整看清。

    **批量是默认姿势**：检索 / 读页拿到的 ref 常有好几个，别一个 ref 一次调 ——
    一次给一批：

        block_get(refs=[3, 5, 9])

    回执按**你给的顺序逐条对应**（每条带自己的 `ref`）；某一条读不到只在那一条上给
    `ok=false` + 原因，别的照常返回 —— 照着失败的那几条补读，**不要整批重来**。
    一次最多 12 个；超了会截断，回执里写明「还有 N 条没处理」。

    参数：
      ref  —— 单个引用编号（老用法，照旧）。
      refs —— 一批引用编号。两种一起给时按「ref 在前、refs 在后」合并去重。
    """
    async def _read_rows(cit, row_id: str) -> dict | None:
        """按行读这一段（一个 chunk 可能跨好几行 → 拼成整段，而不是只给第一行）。"""
        span = ctx.deps.row_spans.get(row_id) or [row_id]
        texts: list[str] = []
        zh: list[str] = []
        page = cit.page
        kind = None
        for rid in span[:SPAN_ROWS_MAX]:
            page_no = _row_page(rid) or cit.page
            if not page_no:
                continue
            data = await ctx.deps.host.row(ctx.deps.paper_id, int(page_no), rid)
            if not data:
                continue
            page = data.get("page") or page
            kind = data.get("type") or kind
            text = str(data.get("text") or "").strip()
            if text:
                texts.append(text)
            translation = str(data.get("translation") or "").strip()
            if translation:
                zh.append(translation)
        if not texts and not zh:
            return None
        return {"ref": cit.ref, "page": page, "type": kind,
                "text": "\n\n".join(texts),
                "translation": "\n\n".join(zh) or None}

    def _row_fail(cit) -> dict:
        """行路径也读不到。**不报内部 id**（模型拿它没用，还会学着复述给用户）。"""
        where = f"第 {int(cit.page)} 页" if cit.page else "那一页"
        return {"ok": False,
                "error": f"这一条读不到（可能随重新转录变了号）。"
                         f"用 page_markdown_get 读{where}，或重新检索一次。"}

    async def _one(one_ref: int) -> dict:
        """读一条（单条与批量共用同一条路径，省得两份逻辑慢慢走偏）。"""
        cit = ctx.deps.registry.by_ref(one_ref)
        if cit is None:
            return {"ok": False,
                    "error": f"ref={one_ref} 不存在。只能用工具**实际返回过**的编号。"}
        if cit.row_id:
            # 行优先：写侧把行 id 也塞进了 block_id（见 project.anchor_ids），
            # 拿它去查块表必然 404，所以有行身份就直接走行走廊。
            got = await _read_rows(cit, cit.row_id)
            if got:
                return got
            return _row_fail(cit)
        if not cit.block_id:
            where = f"第 {int(cit.page)} 页" if cit.page else "它所在的那一页"
            return {"ok": False,
                    "error": f"ref={one_ref} 这一条没有可展开的正文（只有定位信息）："
                             f"用 page_markdown_get 读{where}。"}
        try:
            data = await ctx.deps.host.block(ctx.deps.paper_id, cit.block_id)
        except HostError as exc:
            # 兜底：这个 "block_id" 其实是行 id（存量索引 96.98% 都这样）→ 按行再试一次
            row_id = _row_id_of(cit.block_id)
            if row_id and (exc.status == 404
                           or str(exc.code or "").upper() in ("NOT_FOUND", "404")):
                got = await _read_rows(cit, row_id)
                if got:
                    got["note"] = "这一条是页级正文行，已按行展开。"
                    return got
                return _row_fail(cit)
            raise
        return {
            "ref": cit.ref, "page": data.get("page"), "section": data.get("section"),
            "type": data.get("type"),
            "text": str(data.get("text") or "").strip(),
            "translation": str(data.get("translation") or "").strip() or None,
            "formula": (data.get("formula") or {}).get("latex") or None,
        }

    async def _impl() -> dict:
        wanted, repeats = _int_list(ref, refs)
        if not wanted:
            return {"ok": False,
                    "error": "要给 ref 或 refs：refs=[3, 5, 9] 一次展开多个块。"}
        kept, left = _cap_batch(wanted, BATCH_REF_MAX)
        if refs is None and len(wanted) == 1:
            # 老用法（只给 ref）的回执结构一个字都不变；给了 refs 就一律走批量形态，
            # 免得模型按 `refs` 下注、拿到的却是另一种结构。
            return await _one(kept[0])
        rows: list[dict] = []
        for one in kept:
            try:
                got = await _one(one)
            except HostError as exc:
                got = {"ok": False, "error": f"取数失败：{exc.message}"}
            if got.get("ok") is False:
                # 失败只挂在这一条上：整批失败会让模型以为"这批 ref 全废了"
                rows.append({"ref": one, "ok": False,
                             "error": got.get("error") or "这一条读不到"})
            else:
                rows.append(got)
        failed = [row for row in rows if row.get("ok") is False]
        tips = [_left_note(left, BATCH_REF_MAX, "条")] if left else []
        if repeats:
            tips.append(f"重复的 {repeats} 条已合并。")
        if failed:
            tips.append("失败的那几条在 items 里带 ok=false 与原因，别的照常返回 —— "
                        "只补失败的那几条，不要整批重来。")
        out = {"requested": len(wanted), "returned": len(rows) - len(failed),
               "failed": len(failed), "unprocessed": left, "items": rows}
        if tips:
            out["note"] = " ".join(tips)
        if out["returned"]:
            out["hint"] = ("这些是逐条的完整原文；某条上下文还不够时，再用 "
                           "page_markdown_get 读它所在的页。")
        return out

    return await _guarded(ctx, "block_get", {"ref": ref, "refs": list(refs or [])}, _impl())


# --------------------------------------------------------------------------
# ③ 看版面：公式/表格/插图这类 OCR 不可靠的东西
# --------------------------------------------------------------------------


async def page_image_get(ctx: RunContext[Deps], pages: str, dpi: int = 200):
    """把整页渲染成图片交给视觉模型看（字面意思：**你自己看这一页**）。

    什么时候用：公式的上下标/希腊字母认不准、表格结构看不懂、怀疑解析漏了内容、
    图文混排的版面说不清时。**不要**用它代替读正文 —— 正文用 page_markdown_get。

    参数：
      pages —— ``"5"`` / ``"3-4"`` / ``"4,7"``。一次最多 3 页。
      dpi   —— 默认 200（阅读器页图是 150，公式密集的页建议 300~400）。
               注意：**不要**靠放大低 dpi 图来"看得更清"，放大只是插值，信息量没变。
    """
    async def _impl():
        wanted, clipped = parse_pages(pages, total=ctx.deps.page_total,
                                      limit=ctx.deps.cfg.image_max_pages)
        if not wanted:
            return {"ok": False, "error": pages_error(pages, total=ctx.deps.page_total)}
        use_dpi = max(150, min(int(dpi or 200), ctx.deps.cfg.image_max_dpi))
        content: list = []
        got: list[int] = []
        for page_no in wanted:
            raw, media = await ctx.deps.host.page_image(ctx.deps.paper_id, page_no, use_dpi)
            if not raw:
                continue
            ctx.deps.registry.assign(
                key=f"page:{page_no}", kind="page", page=page_no,
                snippet=_clip(f"第 {page_no} 页页面图（{use_dpi}dpi）"),
                label=_locate(page_no, None, "page"))
            content.append(BinaryContent(data=raw, media_type=media or "image/png"))
            got.append(page_no)
            ctx.deps.pages_read.append(page_no)
        if not got:
            return {"ok": False, "error": f"取不到第 {pages} 页的页面图。"}
        note = (f"已附上第 {'、'.join(map(str, got))} 页的页面图（{use_dpi}dpi）。"
                + ("页码超出范围，已截断。" if clipped else ""))
        return ToolReturn(return_value=note, content=[note, *content])

    return await _guarded(ctx, "page_image_get",
                          {"pages": _clip(pages, 40), "dpi": dpi}, _impl())


async def figure_list(ctx: RunContext[Deps], page: int | None = None,
                      pages: list[int] | None = None) -> dict:
    """列出某一页（或**一次列好几页**）的插图与表格（编号、图注、可嵌入的受控图片地址）。

    什么时候用：要回答"图 3 画了什么 / 表 1 的数字是多少"之前，先看这一页有哪些图形区域。
    **表格也在这里**：解析出的表格正文经常把单元格粘成一串（`1645.2945.16`），
    拿数字要用 kind="table" 那条的 index 去 figure_image_get 看表图。

    **批量**：不确定图在哪一页时别一页一次试 —— 一次给几页：`figure_list(pages=[4, 7, 9])`。
    回执按页分组（`pages: [{page, returned, items}]`），顺序与你给的页码一致；
    这一页没有图形区域是正常情况（`returned=0`），不报错、不影响别的页。一次最多 6 页。

    参数：
      page  —— 单页页码（老用法，照旧）。
      pages —— 一批页码。两种一起给时按「page 在前、pages 在后」合并去重。
    """
    async def _one(one_page: int) -> dict:
        data = await ctx.deps.host.figures(ctx.deps.paper_id, int(one_page))
        rows = _caption_rows(ctx.deps.paper_id, int(one_page))
        items = []
        for fig in data.get("items") or []:
            url = fig.get("url") or ""
            kind = str(fig.get("kind") or "figure")
            # 算法块也算"可看的内容区"（2026-12-07）：标签别写成"图 1"，那是伪代码
            label = ({"table": "表 ", "algorithm": "算法 "}.get(kind, "图 ")
                     + str(fig.get("number") or "?"))
            cit = ctx.deps.registry.assign(
                key=f"figure:{one_page}:{fig.get('index')}", kind=kind, page=int(one_page),
                snippet=_clip(fig.get("caption") or "", CAPTION_CHARS),
                # 图注行的行 id：前端点 `[n]` 才能闪到那一行（模型侧看不到这个 id）
                row_id=rows.get(str(fig.get("number") or "")),
                image_urls=[url] if url else [],
                label=f"第 {one_page} 页 · {label}")
            items.append({
                "ref": cit.ref, "index": fig.get("index"), "kind": kind,
                "number": fig.get("number") or "",
                "caption": _clip(fig.get("caption") or "", CAPTION_CHARS),
                "image_url": url,
                #: 图注所在的**正文行 id**：直接当 `anchor` 传给 highlight_manage /
                #: note_manage，高亮才画在阅读视图里（不用拿 `md:pN` 去猜，猜空了就标不上）。
                "anchor": rows.get(str(fig.get("number") or "")) or "",
            })
        out = {"page": int(one_page), "returned": len(items), "items": items}
        if not items:
            out["hint"] = "这一页没有插图或表格（或图注没被识别）。可以换一页，或直接用 page_image_get 看版面。"
        else:
            out["hint"] = ("看内容用 figure_image_get(items=[{page, index}, …]) —— 一次可以看几个区域；"
                           "要在回答里配图，就把 image_url **逐字**写进 ![简短说明](image_url)，"
                           "不要改写、拼接或换成别的地址。"
                           "图注要标高亮/写笔记时，把它的 anchor 原样传给 highlight_manage。")
        return out

    async def _impl() -> dict:
        wanted, repeats = _int_list(page, pages)
        if not wanted:
            return {"ok": False,
                    "error": "要给 page 或 pages：pages=[4, 7, 9] 一次看几页的图形区域。"}
        kept, left = _cap_batch(wanted, BATCH_PAGE_MAX)
        if pages is None and len(wanted) == 1:
            return await _one(kept[0])      # 老用法：单页回执结构一个字都不变
        groups: list[dict] = []
        for one in kept:
            try:
                groups.append(await _one(one))
            except HostError as exc:
                groups.append({"page": one, "returned": 0, "items": [],
                               "ok": False, "error": f"取数失败：{exc.message}"})
        failed = [g for g in groups if g.get("ok") is False]
        tips = [_left_note(left, BATCH_PAGE_MAX, "页")] if left else []
        if repeats:
            tips.append(f"重复的 {repeats} 页已合并。")
        if failed:
            tips.append("取数失败的那几页单独带 ok=false 与原因，别的页照常给出。")
        out = {"requested": len(wanted), "pages": groups,
               "returned": sum(int(g.get("returned") or 0) for g in groups),
               "failed": len(failed), "unprocessed": left}
        if tips:
            out["note"] = " ".join(tips)
        return out

    return await _guarded(ctx, "figure_list", {"page": page, "pages": list(pages or [])}, _impl())


async def figure_image_get(ctx: RunContext[Deps], page: int | None = None,
                            index: int | None = None,
                            items: list[dict] | None = None):
    """取一页里某个图形区域的裁图（插图或**表格**，按图注锚定 600dpi）交给视觉模型看。

    什么时候用：回答涉及图里的具体细节（曲线趋势、结构图）或**表格里的数字**时。
    表格的数字**不要**从 page_markdown_get 的正文里抄 —— 单元格常常粘在一起。

    **一次可以看几个区域**（用户口径：能批量就别一次一条）：

        figure_image_get(items=[{"page": 4, "index": 1}, {"page": 7, "index": 2}])

    为什么是 `items=[{page, index}, …]` 而不是 `pages=[…] + indices=[…]` 两个平行数组：
    平行数组一旦长度不等就没有确定语义（3 页配 2 个序号，谁对谁？），而且每条的失败/成功
    没法单独挂。成对写进去，每一条自带 page + index，回执也能**按条对应**给结论。
    回执里逐条列出「第 N 页 · 图 3（ref=…）」，取不到的那条单独给原因，别的照常附图。
    一次最多 6 个区域；超了会截断，并在回执里写明「还有 N 条没处理」。

    参数：
      page  —— 页码（单条老用法）。
      index —— ``figure_list`` 返回的 index（**不是**图号/表号）。
      items —— 一批区域：``[{"page": 4, "index": 1}, …]``。与 page/index 一起给时合并去重。
    """
    async def _one(one_page: int, one_index: int) -> dict:
        raw, media = await ctx.deps.host.figure_image(ctx.deps.paper_id, int(one_page),
                                                      int(one_index))
        if not raw:
            return {"page": one_page, "index": one_index, "ok": False,
                    "error": "取不到这个区域的裁图。"}
        kind, number = "figure", ""
        try:                                   # 顺手取编号，让 ref 的标题写成"表 1"而不是"插图 #0"
            data = await ctx.deps.host.figures(ctx.deps.paper_id, int(one_page))
            hit = next((i for i in (data.get("items") or [])
                        # ⚠️ 别写 `i.get("index") or -1`：序号 0 是 falsy，会被吃成 -1，
                        # 于是第 0 个区域永远匹配不上（踩过：表 1 的 ref 标题退回"图 #0"）
                        if i.get("index") is not None
                        and int(i["index"]) == int(one_index)), None)
            if hit:
                kind = str(hit.get("kind") or "figure")
                number = str(hit.get("number") or "")
        except Exception as exc:  # noqa: BLE001 —— 拿不到编号不影响看图
            log.debug("取区域编号失败（不影响看图）：%s", exc)
        what = ("表 " if kind == "table" else "图 ") + (number or f"#{one_index}")
        cit = ctx.deps.registry.assign(key=f"figure_img:{one_page}:{one_index}", kind=kind,
                                       page=int(one_page),
                                       # 图注行的行 id：前端点 `[n]` 闪到那一行
                                       row_id=_caption_rows(ctx.deps.paper_id,
                                                           int(one_page)).get(number),
                                       snippet=_clip(f"第 {one_page} 页 · {what}"),
                                       label=f"第 {one_page} 页 · {what}")
        return {"page": one_page, "index": one_index, "ok": True, "ref": cit.ref,
                "what": what, "raw": raw, "media": media or "image/png"}

    def _asked() -> list[tuple[int, int]]:
        """page/index 与 items 合并去重：(page, index) 成对，顺序就是回执顺序。"""
        pairs: list[tuple[int, int]] = []
        if page is not None and index is not None:
            pairs.append((int(page), int(index)))
        for one in (items or []):
            if not isinstance(one, dict):
                continue
            try:
                pair = (int(one.get("page")), int(one.get("index")))
            except (TypeError, ValueError):
                continue
            if pair not in pairs:
                pairs.append(pair)
        return pairs

    async def _impl():
        wanted = _asked()
        if not wanted:
            return {"ok": False,
                    "error": "要给 page + index，或 items=[{page, index}, …] 一次看几个区域。"}
        kept, left = _cap_batch(wanted, BATCH_FIGURE_MAX)
        if items is None and len(wanted) == 1:
            one_page, one_index = kept[0]
            got = await _one(one_page, one_index)
            if got.get("ok") is not True:
                return {"ok": False, "error": got.get("error") or "取不到这个区域的裁图。"}
            note = f"已附上第 {one_page} 页的{got['what']}（ref={got['ref']}）。"
            return ToolReturn(return_value=note,
                              content=[note,
                                       BinaryContent(data=got["raw"],
                                                     media_type=got["media"])])
        lines: list[str] = []
        content: list = []
        done = failed = 0
        for i, (one_page, one_index) in enumerate(kept, start=1):
            try:
                got = await _one(one_page, one_index)
            except HostError as exc:
                got = {"page": one_page, "index": one_index, "ok": False,
                       "error": f"取数失败：{exc.message}"}
            if got.get("ok") is True:
                done += 1
                lines.append(f"{i}. 第 {one_page} 页 · {got['what']}（ref={got['ref']}）")
                content.append(BinaryContent(data=got["raw"], media_type=got["media"]))
            else:
                failed += 1
                lines.append(f"{i}. 第 {one_page} 页 · 区域 #{one_index} —— 没取到："
                             f"{got.get('error') or '取不到这个区域的裁图。'}")
        head = f"已附上 {done} 个区域的裁图（序号与你给的 items 顺序一一对应）"
        if left:
            head = f"{head} {_left_note(left, BATCH_FIGURE_MAX, '条')}"
        if failed:
            head = (f"{head} 有 {failed} 条没取到（原因见那一条），别的照常附上 —— "
                    f"只补失败的那几条。")
        note = "\n".join([head, *lines])
        return ToolReturn(return_value=note, content=[note, *content])

    return await _guarded(ctx, "figure_image_get",
                          {"page": page, "index": index, "items": list(items or [])},
                          _impl())


# --------------------------------------------------------------------------
# ④ P1：跨库 / 公式 / 译文 / 书架 / 笔记
# --------------------------------------------------------------------------


async def kb_search(ctx: RunContext[Deps], query: str = "", top_k: int = 8,
                    queries: list[str] | None = None) -> dict:
    """在整个知识库里检索（**不限当前论文**），返回候选位置与它属于哪篇论文。

    什么时候用：用户问"我库里还有谁讲过这个 / 有没有别的论文提到 X"，
    或者当前这篇论文里找不到依据、想看看别的论文有没有时。

    **不要**用它回答"这篇论文怎么说的"——那要用 paper_search，只查当前论文。
    拿到结果后如果要读内容，用 `paper_id` + `page` 判断；当前这一篇可以直接用
    page_markdown_get，别的论文只能靠返回的片段说明，不要假装读过。

    **一次可以查几个词**：`kb_search(queries=["random rotation", "QJL"])`。
    回执按 query 分组（`groups`），顺序与你给的词一致；某一条失败只挂在那一条上。
    一次最多 8 个词；超了会截断并写明「还有 N 条没处理」。

    参数：
      query   —— 单个检索词。
      queries —— 一批检索词。与 query 一起给时按「query 在前、queries 在后」合并去重。
      top_k   —— 每个词最多几条，默认 8，上限 12。
    """
    async def _one(q: str) -> dict:
        k = max(1, min(int(top_k or 8), SEARCH_MAX))
        cached = _cached(ctx, "kb_search", q, k)
        if cached is not None:
            return cached
        ctx.deps.count_search()
        before = len(ctx.deps.registry)
        data = await ctx.deps.host.kb_search(q, top_k=k)
        items = []
        for raw in list(data.get("items") or [])[:ctx.deps.cfg.evidence_limit]:
            paper_id = str(raw.get("paper_id") or "")
            title = str(raw.get("paper_title") or "") or paper_id
            # 页码与 paper_search 同一个入口（行 id 优先），跨库命中的页才不会自成一套口径
            page = _hit_page(raw)
            section = raw.get("section")
            snippet = _clip(raw.get("translation") or raw.get("content") or "")
            # 名字空间同 `_register_chunk`：行 id 不能进 block_id，去重键回落到行 id
            kb_rows = [str(rid) for rid in (raw.get("row_ids") or []) if str(rid).strip()]
            kb_row = str(raw.get("row_id") or "") or (kb_rows[0] if kb_rows else "")
            kb_block = None if _row_id_of(raw.get("block_id")) else raw.get("block_id")
            if kb_row and len(kb_rows) > 1:
                ctx.deps.row_spans[kb_row] = kb_rows
            cit = ctx.deps.registry.assign(
                key=_chunk_key(paper_id, kb_block or kb_row,
                               str(raw.get("chunk_id") or ""), snippet), kind="chunk",
                paper_id=paper_id, page=page, section=section,
                block_id=kb_block, row_id=kb_row or None, snippet=snippet,
                label=" · ".join(x for x in (title, _locate(page, section)) if x),
                ids=(str(raw.get("chunk_id") or ""),),
                # 跨库命中同样带自己那条 chunk 的 pids（拿不到就是空，to_dict 里不出现）
                pids=list(raw.get("pids") or []))
            items.append({
                "ref": cit.ref, "paper_id": paper_id, "paper_title": title,
                "page": page, "section": section, "type": raw.get("chunk_type"),
                "is_current_paper": paper_id == ctx.deps.paper_id,
                "snippet": _snippet_around(raw.get("content") or "", str(q or "")),
            })
        out = {"query": q, "returned": len(items), "items": items}
        if not items:
            out["hint"] = "整个知识库里都没有检索到相关内容。"
        elif len(ctx.deps.registry) == before:
            out["hint"] = _stale_hint()
        elif any(not i["is_current_paper"] for i in items):
            out["hint"] = ("这些是**别的论文**里的片段，只能作为线索。要下结论就得说清是哪篇论文；"
                           "只有 is_current_paper=true 的条目能用 page_markdown_get 展开。")
        else:
            out["hint"] = "命中都来自当前论文，可以直接 page_markdown_get 展开。"
        _remember(ctx, "kb_search", q, k, out)
        return out

    async def _impl() -> dict:
        wanted, repeats = _text_list(query, queries)
        if not wanted:
            return {"ok": False,
                    "error": "要给 query 或 queries：queries=[\"词一\", \"词二\"] 一次查多个。"}
        kept, left = _cap_batch(wanted, BATCH_QUERY_MAX)
        if queries is None and len(wanted) == 1:
            return await _one(kept[0])      # 老用法：单个词的回执结构一个字都不变
        groups: list[dict] = []
        for q in kept:
            try:
                groups.append(await _one(q))
            except HostError as exc:
                groups.append({"query": q, "ok": False, "returned": 0, "items": [],
                               "error": f"取数失败：{exc.message}"})
        return _grouped_receipt(
            groups, wanted=len(wanted), left=left, repeats=repeats,
            cap=BATCH_QUERY_MAX,
            hint="每组查自己的词；跨库命中只能当线索，别假装读过那篇论文。")

    return await _guarded(ctx, "kb_search",
                          {"query": _clip(query, 60), "queries": list(queries or []),
                           "top_k": top_k}, _impl())


#: 向宿主一次要多少条公式（宿主 `limit` 的天花板是 500）。取 200 而不是宿主的默认 40：
#: 块级公式全篇通常 10~30 条，**一个窗口就够**，分页只在 `include_inline` 那种几千条的
#: 批次里才真正翻页 —— 上一版按 `missing_pages` 逐页补，一页一个请求，行内那批补不完。
_FORMULA_FETCH_LIMIT = 200

#: 最多取几个窗口（5 × 200 = 1000 条）：块级公式的论文永远够用，行内那几千条时才停，
#: 停下来的那批照旧由 `truncated` + `missing_pages` 说清「还差哪几页」。
_FORMULA_FETCH_WINDOWS = 5


def _host_missing_pages(data: dict) -> list[int]:
    """宿主回执里「窗口之后那几条落在哪几页」（没给就是空列表）。"""
    return [int(one) for one in (data.get("missing_pages") or []) if one]


def _formula_doc_key(item: dict) -> tuple[int, int, str]:
    """公式条的**文档顺序**：页 → 行 id 尾部序号（补页拼起来的列表要按它重排）。"""
    raw = str(item.get("row_id") or "")
    found = re.search(r"(\d{1,4})$", raw)
    return (int(item.get("page") or 0), int(found.group(1)) if found else 0, raw)


async def _formula_listing(ctx: RunContext[Deps], *, page: int | None, query: str | None,
                           include_inline: bool) -> tuple[list[dict], dict]:
    """整份公式列表 + 计数：按 offset 窗口把列表拿全，再按归一化 LaTeX 做跨页合并。

    分页要的是**整份列表**（`total` / `missing_pages` / `also_on` 都按全量算），所以这一步
    收成一个函数：**先用窗口拼出整份列表、再去重、最后才由 `formula_get` 切页**。

    宿主的老/新两种回执都在这里收口（见下方注释）：新宿主有 `offset`/`next_offset`/`has_more`，
    老宿主只有 `truncated` + `missing_pages`，后者退回「提示按页补」而不是抛异常。
    """
    data = await ctx.deps.host.formulas(ctx.deps.paper_id, page=page, query=query,
                                        include_inline=include_inline,
                                        offset=0, limit=_FORMULA_FETCH_LIMIT)
    raw_items = [dict(one) for one in (data.get("items") or [])]
    if "next_offset" in data or "has_more" in data:
        # **新宿主**（`project.formulas` 有真 offset 分页）：顺着 next_offset 把整份列表
        # 一次拿回来 —— 一次调用就够，不让模型自己按页试。窗口数用尽（行内那几千条）时
        # 就停在 1000 条，剩下的由最后一个窗口的 missing_pages 说清还差哪几页。
        trailing = data
        windows = 1
        while trailing.get("has_more") and windows < _FORMULA_FETCH_WINDOWS:
            next_offset = trailing.get("next_offset")
            if next_offset is None:
                break
            try:
                # `page` 必须跟着带上：否则 `page=22` 这种窄查询的第二个窗口会跑到全文去取
                trailing = await ctx.deps.host.formulas(
                    ctx.deps.paper_id, page=page, query=query,
                    include_inline=include_inline,
                    offset=int(next_offset), limit=_FORMULA_FETCH_LIMIT)
            except HostError as exc:   # 取不满不算失败：照 missing_pages 说清还差哪几页
                log.debug("取公式窗口 offset=%s 失败：%s", next_offset, exc.message)
                break
            windows += 1
            raw_items.extend(dict(one) for one in (trailing.get("items") or []))
        complete = not bool(trailing.get("has_more"))
        host_pages = [] if complete else _host_missing_pages(trailing)
    else:
        # **老宿主**（回执里没有 next_offset / has_more）：它没有 offset 可翻，只有自己的
        # truncated + missing_pages。这时**不抛异常、也不多发请求**，退回上一轮的做法：
        # 照它的 truncated 判完整度，剩下的靠 missing_pages 提示 `formula_get(page=…)` 补。
        complete = not bool(data.get("truncated"))
        host_pages = [] if complete else _host_missing_pages(data)
    merged_items, merges = _merge_formula_dups(raw_items)
    held_display = sum(1 for one in merged_items if one.get("display"))
    held_inline = len(merged_items) - held_display
    #: 宿主的三个计数**不随 include_inline 变**（说的是"有多少"，不是"这次给多少"），
    #: 所以拿它当基数、只减掉我们自己合并掉的 —— 不能直接数手里的条目：
    #: 默认（不要行内）时手里只有块级那批，数出来 inline 永远是 0。
    host_display = int(data.get("display_total") or 0)
    host_inline = int(data.get("inline_total") or 0)
    if not (host_display or host_inline):   # 老回执只有 total：按条数兜底，别当成 0 条
        host_display = len(raw_items)
    if complete:
        # 整份列表在手 → 块级条数就是合并后的条数（42 → 40 这种事在这里落地）
        merged_items.sort(key=_formula_doc_key)
        display_total = held_display
        inline_total = held_inline if include_inline else max(0, host_inline)
    else:
        # 窗口预算用尽 / 老宿主还压着一批没给到 → 用宿主的全量数，减掉我们合并掉的
        display_total = max(0, host_display - merges)
        inline_total = max(0, host_inline)
    total = display_total + inline_total
    omitted_inline = data.get("omitted_inline")
    if omitted_inline is None:
        # 上游没给（老回执 / 测试桩）时按「默认不列行内」兜底：否则回执一边说「另有 N 条」、
        # 一边计数是 0，两个数自相矛盾
        omitted_inline = 0 if include_inline else inline_total
    return merged_items, {
        "total": total, "display_total": display_total, "inline_total": inline_total,
        "omitted_inline": int(omitted_inline or 0), "complete": complete,
        "source": data.get("source"),
        # 宿主压根没给到的那几页（没收齐时才算进 missing_pages）：
        # 停在窗口预算上的那一批，用**最后一个窗口**的 missing_pages —— 它说的正是"还差哪几页"
        "host_pages": host_pages,
    }


async def formula_get(ctx: RunContext[Deps], page: int | None = None,
                      query: str | None = None,
                      include_inline: bool = False,
                      offset: int = 0,
                      limit: int = FORMULA_PAGE_DEFAULT) -> dict:
    r"""取本论文的**块级公式**（LaTeX + 页码），默认来自该页 Markdown 正文。

    什么时候用：用户问某个公式是什么意思、某个符号怎么定义、推导里的某一步；
    或者卷面里的公式被 OCR 认错、需要拿到 LaTeX 原文再核对。

    **默认给的是页级 Markdown 里的块级公式**（`$$…$$` 那批，按论文文档顺序排好，
    **完整、可引用**）。版面**解析卡只在这一页没有块级公式时兜底**，不是主体来源 ——
    别把它当成"默认就是解析卡"。返回里的 `source` 说明这份列表整体来自哪：
    `markdown`（页级正文）/ `parse`（解析卡兜底）/ `mixed`（两者都有）。

    `grade`（A/B/C）是**解析卡的字段**：转录层来的条目 `grade=None` 是正常的，
    不代表数据缺失，也不影响这条公式可用。

    参数：
      page  —— 只看某一页（不确定页码就先 paper_search）。
      query —— 关键词过滤。**公式没有可用的编号字段**：论文自己的 `(3)` 编号只出现在
               `context` 里（解析卡往往没有 context），所以按编号找请先
               `page_markdown_get` 定位那一页，再 `formula_get(page=…)`。
      include_inline —— 默认 False：只要块级公式。要看**行内符号**（`$b-1$` 这种）
               或伪代码里的碎片时才传 True —— 它会把行内公式也列出来，条目更多也更碎。
      offset —— 从第几条开始给（默认 0）；负数按 0 算。
      limit  —— 这一页给几条（默认 40，等于以前那个上限；超出 [1, 200] 会被夹回来）。

    **分页**：`total` 是未分页前的条目总数，`offset` / `limit` 是这次的口径，
    `next_offset` 是下一页的起点（没有下一页就是 None），`has_more` 说还有没有。
    **条目多时先看 `total` / `next_offset`，按它翻页，别一页页试。**

    回执里的 `display_total` / `inline_total` / `total` **同一口径**：都数「去重后、在筛选范围
    内的全部条目」（**不随 offset / limit 变**），`total == display_total + inline_total`
    恒成立；`returned` 是这一页给了几条，`omitted_inline` 是这次按需过滤掉的行内条数
    （默认就等于 `inline_total`）。
    `missing_pages` 按**整份列表**算（不是只算这一页）：它说「这次还没给到」的条目落在哪几页 ——
    还有下一页就先看 `next_offset`；`truncated` 为真而 `next_offset` 为空，才轮到
    `formula_get(page=…)` 按页补。

    **同一条公式在不同页重复陈述**（第 6 页与第 18 页各排了一遍的失真上界那种）只出现**一条**，
    其余页码在该条的 `also_on` 里。判等前会**归一化**：`\boldsymbol{y}` / `\mathbf{y}` / `\bm{y}`
    / `\left` / `\right` / 细间距 / 空白都剥掉、只留内容本体 —— 所以正文第 7 页的 `\|y\|_2^2`
    与附录第 19 页的 `\|\boldsymbol{y}\|_2^2` 是同一条。不同的式子绝不合并。

    ⚠️ `partial=True` 的条目**LaTeX 可能只是公式的一段**（一条公式被切成了几段，我们只拿到
    第一段）。要确认全文就用 `page_image_get(page)` 看那一页的原版面
    （阅读器里这个位置本来也有公式裁图）——**不要照着半条 LaTeX 讲整条公式**。
    """
    async def _impl() -> dict:
        items, meta = await _formula_listing(ctx, page=page, query=query,
                                             include_inline=include_inline)
        # 默认只给块级（完整、可引用）；行内那批按需取
        listed = [one for one in items if one.get("display") or include_inline]
        # 参数校验：offset < 0 当 0；limit 夹到 [1, 200]（`limit=0` 也夹成 1，不是"回默认"）
        start = 0 if offset is None else max(0, int(offset))
        size = FORMULA_PAGE_DEFAULT if limit is None else int(limit)
        size = min(max(size, 1), FORMULA_PAGE_MAX)
        window = listed[start:start + size]
        end = start + len(window)
        has_more = end < len(listed)
        # 「还差哪几页」按**整份列表**算（不是只算这一页）：这一页之后的条目 + 宿主没给到的页
        lost = {int(one.get("page") or 0) for one in listed[end:] if one.get("page")}
        page_items = []
        for raw in window:
            latex = str(raw.get("latex") or "")
            page_no = int(raw.get("page") or 0) or None
            cit = ctx.deps.registry.assign(
                key=f"formula:{page_no}:{raw.get('row_id')}:{_latex_key(latex)}", kind="formula",
                page=page_no, row_id=raw.get("row_id"), snippet=_clip(latex, 160),
                label=_locate(page_no, None, "formula"),
                ids=(f"f{page_no:03d}",) if page_no else ())
            page_items.append({
                "ref": cit.ref, "page": page_no, "latex": latex,
                "display": bool(raw.get("display")),
                "grade": raw.get("grade"),
                # LaTeX 只是公式的一段 → 要讲全貌就去看原版面
                "partial": bool(raw.get("partial")),
                "source": raw.get("source"),
                # 同一条公式在**别的页**也排了一遍时才有内容（跨页重复陈述被合并掉的那些）
                "also_on": [int(p) for p in (raw.get("also_on") or [])],
                "context": raw.get("context") or "",
                "context_zh": raw.get("context_zh") or "",
            })
        out = {"total": meta["total"], "returned": len(page_items),
               # 分页口径：条目多时先看 total / next_offset，别一页页试
               "offset": start, "limit": size,
               "next_offset": end if has_more else None, "has_more": has_more,
               "source": meta["source"],
               # 没给完：还有下一页，或者宿主那里还压着一批拿不到的
               "truncated": has_more or not meta["complete"],
               # 三个计数同一口径：都数去重后的**全部**条目，不随 offset / limit 变
               "display_total": meta["display_total"], "inline_total": meta["inline_total"],
               "omitted_inline": meta["omitted_inline"],
               # 「还差哪几页」：按整份列表算，没有下一页也没有宿主遗留时就是空列表
               "missing_pages": sorted(lost | set(meta["host_pages"])),
               "items": page_items}
        if not page_items:
            if listed:   # 有公式、只是 offset 翻过头了：说清总数，别让模型以为"这篇没有公式"
                out["hint"] = (f"这一页是空的：offset={start} 已经越过 {len(listed)} 条，"
                               f"总条目见 total={meta['total']}（`limit` 最大 {FORMULA_PAGE_MAX}）。")
            else:
                out["hint"] = ("这篇论文里没有提取到符合条件的公式（可能整篇没有公式，"
                               "或筛选太窄）。行内符号可以先用 page_markdown_get 看那一页的正文。")
        else:
            # 几条提示可以**同时成立**（默认只给块级 + 来源含解析兜底 + 全是转录层没有 grade
            # + 有条目只是片段 + 还有下一页 / 宿主没给完），拼在一起给，别用 elif 让后面的吞掉
            hints: list[str] = []
            if not include_inline and out["omitted_inline"] > 0:
                hints.append(f"这些是**块级公式**（完整，可引用）；另有 "
                             f"{out['omitted_inline']} 条行内公式（单符号 / 碎片那种）。"
                             f"查符号定义、单点表达式时用 `include_inline=True`，"
                             f"或者直接 `page_markdown_get` 读那一页看上下文。")
            if include_inline and any(one["display"] for one in page_items):
                # 用户实测的困惑：传了 include_inline 以后完整那几条被几十条行内碎片包住，
                # 看起来"块级公式跑到列表末尾去了"。它们确实是文档顺序，只是前面全是碎片。
                hints.append("完整那几条就在这批里（`display: true`），按文档顺序夹在行内碎片"
                             "中间 —— 只想看它们就别传 `include_inline`。")
            if out.get("source") != "markdown":
                hints.append("本次列表含**版面解析兜底**的条目（source=parse/mixed）——"
                             "那几条的 LaTeX 可能只是公式的一段，讲全貌前先用 "
                             "`page_image_get(page)` 核对原版面。")
            if all(one["grade"] is None for one in page_items):
                hints.append("这些公式来自**转录层**，没有 A/B/C 分级（`grade` 是解析卡的字段，"
                             "不是缺数据）。")
            if any(one["partial"] for one in page_items):
                hints.append("有条目 partial=true：那几条的 LaTeX 只是公式的一段 —— "
                             "要对着讲就把 `page_image_get(page)` 的原版面拿来核对。")
            if has_more:
                hints.append(f"全文共 {out['total']} 条，这次给的是第 {start + 1}–{end} 条"
                             f"（按文档顺序）；下一页用 `offset={end}`（`limit` 最大 "
                             f"{FORMULA_PAGE_MAX}）—— 先看 total / next_offset，别一页页试。")
            if not meta["complete"]:
                pages = "、".join(str(one) for one in out["missing_pages"])
                tail = (f" 还差这几页：{pages} —— 照它们 `formula_get(page=…)` 补，"
                        f"不要凭条数猜。" if pages else "")
                hints.append(f"全文共 {out['total']} 条，这次从宿主拿到 {len(listed)} 条"
                             f"（按文档顺序）。{tail}")
            if hints:
                out["hint"] = " ".join(hints)
        return out

    return await _guarded(ctx, "formula_get",
                          {"page": page, "query": _clip(query or "", 40),
                           "include_inline": include_inline,
                           "offset": offset, "limit": limit}, _impl())


async def translate_get(ctx: RunContext[Deps], pages: str) -> dict:
    """取某一页（或几页）的**中文译文**（按块）。

    什么时候用：用户问"这段中文怎么说的 / 译文对不对"，或者你要用中文复述原文、
    想确认某个术语在译文里的统一译法时。判断"译文是否忠实"时也要看它。

    参数：
      pages —— ``"5"`` / ``"3-8"`` / ``"4,7"`` / ``"all"``，一次最多 20 页。
    """
    async def _impl() -> dict:
        wanted, clipped = parse_pages(pages, total=ctx.deps.page_total,
                                      limit=TEXT_PAGES_MAX)
        if not wanted:
            return {"ok": False, "error": pages_error(pages, total=ctx.deps.page_total)}
        data = await ctx.deps.host.pages(ctx.deps.paper_id, ",".join(map(str, wanted)),
                                         lang="zh")
        ctx.deps.page_total = ctx.deps.page_total or int(data.get("page_total") or 0)
        out_pages: list[dict] = []
        missing = 0
        for page in data.get("pages") or []:
            page_no = int(page.get("page") or 0)
            blocks = []
            for block in page.get("blocks") or []:
                zh = str(block.get("translation") or "").strip()
                if not zh:
                    missing += 1
                    continue
                cit = ctx.deps.registry.assign(
                    key=f"block:{block.get('id')}", kind="block", page=page_no,
                    section=block.get("section") or None, block_id=block.get("id"),
                    row_id=block.get("row_id"), snippet=_clip(zh),
                    label=_locate(page_no, block.get("section"), "译文"),
                    ids=(str(block.get("id") or ""),))
                blocks.append({"ref": cit.ref, "translation": zh})
            if blocks:
                out_pages.append({"page": page_no, "blocks": blocks})
                # 读过这一页的译文也算"读过这一页"：`page_markdown_edit` 的先读后写判据看它
                ctx.deps.pages_read.append(page_no)
        if not out_pages:
            return {"ok": False, "truncated": clipped,
                    "error": f"第 {pages} 页还没有译文（这篇论文可能没翻译，或该页未翻译）。"}
        return {"page_total": ctx.deps.page_total or None, "truncated": clipped,
                "pages": out_pages,
                "note": f"另有 {missing} 个块没有译文（未翻译），已跳过。" if missing else ""}

    return await _guarded(ctx, "translate_get", {"pages": _clip(pages, 40)}, _impl())


async def reader_state(ctx: RunContext[Deps]) -> dict:
    r"""读**读者此刻在阅读器里的状态**：读到第几页、看的是哪一面（原文 / 译文 / 对照）、
    什么视图、左栏停在哪个页签。

    什么时候用：要给"这一段 / 这一页"加高亮或笔记、要判断该标**原文面**还是**译文面**、
    要引用"读者正在看的那一页"时 —— 先读一次，再决定 `highlight_manage` / `note_manage`
    的 `side` 与页码。它**只读**，不改阅读器的任何状态（不翻页、不换视图、不切页签）。

    `available=false` = 这次请求**没带**阅读状态（老前端 / 非阅读器入口）：
    那时所有字段都是空值，**绝不要猜**当前页 —— 用文字语言判断该标哪一面（含中文 → 译文面），
    或者直接问用户"你现在看的是哪一面 / 哪一页"。

    `page_side="both"`（读者在对照视图）= 眼前同时有两侧：`note_side` 因此给 null，
    要标的那段文字属于哪一面就传哪一面，**不要**两面对半标、也不要一律标译文面。
    """
    async def _impl() -> dict:
        reader = ctx.deps.reader
        out: dict = {
            "available": reader.available,
            "page": reader.page or None,
            "page_side": reader.page_side or None,
            "order": reader.order or None,
            "view": reader.view or None,
            "side_tab": reader.side_tab or None,
            "visible_pages": list(reader.visible_pages) or None,
            "note_side": reader.note_side,
            "page_known": reader.page_known,
        }
        if not reader.available:
            out["hint"] = ("不知道读者在哪：这次请求没有带阅读状态。不要猜当前页或当前面 —— "
                           "需要位置就用 page_markdown_get 定位，或直接问用户"
                           "（\"你现在看的是第几页、原文还是译文？\"）。"
                           "标高亮时按文字语言判面（含中文 → zh），别编页码。")
            return out
        tips: list[str] = []
        if not reader.page_known:
            tips.append("页未知（读者没报或报的值认不出来）：不要引用「第 X 页」，"
                        "要位置就先问用户或 page_markdown_get 定位。")
        if reader.page_side == "both":
            tips.append("读者在**对照视图**：两侧都看得见，标哪一面取决于要标的那段文字"
                        "本身属于哪一面（原文 → en，译文 → zh）；两面对半标只会让用户"
                        "多删一半。")
        elif reader.note_side:
            tips.append(f"读者正在看 {reader.note_side} 面：要标注的就是这一面，"
                        f"直接传 side=\"{reader.note_side}\"（读者眼前只画得出这一面）。")
        else:
            tips.append("不知道读者在看哪一面：按文字语言判面（含中文 → zh），或问用户。")
        if reader.view:
            #: 这句以前写成「rows = 正文行，高亮才画得出来；markdown / pdf 面上的高亮在阅读视图里看不到」——
            #: 读者正看 pdf 时就成了自相矛盾（"当前视图 pdf" 紧接"pdf 面上的高亮看不到"）。改成
            #: **按锚点说清各自在哪看得到**，不提"阅读视图"这个会被当前视图顶掉的词。
            tips.append(f"当前视图：{reader.view}。高亮请锚在**正文行**上（rows 面哪个视图都画得出来）；"
                        "落 md:pN 的高亮只在 Markdown 视图可见，落 pdf:pN 的高亮只在原 PDF 页图视图可见。")
        out["hint"] = " ".join(tips)
        return out

    return await _guarded(ctx, "reader_state", {}, _impl())


async def paper_archive_get(ctx: RunContext[Deps]) -> dict:
    """读**这篇论文的档案**：标题、篇幅、领域与核心方法、主要论断、统一译法、章节结构。

    什么时候用：需要"这篇论文大概在讲什么 / 有哪几章"这种**背景**时（例如用户问全篇性的问题、
    或你想先知道结构再决定读哪几页）。**不要**每个问题都先调它一次 —— 本次会话里看过就够。

    ⚠️ 档案**不是可引用的材料**：返回里没有编号，它只帮你决定"该去读哪里"。
    写进回答的判断必须再用 paper_search / page_markdown_get / block_get 拿到**有编号**的正文。
    """
    async def _impl() -> dict:
        data = await ctx.deps.host.archive(ctx.deps.paper_id)
        title = str(data.get("title") or ctx.deps.title or "")
        page_total = int(data.get("page_total") or ctx.deps.page_total or 0) or 0
        # 页数顺带记进 deps：后面的 page_markdown_get("all") 之类要用
        ctx.deps.page_total = ctx.deps.page_total or page_total
        ctx.deps.title = ctx.deps.title or title
        text = prompts.archive_block(int(ctx.deps.cfg.archive_chars or 6000),
                                     data.get("brief") or {},
                                     data.get("outline") or [],
                                     title=title, page_total=page_total)
        return {
            "title": title, "page_total": page_total or None, "archive": text,
            "note": "档案只是背景（没有编号、不能引用）：要写进回答的判断，"
                    "请用 paper_search / page_markdown_get 拿有编号的正文。",
        }

    return await _guarded(ctx, "paper_archive_get", {}, _impl())


async def library_list(ctx: RunContext[Deps], query: str | None = None,
                       limit: int = 0) -> dict:
    """列出书架上的论文（按标题 / 作者模糊匹配），返回元数据。

    什么时候用：用户问"我库里有哪些关于 X 的论文 / 有没有讲过 Y 的文章"。
    这是**找论文**，不是找内容；挑出候选之后用 kb_search 去找具体段落。

    **条数是有上限的**（默认 30 条、最多 50 条）：书架大起来一次列全就会把上下文吃光。
    被砍掉的条数写在回执的 `unprocessed` 与 `note` 里（「还有 N 篇没列」），
    不是静默丢 —— 要更多就换关键词缩小范围，或显式调大 `limit`。

    参数：
      query —— 关键词（标题或作者）。不传就按最近阅读顺序列。
      limit —— 最多列几篇（0 = 默认 30，上限 50）。
    """
    async def _impl() -> dict:
        ctx.deps.count_search()
        data = await ctx.deps.host.library(q=query)
        raw_items = list(data.get("items") or [])
        total = int(data.get("total") or 0) or len(raw_items)
        cap = LIBRARY_LIST_DEFAULT if not limit else max(1, min(int(limit), LIBRARY_LIST_MAX))
        cap = min(cap, LIBRARY_LIST_MAX)
        shown, left = _cap_batch(raw_items, cap)
        # 服务端自己也封顶 30 条：它砍掉的那些同样要算进「没列出来的条数」
        left = max(left, total - len(shown))
        items = []
        for raw in shown:
            paper_id = str(raw.get("paper_id") or "")
            meta = "，".join(x for x in (
                str(raw.get("authors") or ""),
                f"{raw.get('venue')} {raw.get('year')}".strip(),
                f"{raw.get('page_count')} 页" if raw.get("page_count") else "",
            ) if x)
            cit = ctx.deps.registry.assign(
                key=f"library:{paper_id}", kind="paper", paper_id=paper_id,
                snippet=_clip(f"{raw.get('title')}（{meta}）", 200) if meta
                        else _clip(str(raw.get("title") or "")),
                label=str(raw.get("title") or paper_id))
            items.append({
                "ref": cit.ref, "paper_id": paper_id, "title": raw.get("title") or "",
                "authors": raw.get("authors") or "", "venue": raw.get("venue") or "",
                "year": raw.get("year") or 0, "status": raw.get("status") or "",
                "page_count": raw.get("page_count") or 0,
                "tags": list(raw.get("tags") or []),
                "is_current_paper": paper_id == ctx.deps.paper_id,
            })
        out = {"total": total or None, "returned": len(items), "limit": cap,
               "unprocessed": left, "items": items}
        if left:
            out["note"] = (f"还有 {left} 篇没列（一次最多 {cap} 篇）：换更窄的关键词，或者显式调大 limit（上限 50）。")
        if not items:
            out["hint"] = "书架上没有匹配的论文。可以换个关键词，或不带关键词看看全部。"
        return out

    return await _guarded(ctx, "library_list",
                          {"query": _clip(query or "", 40), "limit": limit}, _impl())


async def notes_search(ctx: RunContext[Deps], query: str | None = None,
                       scope: str = "paper", kind: str = "all",
                       queries: list[str] | None = None) -> dict:
    """检索用户自己的笔记 / 高亮 / 批注（第一人称证据）。

    什么时候用：用户问"我之前记过什么 / 我标注过哪里 / 我的笔记里怎么说的"。
    **这些是用户写的，不是论文写的** —— 回答时必须说清哪是原文、哪是用户笔记。

    **一条笔记挂在一条高亮上**（`Highlight └── Note`）：所以一条笔记天然同时带着
    `selected_text`（用户**选中的原文**）与 `note`（用户**写的正文**）两个字段 ——
    引用时别把 `selected_text` 当成用户说的话，那是论文的句子；用户说的话在 `note` 里。

    **一次可以查几个关键词**：`notes_search(queries=["量化误差", "KL 散度"])`。
    回执按 query 分组（`groups`），顺序与你给的词一致；某一条失败只挂在那一条上。
    一次最多 8 个词；超了会截断并写明「还有 N 条没处理」。

    参数：
      query   —— 关键词（**选中文本**与**笔记正文**都参与过滤，模糊匹配、大小写不敏感）。
                 `kind="all"` 时不该有"与关键词无关的高亮"混进结果里、把命中的笔记挤掉。
      queries —— 一批关键词。与 query 一起给时按「query 在前、queries 在后」合并去重。
      scope —— ``paper``（默认，只看当前论文）｜``all``（整个库）。
      kind  —— ``all``（默认）｜``notes``（只要笔记正文）｜``highlights``（只要高亮）｜
               ``annotations``（AI 批注）。`note` 为空 = 这条高亮上**没写过笔记**，
               不是"笔记正文丢了"。
    """
    async def _one(q: str) -> dict:
        paper_id = None if scope == "all" else ctx.deps.paper_id
        ctx.deps.count_search()
        data = await ctx.deps.host.notes(query=q, paper_id=paper_id, kind=kind)
        items = []
        for raw in data.get("items") or []:
            pid = str(raw.get("paper_id") or "")
            label = " · ".join(x for x in (
                str(raw.get("paper_title") or ""),
                {"note": "笔记", "highlight": "高亮",
                 "annotation": "批注"}.get(str(raw.get("kind")), ""),
            ) if x)
            cit = ctx.deps.registry.assign(
                key=f"note:{raw.get('kind')}:{pid}:{raw.get('block_id')}:"
                    f"{(raw.get('note') or raw.get('selected_text') or '')[:24]}",
                kind="note", paper_id=pid, block_id=raw.get("block_id"),
                snippet=_clip(raw.get("note") or raw.get("selected_text") or "", 200),
                label=label or "笔记")
            items.append({
                "ref": cit.ref, "paper_id": pid,
                "paper_title": raw.get("paper_title") or "",
                "kind": raw.get("kind"), "selected_text": raw.get("selected_text") or "",
                "note": raw.get("note") or "",
                # 笔记自己的 id（上游肯给才给）：模型据此说清"我在讲哪一条笔记"
                "note_id": str(raw.get("note_id") or raw.get("id") or ""),
                "tags": list(raw.get("tags") or []),
            })
        out = {"query": q, "scope": scope, "kind": kind, "returned": len(items),
               "items": items}
        if not items:
            out["hint"] = "没有找到匹配的笔记 / 高亮 / 批注。"
        elif kind != "notes" and all(not one["note"] for one in items):
            # "笔记正文永远拿不到"这条投诉就出在这一步：回执全是高亮、note 全空，
            # 模型会以为**检索坏了**，而不是"这些高亮上本来就没写笔记"
            out["hint"] = ("这些是高亮，没有附笔记 —— `note` 为空表示这条高亮上"
                           "**没写过笔记**（不是查不到正文）。要看笔记正文用 "
                           "kind='notes'；`query` 会同时过滤选中原文与笔记正文。")
        return out

    async def _impl() -> dict:
        nonlocal scope, kind
        scope = str(scope or "paper").strip().lower()
        kind = str(kind or "all").strip().lower()
        wrong = [f"{field}={value!r}" for field, value, allowed in
                 (("scope", scope, NOTES_SCOPES), ("kind", kind, NOTES_KINDS))
                 if value not in allowed]
        if wrong:
            # 闭集校验（2026-12）：以前拼错**静默降级**，模型以为查了全库、其实只有本篇，
            # 接着就断言"你库里没有别的论文讲过" —— 比报错坏得多。
            return {"ok": False,
                    "error": (f"{'、'.join(wrong)} 不是合法取值。"
                              f"scope 只能是 {'/'.join(NOTES_SCOPES)}；"
                              f"kind 只能是 {'/'.join(NOTES_KINDS)}。"
                              "按上面的写法重发一次，不要自己发明取值。"),
                    # 回显"旧实现会当成什么"：模型据此看出自己那句自信的话从哪来
                    "effective_scope": scope if scope in NOTES_SCOPES else "paper",
                    "effective_kind": kind if kind in NOTES_KINDS else "all",
                    "returned": 0, "items": []}
        wanted, repeats = _text_list(query, queries)
        if not wanted:
            # 不给关键词是**合法**用法（看最近的那些）：批量模式下只允许空清单时给一句说明，
            # 单条模式仍按老样子把 None 传给后端。
            wanted = [""]
        kept, left = _cap_batch(wanted, BATCH_QUERY_MAX)
        if queries is None and len(wanted) == 1:
            return await _one(kept[0] or None)      # 老用法：回执结构一个字都不变
        groups: list[dict] = []
        for q in kept:
            try:
                groups.append(await _one(q or None))
            except HostError as exc:
                groups.append({"query": q, "ok": False, "returned": 0, "items": [],
                               "error": f"取数失败：{exc.message}"})
        return _grouped_receipt(
            groups, wanted=len(wanted), left=left, repeats=repeats,
            cap=BATCH_QUERY_MAX,
            hint=("每组查自己的关键词；`note` 是用户写的，`selected_text` 是论文的句子，"
                  "别读反。"))

    return await _guarded(ctx, "notes_search",
                          {"query": _clip(query or "", 40), "scope": scope,
                           "kind": kind, "queries": list(queries or [])}, _impl())


# --------------------------------------------------------------------------
# ⑤ 写：页级产物纠错（整份工具面里**唯一**会改用户数据的能力）
# --------------------------------------------------------------------------

#: 一次 `find` 的长度上限（与主后端 `page_edit.MAX_FIND` 一致；整页覆盖从后门溜不进来）
EDIT_MAX_FIND = 4000
#: 侧别叫法（主后端也会归一，这里只是为了"先读后写"这条判据能对上号）
_SIDE_ALIASES = {
    "original": "original", "en": "original", "transcript": "original",
    "transcription": "original", "ocr": "original",
    "原文": "original", "转录": "original", "英文": "original",
    "translation": "translation", "zh": "translation", "译文": "translation",
    "翻译": "translation", "中文": "translation",
}


def _side_key(side: str) -> str:
    return _SIDE_ALIASES.get(str(side or "").strip().lower(), str(side or "").strip().lower())


async def page_markdown_edit(ctx: RunContext[Deps], op: str, page: int = 0, side: str = "",
                             find: str = "", replace: str = "",
                             reason: str = "", pages: str = "") -> dict:
    """改**页级产物**（A 路英文转录 / B 路中文译文）里的**一处**文本 —— 修正 OCR 认错、
    译文译错、术语不统一这类问题。

    **只有用户明确要求修改时才用它**（"这处识别错了，改成…"、"这句翻译不对"、"把 X 统一成 Y"）。
    你自己觉得译文不顺、或与你的理解不同，不算理由：那要在回答里**说出来**，而不是动手改。
    改写会立刻影响用户的正文、导出与检索索引，所以它是**先读后写 + 最小改动**：

      op="view"    看这一路这一页的**产物原文**（带行号）—— 改之前必须先看它。
                    行号只是路标：`find` 里写行内容本身，不要带 `|` 和行号。
      op="replace" 把 `find`（**逐字**取自 view，且在这一页里**只出现一次**）换成 `replace`。
                    一次只改一处；`replace` 传空串就是删除。找不到 / 命中多处 / 会让这一页
                    的段落结构变化（块数或行数变了）都会被拒绝，结果里会写清下一步怎么做。
      op="undo"    撤回这一页**最后一次**改动（把改动前的整页文本原样写回）。
                    **不占改动预算**：它只会把东西改回去。
      op="replace_all" 同一串文字一次改**多页 / 两侧**（用户口径 2026-12-07：定理在 3 页各
                    陈述一遍，改一处要 6 次调用）。传 `pages="4,7,19"` 与 `side="both"`；
                    逐处回报（`done` / `skipped`），**一处失败不影响别处**，没读过的页会被跳过。

    改之前请先确认"哪个才是对的"：**OCR 认错**要对着 `page_image_get` 的原版面核对；
    **译文错**要对着原文核对（`page_markdown_get` 一次给两侧）。拿不准就先问用户，
    不要凭猜测改。改完在回答里逐条报告：第几页、哪一侧、原来是什么、改成了什么。

    参数：
      page  —— 页码。
      side  —— `"original"`（A 路英文转录 / OCR 结果）｜`"translation"`（B 路中文译文）。
      find  —— 要改掉的**原文**（必须与 view 的结果逐字一致，且整页唯一）。
      replace —— 改成什么（空串 = 删除这一处）。
      reason —— 一句话说明为什么改（写进改动日志，便于用户回看）。
      pages  —— 只有 op="replace_all" 用：页码范围 ``"4,7"`` / ``"4-7"`` / ``"all"``。
                `side` 可以给 ``"both"``（两侧一起改）。最多 60 处（页 × 侧）一次 —— 与 `EDIT_BATCH_MAX` 同口径（一篇 30 页的论文两侧一起改刚好装得下，再多要分批）。
    """
    async def _impl() -> dict:
        page_no = int(page or 0)
        key = _side_key(side)
        what = "view" if op in ("view", "read", "show") else str(op or "").strip().lower()

        async def _replace_many() -> dict:
            """同一串文字在**多页 / 两侧**一次换掉（用户口径 2026-12-07：定理在 3 页各陈述一遍，
            改一处要 6 次操作）。逐页逐侧落，**一处失败不影响别处**，回执里分开写。"""
            if not str(find or "").strip():
                return {"ok": False, "error": "find 不能为空：它是要改掉的原文，逐字取自 view。"}
            if len(str(find)) > EDIT_MAX_FIND or len(str(replace or "")) > EDIT_MAX_FIND:
                return {"ok": False,
                        "error": f"find / replace 各不超过 {EDIT_MAX_FIND} 字；"
                                 "整页覆盖请分多次调用。"}
            raw_side = str(side or "").strip().lower()
            if raw_side in ("both", "两侧", "两路", "all", "全部"):
                want_sides = ["original", "translation"]
            else:
                one = _side_key(raw_side)
                if one not in ("original", "translation"):
                    return {"ok": False,
                            "error": f"side 只能是 original / translation / both，收到 {side!r}。"}
                want_sides = [one]
            spec = str(pages or "").strip() or str(page or "")
            #: 这里**不能**用 `TEXT_PAGES_MAX`（读正文的上限 20 页）：批量改写不往上下文里塞正文，
            #: 按读正文的上限截会静默漏掉第 21 页往后（2026-12-07 实测：唯一的命中就在第 22 页，
            #: 结果 `pages="all"` 根本没把它算进来）。
            want, clipped = parse_pages(spec, total=ctx.deps.page_total,
                                        limit=EDIT_BATCH_MAX)
            if not want:
                return {"ok": False, "error": pages_error(spec, total=ctx.deps.page_total)}
            if len(want) * len(want_sides) > EDIT_BATCH_MAX:
                return {"ok": False,
                        "error": f"一次最多改 {EDIT_BATCH_MAX} 处（现在是 "
                                 f"{len(want)} 页 × {len(want_sides)} 侧 = "
                                 f"{len(want) * len(want_sides)}）：缩小页码范围再分批。"}
            done: list[dict] = []
            skipped: list[dict] = []
            for no in want:
                for one_side in want_sides:
                    if ((no, one_side) not in ctx.deps.viewed_sides
                            and no not in ctx.deps.pages_read):
                        skipped.append({"page": no, "side": one_side,
                                        "reason": f"先读后写：这一轮还没读第 {no} 页"})
                        continue
                    ctx.deps.count_edit()
                    got = await ctx.deps.host.page_edit(
                        ctx.deps.paper_id, no, one_side, str(find), str(replace or ""),
                        reason=str(reason or ""))
                    if got.get("ok"):
                        ctx.deps.edits.append({
                            "page": got.get("page", no), "side": got.get("side", one_side),
                            "line": got.get("line"), "before": got.get("before"),
                            "after": got.get("after"), "reason": str(reason or "")[:120]})
                        done.append({"page": no, "side": one_side,
                                     "line": got.get("line"),
                                     "summary": got.get("summary") or ""})
                    else:
                        skipped.append({"page": no, "side": one_side,
                                        "reason": str(got.get("error") or "没改成")})
            if done:
                _forget_searches(ctx, "批量改写")
            total = len(want) * len(want_sides)
            out: dict = {"op": "replace_all", "pages": want, "sides": want_sides,
                         "truncated": clipped, "done": done, "skipped": skipped,
                         "summary": f"{total} 处里改了 {len(done)} 处"
                                    + (f"，{len(skipped)} 处没动" if skipped else "")}
            out["ok"] = bool(done)
            if not done:
                why = "；".join(str(s.get("reason") or "") for s in skipped[:3])
                out["error"] = f"一处都没改成：{why}"
            return out

        if what in ("replace_all", "replaceall", "replace-many", "bulk"):
            return await _replace_many()

        #: `page` 给了默认值（批量不需要它）：单页操作没给页码时要说人话，
        #: 而不是让它掉进「第 0 页没有产物」那种看不出是哪错了的提示里。
        if page_no <= 0:
            return {"ok": False,
                    "error": "page 要给页码（单页操作用）；多页同串改写请用 "
                             "op=\"replace_all\" + pages=\"4,7,19\"。"}

        if what == "view":
            data = await ctx.deps.host.page_product(ctx.deps.paper_id, page_no, side)
            if data.get("ok") is False:
                return data
            ctx.deps.viewed_sides.add((page_no, key))
            if page_no not in ctx.deps.pages_read:
                ctx.deps.pages_read.append(page_no)
            return data

        if what in ("replace", "edit", "str_replace", "update"):
            if ((page_no, key) not in ctx.deps.viewed_sides
                    and page_no not in ctx.deps.pages_read):
                return {"ok": False,
                        "error": f"先读后写：这一轮还没有读第 {page_no} 页。"
                                 f"先 page_markdown_get(pages=\"{page_no}\") 看清原文/译文"
                                 f"（改 OCR 就先 page_image_get 核对原版面），"
                                 f"再用 op=\"view\" 拿到要改的那一段逐字文本。"}
            if not str(find or "").strip():
                return {"ok": False, "error": "find 不能为空：它是要改掉的原文，逐字取自 view。"}
            if len(str(find)) > EDIT_MAX_FIND or len(str(replace or "")) > EDIT_MAX_FIND:
                return {"ok": False,
                        "error": f"一次只改**一处**（find / replace 各不超过 {EDIT_MAX_FIND} 字），"
                                 f"不接受整页覆盖；多处修改请分多次调用。"}
            ctx.deps.count_edit()
            data = await ctx.deps.host.page_edit(
                ctx.deps.paper_id, page_no, side, str(find), str(replace or ""),
                reason=str(reason or ""))
            if data.get("ok"):
                ctx.deps.edits.append({
                    "page": data.get("page", page_no), "side": data.get("side", key),
                    "line": data.get("line"), "before": data.get("before"),
                    "after": data.get("after"), "reason": str(reason or "")[:120],
                })
                _forget_searches(ctx, f"p{page_no} 改写")
            return data

        if what in ("undo", "revert", "rollback"):
            # 撤回**不扣改动预算**：它只会把东西改回去，扣预算等于"改错了还不让人撤"
            data = await ctx.deps.host.page_undo(ctx.deps.paper_id, page_no, side)
            if data.get("ok"):
                ctx.deps.edits = [e for e in ctx.deps.edits
                                  if not (e.get("page") == page_no
                                          and e.get("side") == key)]
                _forget_searches(ctx, f"p{page_no} 撤回")
            return data

        return {"ok": False, "error": "op 只能是 view / replace / undo。"}

    return await _guarded(ctx, "page_markdown_edit",
                          {"op": str(op or ""), "page": page, "side": str(side or ""),
                           "find": _clip(find or "", 60)}, _impl())


async def paper_meta_write(ctx: RunContext[Deps], venue: str = "", year: int = 0,
                           tags: list[str] | None = None,
                           keywords_en: list[str] | None = None,
                           abstract_zh: str = "", domain: str = "",
                           one_liner: str = "", contributions: list[str] | None = None,
                           prereq: list[str] | None = None, difficulty: str = "",
                           paper_type: str = "", read_time_min: int = 0,
                           doi: str = "", code_url: str = "") -> dict:
    """把这篇论文的**元数据**写回论文库（一次会话只写一次，读完再写）。

    什么时候用：表单里让你"补全这篇论文的信息"时。它要的是**整篇的结论**，所以顺序是
    「先读 → 再判断 → 最后一次性提交」：`page_markdown_get` 读首页/摘要/结论页，
    `paper_search` 找创新点与实验结论的原文，拿不准的字段**留空**（留空 = 这项不动）。

    **不要编造**：只有材料里明确写了才填。摘要要**译文**、不要用一句话总结冒充；
    会议名只有文中出现才算数。

    参数（全部可选，留空的不写）：
      venue          会议或期刊名（含年份，如 ICLR 2026）。
      year           发表年份（整数）。
      tags           3~6 个**中文**关键词。
      keywords_en    3~6 个**英文**关键词（论文自己列了 Keywords 就用它）。
      abstract_zh    摘要的中文译文（≤400 字）。
      domain         所属领域 / 研究方向（≤20 字）。
      one_liner      一句话概括（≤60 字）。
      contributions  2~5 条核心创新点，每条 ≤60 字，说清相对已有工作新在哪。
      prereq         2~4 条前置知识。
      difficulty     入门 / 进阶 / 专家。
      paper_type     方法 / 综述 / 数据集 / 系统 / 理论 / 其他。
      read_time_min  推荐阅读时间（分钟）。
      doi / code_url DOI 与开源地址（没有就留空）。
    """
    async def _impl() -> dict:
        if ctx.deps.meta_submitted:
            # 文案以前说"带上要改的一起重发" —— 而这一段就是拒掉重发的代码，重发必被拒。
            # 真话是：主后端只**填空字段**（api/agent.py 的 overwrite=False），已有值的字段
            # 重发也写不进去，所以这里明说"不能再改"，并把已经生效的字段回给模型，
            # 让它据实向用户交代，而不是假装改了。
            return {"ok": False, "applied": dict(ctx.deps.meta_applied),
                    "error": "元数据本次会话已经提交并落库了，**不能再改**（主后端只填空字段，"
                             "已有值的字段再提交一次也会被跳过）。下面 applied 里是真正写进去的字段；"
                             "要改这些值，请让用户自己在论文设置里编辑。"}
        payload: dict = {}
        if str(venue or "").strip():
            payload["venue"] = str(venue).strip()
        if int(year or 0):
            payload["year"] = int(year)
        for key, value, limit in (("tags", tags, 8), ("keywords_en", keywords_en, 8),
                                  ("contributions", contributions, 6),
                                  ("prereq", prereq, 6)):
            items = [str(x).strip() for x in (value or []) if str(x).strip()][:limit]
            if items:
                payload[key] = items
        for key, value in (("abstract_zh", abstract_zh), ("domain", domain),
                           ("one_liner", one_liner), ("difficulty", difficulty),
                           ("paper_type", paper_type), ("doi", doi), ("code_url", code_url)):
            if str(value or "").strip():
                payload[key] = str(value).strip()
        if int(read_time_min or 0):
            payload["read_time_min"] = int(read_time_min)
        if not payload:
            return {"ok": False, "error": "没有给出任何字段。至少填你知道的那几项。"}
        data = await ctx.deps.host.meta_write(ctx.deps.paper_id, payload)
        if data.get("ok"):
            ctx.deps.meta_submitted = True
            ctx.deps.meta_applied.update(data.get("applied") or {})
        return data

    return await _guarded(ctx, "paper_meta_write",
                          {"fields": sorted(k for k, v in {
                              "venue": venue, "year": year, "tags": tags,
                              "keywords_en": keywords_en, "abstract_zh": abstract_zh,
                              "domain": domain, "one_liner": one_liner,
                              "contributions": contributions, "prereq": prereq,
                              "difficulty": difficulty, "paper_type": paper_type,
                              "read_time_min": read_time_min, "doi": doi,
                              "code_url": code_url}.items() if v)},
                          _impl())


def _with_rendered_length(receipt: dict) -> dict:
    """给高亮回执逐条补上 `rendered_length`（= end - start，**渲染坐标**下的长度）。

    为什么需要它：`start` / `end` 是渲染坐标下的偏移（公式在那一套里只占
    1 个字符），而 `text` 里露出来的是 LaTeX 原文 —— 两者长度天生不同，模型拿
    `len(text)` 去推偏移必然错位（用户自检报的摩擦点）。这里只**加**一个字段，
    老的 `start` / `end` 一个都不动。
    """
    def one(row: object) -> None:
        if not isinstance(row, dict) or "start" not in row or "end" not in row:
            return
        try:
            start, end = int(row.get("start") or 0), int(row.get("end") or 0)
        except (TypeError, ValueError):
            return
        if end >= start:
            row["rendered_length"] = end - start

    if not isinstance(receipt, dict):
        return receipt
    for row in receipt.get("items") or []:
        one(row)
    one(receipt.get("highlight"))
    return receipt


async def highlight_manage(ctx: RunContext[Deps], op: str, anchor: str = "", text: str = "",
                           color: str = "", side: str = "", occurrence: int = 1,
                           highlight_id: str = "", query: str = "", reason: str = "",
                           items: list[dict] | None = None) -> dict:
    r"""给这篇论文的**高亮**做增删改查、以及"这条高亮在哪"。

    高亮是"用户在某一面上标出来的一段文字"，也是**笔记的锚点**（`Highlight └── Note`）。
    三面各有各的位置模型（正文行 / Markdown 视图 / 原 PDF 页图）—— 你只需要给
    `anchor`（哪一面）+ `text`（要标的**原文**），偏移量由宿主算，不要自己编数字。

    ⚠️ **默认标在"正文行"那一面**：用户读的对照 / 译文视图画的就是它。
    **行 id 只有一处拿得到**：`figure_list` 回执里每条图形的 `anchor`（= 图注所在行的
    行 id，如 `p008-02`），把它**原样**传进来就是最准的锚点。
    其它读页工具的返回里**没有**行 id（`page_markdown_get` / `block_get` 都没有 `row_id`）——
    **不要自己编 `p009-08` 这种**；没有 `anchor` 时给 `md:pN`。
    给 `md:pN`：宿主会**尽量把它挪到这一页对应的正文行**上（见 `annotations.py` 的
    `locate_in_rows`），挪不动才留在 Markdown 面；`md:pN` 的高亮只在 Markdown 视图里出现，
    用户在你面前看不到（实测被报过"高亮没出现"）。原 PDF 那面服务端算不出偏移量（文字层在前端）。

      op="create"  anchor + text（+ color / side / occurrence）→ 建一条高亮。
                   **批量**：改传 `items`（数组，每条 `{anchor, text, color?, side?}`，最多 24 条），
                   一次建一批；回执逐条给 `created` / `failed`（含原因与最像的原文），
                   一条失败不影响别的。
                   **color 可以是你想要的任何颜色**（用户口径 2026-11-26）：直接给 `#rrggbb`
                   （或 #rgb / rgb() / hsl()）即可。界面上那排色点现在是**多巴胺色板**这八个：
                   strawberry `#ff4d6d` / tangerine `#ff8a3d` / lemon `#ffc93c` / mint `#3ddc84` /
                   lagoon `#22d3ee` / vega `#4d7cff` / grape `#a855f7` / sakura `#ff5cc8` ——
                   用户说"标成黄的/蓝的"时就近取这几个，报上来的是色名也照收。
                   ⚠️ **配色分两级**（用户口径 2026-12-07 修正）：先按**类别**定色系，再在同一
                   色系里用深浅区分条目 —— 颜色表达语义，不是「每条都不一样」。
                   公式 = indigo（`#6366f1`/`#4f46e5`）、定理/结论 = emerald（`#10b981`/`#059669`）、
                   术语/定义 = violet（`#8b5cf6`/`#7c3aed`）、存疑 = amber（`#f59e0b`/`#d97706`）、
                   错误/待办 = rose（`#f43f5e`/`#e11d48`）。**同类别允许同色**，跨类别才必须不同；
                   同色系条目多了就换深浅（indigo-400/500/600），不要跳到别的色系去。
                   动手前先 `op=list` 看这篇已经用过哪几套色系，跟着已有的走。
                   ✅ **颜色代码可以由你自己给**（2026-12-06 明确）：`#0d9488` / `#f59e0b` /
                   `rgb(20,120,140)` / `hsl(180 60% 40%)` 都能如实存下并原样画出 —— 想按语义
                   自己设计一套配色（这套论文用 teal 系、那套用 indigo 系）就直给 hex，不必迁就
                   菜单上那八个色点。常见颜色词（teal / cyan / amber / rose / slate …）也会被认成 hex，
                   不再静默变成黄色；只有**完全没给**时才由宿主挑当前用得最少的。
                   `text` 必须与那一面的文字**逐字一致**（Markdown 记号不算字：`**重点**` 就写
                   `重点`，照抄 `page_markdown_get` / `translate_get` 里的原文即可）。
                   **公式可以标**（2026-12-06 补）：把公式的 LaTeX 原样给它（带不带 `$$` 都行，
                   只给式子的一部分也认，但至少 8 个字符）—— 坐标系里公式只占 1 个占位符，
                   宿主会把 LaTeX 换成那个占位符的位置，前端据此把整个公式节点包起来（块级
                   公式连外接框一起）。**不要因为"公式不是文字"就退回去标引出它的那句话。**
                   **公式那一处可以留空**（2026-12-07 实测）：`$x$` 在界面上是个公式元素，
                   用户从界面复制那句中文时那一处常常只剩一个空位 —— 按「没有公式」的样子传
                   进来也能对上（宿主匹配时会忽略公式占位与空白），带 `$x$` 也认。
                   `occurrence`：同一段文字出现多次时标第几次（默认第 1 次）。
                   找不到时回的「最像的原文」来自**原始 Markdown**（公式以 `$…$` 露出），照着
                   改一版再试；**跨行 / 跨页**这种标不了的情况会明说是哪一种，不要瞎猜。
                   ⚠️ **不指定 `side` 就落译文面（中文）**（用户口径 2026-12-06）：读者平时看的
                   是中文那一列，标在原文面的高亮**画不出来**（用户报过「未产生高亮」）。所以：
                   - 一般**不要显式给 `side`**，宿主按文字自动判（中文 / 公式 → 译文面）；
                   - 要给，就写 `zh`（= 译文面）/ `en`（= 原文面）—— 两套词表（`original`/`translation`）
                     也认，但只有 `zh`/`en` 是准的；**显式给了就照办**（2026-12-07：以前公式会被
                     静默改成译文面，落到另一条公式上），落不成时回执带 `side_note` 说明；
                   - 只有用户明确说「我在读英文原文」时才用 `en`；
                   - 回执里出现 `warning` = 这条落在 **Markdown 面**（阅读视图里看不到它），
                     后面写着原因（跨行 / 页内对不上）—— 请如实转述给用户。
      op="list"    query（可选）→ 列这篇的高亮（带页码与"在哪一面"）。
                   ⚠️ 每条的 `start` / `end` 是**渲染坐标**下的偏移，不是
                   `text` 里的字符下标：那个坐标系里**一整条公式只占 1 个字符**
                   （前端把公式节点当单个占位符画），而 `text` 里露出来的是 LaTeX 原文
                   （`\frac{a}{b}` 有十几个字符）。所以拿 `len(text)` 去切偏移、
                   或者把 `end - start` 当成"引文多长"，一定对不上 —— 两者长度本来就不是一回事。
                   要知道引文在坐标系里多长，读 `rendered_length`（= end - start）。
      op="locate"  highlight_id → 这条高亮挂在哪一面、第几页、原文是什么（还能顺带看它有没有笔记）。
      op="update"  highlight_id + color（改颜色）/ text（改引用文字）/ **anchor（挪位置）**。
                   改锚点：`anchor="md:p6"`（+ 可选 `text`）→ 宿主在新锚点重新定位这段文字，
                   **id 不变**、挂在它上面的笔记跟着走（用户口径 2026-12-07：标错一行时删了
                   重建会连带删笔记，成本太高）。新锚点那边找不到会回原因，什么都不改。
      op="delete"  highlight_id + reason → 删一条高亮（跨行/跨页的整组一起删，
                   **它挂着的笔记也会一起删**）。删之前先 `op="locate"` 确认是那一条；
                   用户没明确说"删掉这处高亮"就不要删。

    与"解释这段内容"的分工：解释是**读**（不需要动数据）；只有用户说"把这段标出来 / 加个高亮"
    才用本工具写。写完在回答里说清标在哪一页、哪一段文字。

    ⚠️ 标的若是**含常数的公式**（`\sqrt{3}\,\pi^2` 这种），先 `page_image_get` 对原版面再动手：
    转录里的根号覆盖范围与上下标不可靠，标错对象比没标更糟。
    """
    async def _impl() -> dict:
        what = str(op or "").strip().lower()
        writes = {"update", "delete", "remove"}
        if what in writes:
            ctx.deps.count_edit()

        async def _create_one(a_anchor: str, a_text: str, a_color: str,
                              a_side: str, a_occurrence: int) -> dict:
            """建**一条**高亮（单条与批量共用同一个落库路径，省得两份字段慢慢走偏）。"""
            ctx.deps.count_edit()
            data = await ctx.deps.host.highlight_create(ctx.deps.paper_id, {
                "anchor": str(a_anchor or ""), "text": str(a_text or ""),
                "color": str(a_color or "yellow"),
                # **原样透传**：`en` / `zh` 是库里的侧别词表，翻译成 `original` / `translation`
                # 反而会被 `create_highlight` 当成不认识而回落成 `en` —— 用户要标中文，高亮却画在
                # 英文面（agent 报过"未产生高亮"）。空着则宿主按文字自动判（中文 / 公式 → 译文面）。
                "side": str(a_side or "").strip().lower(),
                "occurrence": int(a_occurrence or 1)})
            if data.get("ok"):
                hl = data.get("highlight") or {}
                out = {"ok": True, "highlight_id": hl.get("id"), "page": data.get("page"),
                       "scope": data.get("scope"), "text": hl.get("text"),
                       "color": hl.get("color"),
                       #: 落面 / 页码 / 命中原文**每次都给**（用户口径 2026-12-07）：
                       #: 以前只有失败才有提示，模型只能推断"这条标在了译文面"。
                       "summary": data.get("summary"), "landed": data.get("landed"),
                       "note": "已建立高亮。要给它写笔记就用 note_manage op=create。"}
                # 宿主的两条"别忽略我"的信号必须原样带出去：这里是白名单字典，新增字段
                # 不显式搬一遍就会被丢掉（2026-12-07 实测：跨块落在 Markdown 面的高亮
                # 明明带了 warning，模型侧一个字都没看到）。
                for extra in ("warning", "side_note", "text_note", "overlap"):
                    if data.get(extra):
                        out[extra] = data[extra]
                return out
            # 失败时把"最接近的原文"贴进 error：模型改对引文只能靠它（near_miss 也原样带上）
            near = [str(item) for item in (data.get("near_miss") or [])][:2]
            error = str(data.get("error") or "建立高亮失败")
            if near:
                error = f"{error}最接近的是：{' / '.join(near)}（照着它逐字改 text 再试一次）"
            return {**data, "error": error}

        if what in ("create", "add", "new"):
            rows = [one for one in (items or []) if isinstance(one, dict)]
            if items and not rows:
                return {"ok": False,
                        "error": "items 要是数组：每条 {anchor, text, color?, side?}。"}
            if rows:
                #: 批量：**一条失败不影响别的**（用户口径 2026-12-07：12 条高亮 + 12 条笔记，
                #: 一条条来回 24 次太贵）。逐条回报，模型据此只重试失败的那几条。
                if len(rows) > ANNOTATE_BATCH_MAX:
                    return {"ok": False,
                            "error": f"一次最多建 {ANNOTATE_BATCH_MAX} 条（收到 {len(rows)} 条）："
                                     "分批来。"}
                made: list[dict] = []
                missed: list[dict] = []
                for i, one in enumerate(rows):
                    got = await _create_one(one.get("anchor") or anchor,
                                            one.get("text") or "",
                                            one.get("color") or color,
                                            one.get("side") or side,
                                            one.get("occurrence") or occurrence)
                    if got.get("ok"):
                        entry = {"index": i, "highlight_id": got.get("highlight_id"),
                                 "page": got.get("page"), "color": got.get("color"),
                                 "summary": got.get("summary")}
                        if got.get("warning"):
                            entry["warning"] = got["warning"]
                        made.append(entry)
                    else:
                        missed.append({"index": i, "error": got.get("error"),
                                       "near_miss": got.get("near_miss") or []})
                out = {"ok": bool(made), "op": "create", "created": made,
                       "failed": missed,
                       "summary": f"建了 {len(made)} 条高亮"
                                  + (f"，{len(missed)} 条没建成" if missed else "")}
                if not made:
                    out["error"] = "一条都没建成：" + "；".join(
                        str(one.get("error") or "") for one in missed[:2])
                return out
            return await _create_one(anchor, text, color, side, occurrence)
        if what in ("list", "search"):
            return _with_rendered_length(
                await ctx.deps.host.highlights(ctx.deps.paper_id, query=str(query or "")))
        if what in ("locate", "info", "show"):
            if not highlight_id:
                return {"ok": False, "error": "locate 要给 highlight_id（先用 op=list 拿）。"}
            return await ctx.deps.host.highlights(ctx.deps.paper_id,
                                                  highlight_id=str(highlight_id))
        if what in ("update", "color", "set", "move"):
            if not highlight_id:
                return {"ok": False, "error": "update 要给 highlight_id（先用 op=list 拿）。"}
            payload: dict = {}
            if str(color or "").strip():
                payload["color"] = str(color).strip()
            if str(text or "").strip():
                payload["selected_text"] = str(text).strip()
            move_to = str(anchor or "").strip()
            target: dict = {}
            moved_page = None
            if move_to:
                # 改锚点（用户口径 2026-12-07）：标错一行时**挪**，不要删了重建 ——
                # 删高亮会连带删笔记。这里先在新锚点算落点（不建新的），再把坐标写回这一条，
                # id 不变、笔记跟着走。
                probe = str(text or "").strip()
                if not probe:
                    cur = await ctx.deps.host.highlights(ctx.deps.paper_id,
                                                         highlight_id=str(highlight_id))
                    probe = str(((cur.get("highlight") or {}).get("text")) or "")
                if not probe:
                    return {"ok": False,
                            "error": "改锚点要知道标的是哪段文字：给 text，或先 op=locate 看它引用的原文。"}
                found = await ctx.deps.host.annotation_locate(ctx.deps.paper_id, {
                    "anchor": move_to, "text": probe, "side": str(side or ""),
                    "occurrence": int(occurrence or 1)})
                if not found.get("ok"):
                    return {**found,
                            "error": "新锚点那边定位不到这段文字："
                                     + str(found.get("error") or "")}
                target = found.get("target") or {}
                moved_page = found.get("page")
                payload.update({"block_id": target.get("anchor"),
                                "scope": target.get("scope"), "side": target.get("side"),
                                "start_offset": target.get("start"),
                                "end_offset": target.get("end")})
                if not str(text or "").strip():
                    payload["selected_text"] = str(target.get("text") or "")
            if not payload:
                return {"ok": False, "error": "update 至少要给 color / text / anchor 之一。"}
            got = await ctx.deps.host.highlight_update(str(highlight_id), payload)
            out: dict = {"ok": True, "highlight": got}
            if move_to:
                side_word = {"zh": "译文面", "en": "原文面"}.get(
                    str(target.get("side") or ""), "Markdown 面")
                quote = _clip(str(target.get("text") or ""), 30)
                out["moved_to"] = {"page": moved_page, "side": target.get("side"),
                                   "scope": target.get("scope"), "text": quote}
                out["summary"] = (f"已挪到第 {moved_page} 页 · {side_word} · 「{quote}」"
                                  if moved_page else f"已挪到新位置 · {side_word} · 「{quote}」")
                out["note"] = "高亮 id 没变，挂在它上面的笔记跟着走；位置不对再挪一次即可。"
            return out
        if what in ("delete", "remove", "rm"):
            if not highlight_id:
                return {"ok": False, "error": "delete 要给 highlight_id。"}
            if not str(reason or "").strip():
                return {"ok": False, "error": "delete 要写一句 reason（为什么删这一条）。"}
            gone = await ctx.deps.host.highlight_delete(str(highlight_id))
            return {"ok": True, "deleted": gone.get("deleted_ids") or [gone.get("deleted")],
                    "deleted_notes": gone.get("deleted_notes") or [],
                    "note": "高亮（以及挂在它上面的笔记）已删除。"}
        return {"ok": False, "error": "op 只能是 create / list / locate / update / delete。"}

    return await _guarded(ctx, "highlight_manage", {
        "op": str(op or ""), "anchor": _clip(anchor or "", 40),
        "text": _clip(text or "", 40), "highlight_id": highlight_id,
    }, _impl())


async def note_manage(ctx: RunContext[Deps], op: str, highlight_id: str = "", note_id: str = "",
                      content: str = "", query: str = "", reason: str = "",
                      items: list[dict] | None = None) -> dict:
    r"""给**高亮**写笔记 / 查笔记 / 改笔记 / 删笔记 / 看这条笔记对应的高亮。

    关系是固定的：`Highlight └── Note` —— **笔记必须挂在一条高亮上**（高亮是它的位置锚点），
    所以 `op="create"` 一定要 `highlight_id`；没有高亮就先用 `highlight_manage op=create`
    把那段文字标出来，别建"孤儿笔记"（用户口径）。

      op="create"  highlight_id + content（Markdown）→ 加笔记。
                   **批量**：改传 `items`（数组，每条 `{highlight_id, content}`，最多 24 条），
                   一次写一批；回执逐条给 `created` / `failed`。
      op="list"    query（可选）→ 列这篇的笔记（每条带**它挂靠的高亮原文**与页码）。
      op="locate"  note_id → 这条笔记挂在哪一面的哪一处（"定位到某条笔记对应的原文"）。
      op="update"  note_id + content → 改正文。
      op="delete"  note_id + reason → 删笔记（**高亮不动**：笔记只是附注）。

    正文用 Markdown 写（支持 `$公式$`）。写完在回答里说清"挂在哪一处高亮上"。

    ⚠️ **笔记里要写数值 / 常数 / 上下标时，先 `page_image_get` 对原版面**（用户口径 2026-12-07）：
    转录里的 LaTeX **不可靠** —— `\sqrt{3}\,\pi^2` 与 `\sqrt{3\pi^2}` 会是同一串字符，
    根号覆盖到哪儿的信息丢了，抄错一个常数就把结论写反。只做概念性说明时才可以直接依据转录。
    """
    async def _impl() -> dict:
        what = str(op or "").strip().lower()
        if what in {"create", "add", "new", "update", "delete", "remove"}:
            ctx.deps.count_edit()
        async def _create_one(hid: str, body: str) -> dict:
            """写**一条**笔记（单条与批量共用；批量省掉一条一来回的 12 次往返）。"""
            if not str(hid or "").strip():
                return {"ok": False, "error": "笔记必须挂在一处高亮上：要给 highlight_id"
                                              "（先用 highlight_manage op=create 标出来）。"}
            if not str(body or "").strip():
                return {"ok": False, "error": "content 不能为空（笔记正文）。"}
            data = await ctx.deps.host.note_create(ctx.deps.paper_id, {
                "highlight_id": str(hid), "content": str(body)})
            note = data.get("note") or {}
            page = note.get("page")
            quote = str(note.get("selected_text") or "")
            out = {"ok": True, "note_id": note.get("id"), "page": page,
                   "scope": note.get("scope"), "quote": quote,
                   "summary": f"第 {page} 页 · 挂在「{_clip(quote, 24)}」上" if page
                              else f"挂在「{_clip(quote, 24)}」上"}
            for extra in ("warning", "similar"):   # 重复笔记提醒（只提醒不拒绝）
                if note.get(extra):
                    out[extra] = note[extra]
            return out

        if what in ("create", "add", "new"):
            rows = [one for one in (items or []) if isinstance(one, dict)]
            if items and not rows:
                return {"ok": False,
                        "error": "items 要是数组：每条 {highlight_id, content}。"}
            if rows:
                if len(rows) > ANNOTATE_BATCH_MAX:
                    return {"ok": False,
                            "error": f"一次最多写 {ANNOTATE_BATCH_MAX} 条（收到 {len(rows)} 条）："
                                     "分批来。"}
                made: list[dict] = []
                missed: list[dict] = []
                for i, one in enumerate(rows):
                    got = await _create_one(one.get("highlight_id") or highlight_id,
                                            one.get("content") or "")
                    if got.get("ok"):
                        made.append({"index": i, "note_id": got.get("note_id"),
                                     "page": got.get("page"), "summary": got.get("summary")})
                    else:
                        missed.append({"index": i, "error": got.get("error")})
                out = {"ok": bool(made), "op": "create", "created": made,
                       "failed": missed,
                       "summary": f"写了 {len(made)} 条笔记"
                                  + (f"，{len(missed)} 条没写成" if missed else "")}
                if not made:
                    out["error"] = "一条都没写成：" + "；".join(
                        str(one.get("error") or "") for one in missed[:2])
                return out
            return await _create_one(highlight_id, content)
        if what in ("list", "search"):
            return await ctx.deps.host.paper_notes(ctx.deps.paper_id,
                                                   query=str(query or ""))
        if what in ("locate", "info", "show"):
            if not note_id:
                return {"ok": False, "error": "locate 要给 note_id（先用 op=list 拿）。"}
            return await ctx.deps.host.paper_notes(ctx.deps.paper_id,
                                                   note_id=str(note_id))
        if what in ("update", "edit"):
            if not note_id:
                return {"ok": False, "error": "update 要给 note_id。"}
            if not str(content or "").strip():
                return {"ok": False, "error": "update 要给 content。"}
            got = await ctx.deps.host.note_update(str(note_id), {"content_md": str(content)})
            return {"ok": True, "note": got}
        if what in ("delete", "remove", "rm"):
            if not note_id:
                return {"ok": False, "error": "delete 要给 note_id。"}
            if not str(reason or "").strip():
                return {"ok": False, "error": "delete 要写一句 reason（为什么删这一条）。"}
            return {"ok": True, "deleted": await ctx.deps.host.note_delete(str(note_id)),
                    "note": "笔记已删除（它挂的高亮还在）。"}
        return {"ok": False, "error": "op 只能是 create / list / locate / update / delete。"}

    return await _guarded(ctx, "note_manage", {
        "op": str(op or ""), "highlight_id": highlight_id, "note_id": note_id,
        "content": _clip(content or "", 40),
    }, _impl())


# --------------------------------------------------------------------------
# ⑤ 联网：搜索公开网页 / 抓网页正文（自带技能 `web-search` 的手脚）
#
# 这两个工具的返回值**仍然要瘦**：一次 8 条 × 上千字的 summary 就是上万字，
# 模型会以为"我已经读过了"。所以摘要按下面两个上限收口，正文只给 web_fetch 要的那一篇。
# --------------------------------------------------------------------------

#: 一条搜索结果的摘要上限（官方 summary 通常 600~800 字，留一半够判断要不要读原文）
WEB_SUMMARY_CHARS = 400
#: 随搜索结果附带的正文上限：真要读全文请走 web_fetch，别一次拉 8 篇
WEB_CONTENT_CHARS = 600


def _need_content_slot(need_content: bool | None) -> int:
    """`need_content` 的三态编码（0 = 不传，跟随主后端的设置）。

    缓存键如果只有 (query, count)，"不传 / False / True"三种调用会互相命中：
    它们的**返回字段并不一样**（带不带 content），串味之后模型会以为"这次也没正文"。
    所以把三态编进键里 —— 反正 count 上限 50，`count * 3 + slot` 不会撞。
    """
    if need_content is None:
        return 0
    return 2 if need_content else 1


async def web_search(ctx: RunContext[Deps], query: str = "", count: int | None = None,
                     need_content: bool | None = None, question: str = "",
                     queries: list[str] | None = None) -> dict:
    """联网搜索公开网页（DeepSeek 官方搜索），返回标题 / 链接 / 站点 / 摘要。

    什么时候用：问题问的是**论文之外**的公开信息 —— 某个方法的官方文档或开源实现、
    数据集在哪下载、这篇论文是否已发表、某个术语业界现在怎么说；或者用户直接让你
    "上网查一下"。需要论文里的论断时**不要**用它：那是 paper_search / kb_search 的活，
    网页不是论文原文。

    **一次可以查几件事**（每个 query 仍只查一件事，只是不必来回几次）：

        web_search(queries=['site:arxiv.org "TurboQuant"', "TurboQuant github"])

    回执**按 query 分组**（`groups`），顺序与你给的词一致。一次最多 8 个词：超了会截断并
    写明「还有 N 条没处理」。某一条失败只在那一条上给 `ok=false` + 原因，别的照常返回。

    参数：
      query   —— 搜索词。支持 `site:arxiv.org`（限站）、`-site:csdn.net`（排除）、
                 `"精确短语"`、`after:2025-01-01`（限时间）这些语法；多个站点用 `OR` 连接。
                 **一个 query 只查一件事**，别把好几个问题塞进一个 query。
      queries —— 一批搜索词（不同的事分开放这里）。与 query 一起给时合并去重。
      count   —— 每个词要几条，1~50。不传就用设置里的默认（现在是 8）。
      need_content —— 是否让每条结果带上网页正文。不传就用设置里的默认（现在是只取摘要）：
                正文动辄上万字，只想看某一篇时用 web_fetch 更省。
      question —— 触发这次搜索的用户原话，帮搜索引擎理解意图；一般值得填。
    """
    async def _one(q: str) -> dict:
        key = int(count or 0) * 3 + _need_content_slot(need_content)
        cached = _cached(ctx, "web_search", q, key)
        if cached is not None:
            return cached
        ctx.deps.count_search()
        data = await ctx.deps.host.web_search(
            q, count=count, need_content=need_content, ori_question=question)
        items = []
        for raw in data.get("results") or []:
            item = {
                "title": raw.get("title") or "",
                "url": raw.get("url") or "",
                "site": raw.get("site_name") or "",
                "date": raw.get("date"),
                "snippet": _clip(raw.get("summary") or raw.get("snippet") or "",
                                 WEB_SUMMARY_CHARS),
            }
            # 按"服务端给没给"收，而不是按我们传了什么：默认可能来自设置里的 True
            if raw.get("content"):
                item["content"] = _clip(raw["content"], WEB_CONTENT_CHARS)
            items.append(item)
        out = {"query": q, "provider": data.get("provider"),
               "returned": len(items), "items": items}
        if not items:
            out["hint"] = ("没有搜到结果：换一组关键词（去掉 `site:` 限制、少用引号）再试一次；"
                           "还是一条都没有，就如实跟用户说没查到，不要凭记忆编。")
        else:
            out["hint"] = ("这些是**外部网页**，不是论文里的材料：回答里引用它们必须写"
                           "`[标题](链接)` 并带上站点名；要看某一篇的正文就说清是哪一条、"
                           "用 web_fetch 读，不要拿摘要冒充读过全文。")
        _remember(ctx, "web_search", q, key, out)
        return out

    async def _impl() -> dict:
        wanted, repeats = _text_list(query, queries)
        if not wanted:
            return {"ok": False,
                    "error": "要给 query 或 queries：queries=[\"第一件事\", \"第二件事\"] 一次搜几件。"}
        kept, left = _cap_batch(wanted, BATCH_QUERY_MAX)
        if queries is None and len(wanted) == 1:
            return await _one(kept[0])      # 老用法：单个词的回执结构一个字都不变
        groups: list[dict] = []
        for q in kept:
            try:
                groups.append(await _one(q))
            except HostError as exc:
                groups.append({"query": q, "ok": False, "returned": 0, "items": [],
                               "error": f"取数失败：{exc.message}"})
        return _grouped_receipt(
            groups, wanted=len(wanted), left=left, repeats=repeats,
            cap=BATCH_QUERY_MAX,
            hint="每组是那个词的结果；引用时必须写 `[标题](链接)` + 站点名。")

    return await _guarded(ctx, "web_search", {
        "query": _clip(query, 60), "queries": list(queries or []), "count": count,
    }, _impl())


async def web_fetch(ctx: RunContext[Deps], url: str = "", max_chars: int | None = None,
                    urls: list[str] | None = None) -> dict:
    """把一个网页（或**一批网页**）抓成 Markdown 正文（DeepSeek 官方抓取），按需读全文。

    什么时候用：web_search 的结果里有一条看起来正是你要的依据、摘要不够判断；
    或者用户直接给了一个链接让你看内容。

    **一次可以抓几篇**（抓回来的正文很占上下文，所以上限比别的批量工具更紧）：

        web_fetch(urls=["https://a.example/x", "https://b.example/y"])

    批量时是**并发**抓的（最多 3 篇同时、每篇 25 秒超时）—— 串行会让一次调用把整个回合
    拖死。回执按你给的顺序逐条对应（每条带自己的 `url`）；某一条打不开只在那一条上给
    `ok=false` + 原因，别的照常返回。一次最多 6 条；超了会截断并写明「还有 N 条没处理」。
    `total_chars` 会告诉你这批一共抓回来多少字 —— 太多就该只留真正要看的那几篇。

    不要抓：搜索结果页、站点首页、要登录才能看的地址（会报抓不到）。

    参数：
      url       —— 单个完整的 http/https 地址（web_search 返回的 url 原样传即可）。
      urls      —— 一批地址。与 url 一起给时按「url 在前、urls 在后」合并去重。
      max_chars —— 每篇正文最多保留多少字符。不传就用设置里的默认（现在是 6000）；
                   超出部分会被丢掉，返回里 truncated=true。
    """
    async def _one(one_url: str) -> dict:
        key = int(max_chars or 0)
        cached = _cached(ctx, "web_fetch", one_url, key)
        if cached is not None:
            return cached
        ctx.deps.count_search()
        data = await ctx.deps.host.web_fetch(one_url, max_chars=max_chars)
        content = str(data.get("content") or "")
        out = {
            "url": data.get("url") or one_url,
            "title": data.get("title") or "",
            "chars": len(content),
            "truncated": bool(data.get("truncated")),
            "content": content,
        }
        if not content:
            out["hint"] = ("这一页没有取到正文（可能要登录、被站点拦下，或本来就不是文章页）："
                           "换一条搜索结果，或如实说明打不开。")
        elif out["truncated"]:
            out["hint"] = (f"正文只保留了前 {len(content)} 个字符（truncated=true）："
                           f"要看后面的部分，调大 max_chars 再抓一次。")
        else:
            out["hint"] = ("这是**外部网页**的内容，不是论文原文：引用时给 markdown 链接 + 站点名；"
                           "论文里的论断仍然要引用论文里的材料。")
        _remember(ctx, "web_fetch", one_url, key, out)
        return out

    async def _impl() -> dict:
        wanted, repeats = _text_list(url, urls)
        if not wanted:
            return {"ok": False,
                    "error": "要给 url 或 urls：urls=[\"https://a\", \"https://b\"] 一次抓几篇。"}
        kept, left = _cap_batch(wanted, BATCH_URL_MAX)
        if urls is None and len(wanted) == 1:
            return await _one(kept[0])      # 老用法：单篇回执结构一个字都不变
        #: 并发抓：上限 3 篇同时 + 每篇一个超时。串行 6 篇 × 后端几十秒 = 回合被拖死。
        gate = asyncio.Semaphore(BATCH_URL_CONCURRENCY)

        async def _guarded_one(one_url: str) -> dict:
            async with gate:
                try:
                    return await asyncio.wait_for(_one(one_url), WEB_FETCH_TIMEOUT)
                except asyncio.TimeoutError:
                    return {"url": one_url, "ok": False, "returned": 0, "items": [],
                            "error": f"抓取超时（超过 {int(WEB_FETCH_TIMEOUT)} 秒）："
                                     "这一篇单独放弃，别的照常。"}
                except HostError as exc:
                    return {"url": one_url, "ok": False, "returned": 0, "items": [],
                            "error": f"取数失败：{exc.message}"}
                except Exception as exc:  # noqa: BLE001 —— 一条坏不能拖垮整批
                    return {"url": one_url, "ok": False, "returned": 0, "items": [],
                            "error": f"工具内部错误：{type(exc).__name__}: {exc}"[:200]}

        rows = list(await asyncio.gather(*[_guarded_one(one) for one in kept]))
        # 顺序与输入对齐：asyncio.gather 保序，这里只是把结构摆成「按条对应」
        failed = [row for row in rows if row.get("ok") is False]
        got = [row for row in rows if row.get("ok") is not False]
        total_chars = sum(int(row.get("chars") or 0) for row in got)
        tips: list[str] = []
        if left:
            tips.append(_left_note(left, BATCH_URL_MAX, "条"))
        if repeats:
            tips.append(f"重复的 {repeats} 条已合并。")
        if total_chars > WEB_CONTENT_CHARS * BATCH_URL_MAX:
            tips.append(f"这批一共 {total_chars} 字，很占上下文：只留真正要看的那几篇，"
                        "其余按标题 + 链接引用即可。")
        if failed:
            tips.append("打不开的那几条在 items 里带 ok=false 与原因，别的照常返回 —— "
                        "只补那几条，不要整批重来。")
        out = {"requested": len(wanted), "urls": kept,
               "returned": len(got), "failed": len(failed),
               "unprocessed": left, "total_chars": total_chars, "items": rows}
        if tips:
            out["note"] = " ".join(tips)
        out["hint"] = ("每一条都要按 `[标题](链接)` + 站点名引用；批量抓回来的正文很长，"
                       "写回答时只挑支撑论断的那几句。")
        return out

    return await _guarded(ctx, "web_fetch", {
        "url": _clip(url, 120), "urls": [_clip(u, 120) for u in list(urls or [])][:6],
        "max_chars": max_chars,
    }, _impl())


#: `Agent(tools=...)` 用这一份；顺序即提示词里的出现顺序
P0_TOOLS = [paper_search, page_markdown_get, block_get, page_image_get,
            figure_list, figure_image_get]

#: P1 补齐的读工具（`paper_archive_get` = 背景档案：以前是**每轮预注入**，
#: 用户口径 2026-10-07 改成"要背景自己调一次"）
P1_TOOLS = [kb_search, formula_get, translate_get, library_list, notes_search,
            paper_archive_get]

#: 写工具（唯一一处会改用户数据的能力；预算与"先读后写"都在实现里）
EDIT_TOOLS = [page_markdown_edit]

#: 高亮 / 笔记（《修改计划》第六步）：两个 op 式工具，读写都在里面
ANNOTATION_TOOLS = [highlight_manage, note_manage]

#: 阅读状态（自带技能 `reader-state` 的手脚）：**只读**的一块旁证 ——
#: "读者此刻在第几页、看哪一面"。不打后端、不占上下文，只有在要**落笔标注**时才有意义，
#: 所以挨着标注工具摆：模型翻到标注那一段就能看见答案从哪来。
READER_TOOLS = [reader_state]

#: 联网（自带技能 `web-search` 的手脚）：**永远挂载**，没配密钥时返回一句人话错误。
#: 为什么不做成"有密钥才挂"：工具表一变，system 里那份工具说明与函数 schema 也跟着变，
#: 一个设置项就能把整段提示词前缀打掉；而且模型不知道"现在能不能联网"只会少问一句。
WEB_TOOLS = [web_search, web_fetch]

#: 当前挂载的全部工具
AGENT_TOOLS = [*P0_TOOLS, *P1_TOOLS, *EDIT_TOOLS, *ANNOTATION_TOOLS,
               *READER_TOOLS, *WEB_TOOLS]

#: **元数据填写**那一次会话用的工具：只读的读全文能力 + 一个写口。
#: 为什么单独一份、不并进 `AGENT_TOOLS`：问答会话里模型顺手改元数据的场景并不存在，
#: 而"少一个写口"就是少一类越权。
META_TOOLS = [paper_search, page_markdown_get, block_get, paper_meta_write]
