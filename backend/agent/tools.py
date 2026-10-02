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

import re
import time

from pydantic_ai import RunContext
from pydantic_ai.messages import BinaryContent, ToolReturn

from backend.agent.citations import Citation
from backend.agent.deps import Deps
from backend.agent.host import HostError

#: 一次 `paper_search` 最多返回几条（模型爱传 top_k=10，宿主必须自己封顶）
SEARCH_MAX = 12
#: 一次 `page_markdown_get` 最多读几页正文
TEXT_PAGES_MAX = 20
#: 片段长度上限：给全文就等于把"读哪页"的决定权收走
SNIPPET_CHARS = 200

_PAGES_RE = re.compile(r"^\s*(?P<a>\d+)\s*(?:[-~–]\s*(?P<b>\d+))?\s*$")


# --------------------------------------------------------------------------
# 工具实现用的小工具
# --------------------------------------------------------------------------


def parse_pages(spec: str, *, total: int, limit: int) -> tuple[list[int], bool]:
    """``"3"`` / ``"3-8"`` / ``"all"`` → 页码列表。返回 ``(页码, 是否被截断)``。"""
    text = (spec or "").strip().lower()
    if text in ("all", "*", "全部", "全文"):
        pages = list(range(1, max(total, 1) + 1))
    else:
        m = _PAGES_RE.match(text)
        if not m:
            return [], False
        a = int(m.group("a"))
        b = int(m.group("b")) if m.group("b") else a
        lo, hi = min(a, b), max(a, b)
        pages = list(range(max(1, lo), hi + 1))
    if total:
        pages = [p for p in pages if p <= total]
    return pages[:limit], len(pages) > limit


def _clip(text: str, limit: int = SNIPPET_CHARS) -> str:
    t = re.sub(r"\s+", " ", (text or "").strip())
    return t if len(t) <= limit else t[:limit - 1] + "…"


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


def _cached(ctx: RunContext[Deps], name: str, query: str, k: int) -> dict | None:
    """同一条检索重复调用时直接回上一次的结果（省一次后端往返，也提醒模型别绕）。"""
    hit = ctx.deps.query_cache.get(_query_key(name, query, k))
    if hit is None:
        return None
    return {**hit, "note": "这条检索与之前那条完全相同，结果没有变化；"
                          "请基于已有材料作答，或换一个真正不同的检索词。"}


def _remember(ctx: RunContext[Deps], name: str, query: str, k: int, payload: dict) -> None:
    ctx.deps.query_cache[_query_key(name, query, k)] = payload


def _stale_hint() -> str:
    """这一趟检索没有带来新依据 → 明确告诉模型"够了"，别把轮数耗在换词上。"""
    return ("这次检索没有带来**新的**依据（命中的都是已经拿到的那些片段）。"
            "请立刻基于已有材料作答；如果确实不够，就如实说材料不足。")


#: 检索预算用尽时的拒绝话术（**不打后端**，直接回给模型）
BUDGET_EXHAUSTED = {
    "ok": False,
    "error": "本轮的检索次数已经用尽。请立刻基于**已经拿到的材料**作答；"
             "材料不足就如实说明不够，不要再换检索词了。",
}


def _spend_search(ctx: RunContext[Deps]) -> bool:
    """扣一次检索预算（M7：检索预算与轮数预算分开扣）。"""
    return ctx.deps.spend_search()


def _register_chunk(ctx: RunContext[Deps], item: dict) -> Citation:
    """把一条检索命中登记进引用表，返回 citation。"""
    chunk_id = str(item.get("chunk_id") or "")
    section = item.get("section") or None
    page = int(item.get("page_no") or item.get("page") or 0) or None
    # 片段优先用译文：给用户看的引用表是中文语境；原文留给模型去读
    snippet = _clip(item.get("translation") or item.get("content") or "")
    return ctx.deps.registry.assign(
        key=f"chunk:{chunk_id}", kind="chunk",
        page=page, section=section, block_id=item.get("block_id"),
        snippet=snippet, label=_locate(page, section, item.get("chunk_type") or ""),
        ids=(chunk_id,) if chunk_id else ())


# --------------------------------------------------------------------------
# ① 检索：找位置，不读内容
# --------------------------------------------------------------------------


async def paper_search(ctx: RunContext[Deps], query: str, top_k: int = 8) -> dict:
    """在本论文内检索，返回**候选位置**（页码、章节、短片段），不返回全文。

    什么时候用：任何"这篇论文里怎么说的 / 讲了什么 / 为什么这么做"的问题，
    先用它定位；拿到结果里的 ref 与页码之后，再用 page_markdown_get 或 block_get
    读原文，用 page_image_get 看版面。

    参数：
      query  —— 检索词。用**论文里的说法**（术语、方法名）比用口语描述召回好。
      top_k  —— 最多几条，默认 8，上限 12。
    """
    async def _impl() -> dict:
        k = max(1, min(int(top_k or 8), SEARCH_MAX))
        cached = _cached(ctx, "paper_search", query, k)
        if cached is not None:
            return cached
        if not _spend_search(ctx):
            return dict(BUDGET_EXHAUSTED)
        before = len(ctx.deps.registry)
        data = await ctx.deps.host.search(ctx.deps.paper_id, query, top_k=k)
        raw_items = list(data.get("items") or [])
        total = int(data.get("page_total") or ctx.deps.page_total or 0)
        ctx.deps.page_total = ctx.deps.page_total or total
        items = []
        for raw in raw_items:
            cit = _register_chunk(ctx, raw)
            items.append({
                "ref": cit.ref,
                "page": cit.page,
                "section": cit.section,
                "type": raw.get("chunk_type"),
                "snippet": _clip(raw.get("content") or raw.get("translation") or ""),
            })
        out = {
            "query": query, "page_total": total or None, "returned": len(items),
            "items": items,
        }
        if not items:
            out["hint"] = ("本论文内没有检索到相关内容。可以换一个更贴近论文用词的说法；"
                           "仍无结果就如实说明这篇论文里找不到依据。")
        elif len(ctx.deps.registry) == before:
            out["hint"] = _stale_hint()
        else:
            out["hint"] = "这些只是片段。要引用具体内容，请用 page_markdown_get 读对应页。"
        _remember(ctx, "paper_search", query, k, out)
        return out

    return await _guarded(ctx, "paper_search",
                          {"query": _clip(query, 60), "top_k": top_k}, _impl())


# --------------------------------------------------------------------------
# ② 读内容：块级正文（先读后写那条约束只对写工具有效，这里纯读）
# --------------------------------------------------------------------------


async def page_markdown_get(ctx: RunContext[Deps], pages: str) -> dict:
    """读某几页的**块级正文**（原文；有译文时一并给出）。

    什么时候用：检索定位到页码之后读内容，或用户直接指定"第 5 页"。
    返回里每个块都有自己的 ref，回答时引用那个 ref。

    参数：
      pages —— 单页 ``"5"``、区间 ``"3-8"``、整篇 ``"all"``。整篇很长，
               只有确实需要通读时才用；一次最多 20 页，超了会被截断（返回 truncated=true）。
    """
    async def _impl() -> dict:
        wanted, clipped = parse_pages(pages, total=ctx.deps.page_total,
                                      limit=TEXT_PAGES_MAX)
        if not wanted:
            return {"ok": False, "error": "页码格式不对。写法示例：\"5\" / \"3-8\" / \"all\"。"}
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
                cit = ctx.deps.registry.assign(
                    key=f"block:{block.get('id')}", kind="block", page=page_no,
                    section=block.get("section") or None,
                    block_id=block.get("id"), row_id=block.get("row_id"),
                    snippet=_clip(text), label=_locate(page_no, block.get("section"),
                                                       block.get("type") or ""),
                    ids=(str(block.get("id") or ""),))
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


async def block_get(ctx: RunContext[Deps], ref: int) -> dict:
    """按 ref 读**单个块**的完整原文（含译文与公式 LaTeX）。

    什么时候用：读页结果里某段被截断、或某条检索片段看起来关键，想把这一段完整看清。

    参数：
      ref —— 检索或读页结果里给出的引用编号。
    """
    async def _impl() -> dict:
        cit = ctx.deps.registry.by_ref(ref)
        if cit is None or not cit.block_id:
            return {"ok": False,
                    "error": f"ref={ref} 不是可单独展开的块。请先用 paper_search 或 "
                             f"page_markdown_get 拿到带 ref 的块。"}
        data = await ctx.deps.host.block(ctx.deps.paper_id, cit.block_id)
        return {
            "ref": cit.ref, "page": data.get("page"), "section": data.get("section"),
            "type": data.get("type"),
            "text": str(data.get("text") or "").strip(),
            "translation": str(data.get("translation") or "").strip() or None,
            "formula": (data.get("formula") or {}).get("latex") or None,
        }

    return await _guarded(ctx, "block_get", {"ref": ref}, _impl())


# --------------------------------------------------------------------------
# ③ 看版面：公式/表格/插图这类 OCR 不可靠的东西
# --------------------------------------------------------------------------


async def page_image_get(ctx: RunContext[Deps], pages: str, dpi: int = 200):
    """把整页渲染成图片交给视觉模型看（字面意思：**你自己看这一页**）。

    什么时候用：公式的上下标/希腊字母认不准、表格结构看不懂、怀疑解析漏了内容、
    图文混排的版面说不清时。**不要**用它代替读正文 —— 正文用 page_markdown_get。

    参数：
      pages —— ``"5"`` / ``"3-4"``。一次最多 3 页。
      dpi   —— 默认 200（阅读器页图是 150，公式密集的页建议 300~400）。
               注意：**不要**靠放大低 dpi 图来"看得更清"，放大只是插值，信息量没变。
    """
    async def _impl():
        wanted, clipped = parse_pages(pages, total=ctx.deps.page_total,
                                      limit=ctx.deps.cfg.image_max_pages)
        if not wanted:
            return {"ok": False, "error": "页码格式不对。写法示例：\"5\" / \"3-4\"。"}
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


async def figure_list(ctx: RunContext[Deps], page: int) -> dict:
    """列出某一页的插图（图号、图注、可嵌入的受控图片地址）。

    什么时候用：要回答"图 3 画了什么 / 这张图说明了什么"之前，先看这一页有哪些图。

    参数：
      page —— 页码。不知道图在哪一页就先 paper_search（图注文字通常搜得到）。
    """
    async def _impl() -> dict:
        data = await ctx.deps.host.figures(ctx.deps.paper_id, int(page))
        items = []
        for fig in data.get("items") or []:
            url = fig.get("url") or ""
            cit = ctx.deps.registry.assign(
                key=f"figure:{page}:{fig.get('index')}", kind="figure", page=int(page),
                snippet=_clip(fig.get("caption") or ""),
                image_urls=[url] if url else [],
                label=f"第 {page} 页 · 图 {fig.get('number') or '?'}")
            items.append({
                "ref": cit.ref, "index": fig.get("index"),
                "number": fig.get("number") or "", "caption": _clip(fig.get("caption") or ""),
                "image_url": url,
            })
        out = {"page": int(page), "returned": len(items), "items": items}
        if not items:
            out["hint"] = "这一页没有插图（或图注没被识别）。可以换一页，或直接用 page_image_get 看版面。"
        else:
            out["hint"] = ("要在回答里配图，就把 image_url **逐字**写进 "
                           "![简短说明](image_url)；不要改写、拼接或换成别的地址。")
        return out

    return await _guarded(ctx, "figure_list", {"page": page}, _impl())


async def figure_image_get(ctx: RunContext[Deps], page: int, index: int):
    """取一张插图的裁图（按图注锚定重新裁剪，600dpi）交给视觉模型看。

    什么时候用：回答涉及图里的具体细节（曲线趋势、结构图、示意流程）时。

    参数：
      page  —— 页码。
      index —— ``figure_list`` 返回的 index（**不是**图号）。
    """
    async def _impl():
        raw, media = await ctx.deps.host.figure_image(ctx.deps.paper_id, int(page),
                                                      int(index))
        if not raw:
            return {"ok": False, "error": "取不到这张插图。"}
        cit = ctx.deps.registry.assign(key=f"figure_img:{page}:{index}", kind="figure",
                                       page=int(page),
                                       snippet=_clip(f"第 {page} 页插图 #{index}"),
                                       label=f"第 {page} 页 · 插图")
        note = f"已附上第 {page} 页的插图 #{index}（ref={cit.ref}）。"
        return ToolReturn(return_value=note, content=[note,
                          BinaryContent(data=raw, media_type=media or "image/png")])

    return await _guarded(ctx, "figure_image_get", {"page": page, "index": index}, _impl())


# --------------------------------------------------------------------------
# ④ P1：跨库 / 公式 / 译文 / 书架 / 笔记
# --------------------------------------------------------------------------


async def kb_search(ctx: RunContext[Deps], query: str, top_k: int = 8) -> dict:
    """在整个知识库里检索（**不限当前论文**），返回候选位置与它属于哪篇论文。

    什么时候用：用户问"我库里还有谁讲过这个 / 有没有别的论文提到 X"，
    或者当前这篇论文里找不到依据、想看看别的论文有没有时。

    **不要**用它回答"这篇论文怎么说的"——那要用 paper_search，只查当前论文。
    拿到结果后如果要读内容，用 `paper_id` + `page` 判断；当前这一篇可以直接用
    page_markdown_get，别的论文只能靠返回的片段说明，不要假装读过。

    参数：
      query  —— 检索词。
      top_k  —— 最多几条，默认 8，上限 12。
    """
    async def _impl() -> dict:
        k = max(1, min(int(top_k or 8), SEARCH_MAX))
        cached = _cached(ctx, "kb_search", query, k)
        if cached is not None:
            return cached
        if not _spend_search(ctx):
            return dict(BUDGET_EXHAUSTED)
        before = len(ctx.deps.registry)
        data = await ctx.deps.host.kb_search(query, top_k=k)
        items = []
        for raw in list(data.get("items") or [])[:ctx.deps.cfg.evidence_limit]:
            paper_id = str(raw.get("paper_id") or "")
            title = str(raw.get("paper_title") or "") or paper_id
            page = int(raw.get("page_no") or 0) or None
            section = raw.get("section")
            cit = ctx.deps.registry.assign(
                key=f"kb:{paper_id}:{raw.get('chunk_id')}", kind="chunk",
                paper_id=paper_id, page=page, section=section,
                block_id=raw.get("block_id"),
                snippet=_clip(raw.get("translation") or raw.get("content") or ""),
                label=" · ".join(x for x in (title, _locate(page, section)) if x),
                ids=(str(raw.get("chunk_id") or ""),))
            items.append({
                "ref": cit.ref, "paper_id": paper_id, "paper_title": title,
                "page": page, "section": section, "type": raw.get("chunk_type"),
                "is_current_paper": paper_id == ctx.deps.paper_id,
                "snippet": _clip(raw.get("content") or ""),
            })
        out = {"query": query, "returned": len(items), "items": items}
        if not items:
            out["hint"] = "整个知识库里都没有检索到相关内容。"
        elif len(ctx.deps.registry) == before:
            out["hint"] = _stale_hint()
        elif any(not i["is_current_paper"] for i in items):
            out["hint"] = ("这些是**别的论文**里的片段，只能作为线索。要下结论就得说清是哪篇论文；"
                           "只有 is_current_paper=true 的条目能用 page_markdown_get 展开。")
        else:
            out["hint"] = "命中都来自当前论文，可以直接 page_markdown_get 展开。"
        _remember(ctx, "kb_search", query, k, out)
        return out

    return await _guarded(ctx, "kb_search",
                          {"query": _clip(query, 60), "top_k": top_k}, _impl())


async def formula_get(ctx: RunContext[Deps], page: int | None = None,
                      query: str | None = None) -> dict:
    """取本论文的公式（LaTeX + 所在页 + 上下文）。

    什么时候用：用户问某个公式是什么意思、某个符号怎么定义、推导里的某一步；
    或者卷面里的公式被 OCR 认错、需要拿到 LaTeX 原文再核对。

    参数：
      page  —— 只看某一页（不确定页码就先 paper_search）。
      query —— 关键词过滤。**公式没有可用的编号字段**：论文自己的 `(3)` 编号在返回的
               `context` 里，所以按编号取只能传 `query="(3)"` 这样筛。
    两个都不传就返回全文公式（最多 40 条）。
    """
    async def _impl() -> dict:
        data = await ctx.deps.host.formulas(ctx.deps.paper_id, page=page, query=query)
        items = []
        for raw in data.get("items") or []:
            latex = str(raw.get("latex") or "")
            page_no = int(raw.get("page") or 0) or None
            cit = ctx.deps.registry.assign(
                key=f"formula:{page_no}:{raw.get('row_id')}:{latex[:24]}", kind="formula",
                page=page_no, row_id=raw.get("row_id"), snippet=_clip(latex, 160),
                label=_locate(page_no, None, "formula"),
                ids=(f"f{page_no:03d}",) if page_no else ())
            items.append({
                "ref": cit.ref, "page": page_no, "latex": latex,
                "display": bool(raw.get("display")),
                "context": raw.get("context") or "",
                "context_zh": raw.get("context_zh") or "",
            })
        out = {"total": data.get("total"), "returned": len(items),
               "source": data.get("source"), "items": items}
        if not items:
            out["hint"] = "这篇论文里没有提取到符合条件的公式（可能整篇没有公式，或筛选太窄）。"
        return out

    return await _guarded(ctx, "formula_get",
                          {"page": page, "query": _clip(query or "", 40)}, _impl())


async def translate_get(ctx: RunContext[Deps], pages: str) -> dict:
    """取某一页（或几页）的**中文译文**（按块）。

    什么时候用：用户问"这段中文怎么说的 / 译文对不对"，或者你要用中文复述原文、
    想确认某个术语在译文里的统一译法时。判断"译文是否忠实"时也要看它。

    参数：
      pages —— ``"5"`` / ``"3-8"`` / ``"all"``，一次最多 20 页。
    """
    async def _impl() -> dict:
        wanted, clipped = parse_pages(pages, total=ctx.deps.page_total,
                                      limit=TEXT_PAGES_MAX)
        if not wanted:
            return {"ok": False, "error": "页码格式不对。写法示例：\"5\" / \"3-8\" / \"all\"。"}
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
        if not out_pages:
            return {"ok": False, "truncated": clipped,
                    "error": f"第 {pages} 页还没有译文（这篇论文可能没翻译，或该页未翻译）。"}
        return {"page_total": ctx.deps.page_total or None, "truncated": clipped,
                "pages": out_pages,
                "note": f"另有 {missing} 个块没有译文（未翻译），已跳过。" if missing else ""}

    return await _guarded(ctx, "translate_get", {"pages": _clip(pages, 40)}, _impl())


async def library_list(ctx: RunContext[Deps], query: str | None = None) -> dict:
    """列出书架上的论文（按标题 / 作者模糊匹配），返回元数据。

    什么时候用：用户问"我库里有哪些关于 X 的论文 / 有没有讲过 Y 的文章"。
    这是**找论文**，不是找内容；挑出候选之后用 kb_search 去找具体段落。

    参数：
      query —— 关键词（标题或作者）。不传就按最近阅读顺序列前 30 篇。
    """
    async def _impl() -> dict:
        if not _spend_search(ctx):
            return dict(BUDGET_EXHAUSTED)
        data = await ctx.deps.host.library(q=query)
        items = []
        for raw in data.get("items") or []:
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
        out = {"total": data.get("total"), "returned": len(items), "items": items}
        if not items:
            out["hint"] = "书架上没有匹配的论文。可以换个关键词，或不带关键词看看全部。"
        return out

    return await _guarded(ctx, "library_list", {"query": _clip(query or "", 40)}, _impl())


async def notes_search(ctx: RunContext[Deps], query: str | None = None,
                       scope: str = "paper", kind: str = "all") -> dict:
    """检索用户自己的笔记 / 高亮 / 批注（第一人称证据）。

    什么时候用：用户问"我之前记过什么 / 我标注过哪里 / 我的笔记里怎么说的"。
    **这些是用户写的，不是论文写的** —— 回答时必须说清哪是原文、哪是用户笔记。

    参数：
      query —— 关键词（模糊匹配选中文本与笔记正文）。
      scope —— ``paper``（默认，只看当前论文）｜``all``（整个库）。
      kind  —— ``all``（默认）｜``notes``｜``highlights``｜``annotations``。
    """
    async def _impl() -> dict:
        paper_id = None if scope == "all" else ctx.deps.paper_id
        if not _spend_search(ctx):
            return dict(BUDGET_EXHAUSTED)
        data = await ctx.deps.host.notes(query=query, paper_id=paper_id, kind=kind)
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
                "note": raw.get("note") or "", "tags": list(raw.get("tags") or []),
            })
        out = {"scope": scope, "kind": kind, "returned": len(items), "items": items}
        if not items:
            out["hint"] = "没有找到匹配的笔记 / 高亮 / 批注。"
        return out

    return await _guarded(ctx, "notes_search",
                          {"query": _clip(query or "", 40), "scope": scope,
                           "kind": kind}, _impl())


#: `Agent(tools=...)` 用这一份；顺序即提示词里的出现顺序
P0_TOOLS = [paper_search, page_markdown_get, block_get, page_image_get,
            figure_list, figure_image_get]

#: P1 补齐的五个读工具
P1_TOOLS = [kb_search, formula_get, translate_get, library_list, notes_search]

#: 当前挂载的全部工具（P2 的写工具**不在这里**）
AGENT_TOOLS = [*P0_TOOLS, *P1_TOOLS]
