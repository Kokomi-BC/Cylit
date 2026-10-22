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

import hashlib
import re
import time

from pydantic_ai import RunContext
from pydantic_ai.messages import BinaryContent, ToolReturn

from backend.agent.citations import Citation
from backend.agent.deps import Deps
from backend.agent.host import HostError
from backend.agent import prompts
from backend.core.logging import get_logger

log = get_logger(__name__)

#: 一次 `paper_search` 最多返回几条（模型爱传 top_k=10，宿主必须自己封顶）
SEARCH_MAX = 12
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


def _register_chunk(ctx: RunContext[Deps], item: dict) -> Citation:
    """把一条检索命中登记进引用表，返回 citation。"""
    chunk_id = str(item.get("chunk_id") or "")
    paper_id = str(item.get("paper_id") or ctx.deps.paper_id)
    section = item.get("section") or None
    page = int(item.get("page_no") or item.get("page") or 0) or None
    # 片段优先用译文：给用户看的引用表是中文语境；原文留给模型去读
    snippet = _clip(item.get("translation") or item.get("content") or "")
    #: 这条命中覆盖的正文行：写侧把行 id 也塞进了 block_id，所以行身份从这里来
    row_ids = [str(rid) for rid in (item.get("row_ids") or []) if str(rid).strip()]
    row_id = str(item.get("row_id") or "") or (row_ids[0] if row_ids else "")
    # 兜底再判一次名字空间：即便上游把行 id 塞在 block_id 里（存量投影/别的调用方），
    # 也绝不让它进引用表的 block_id —— 那等于给 block_get 一个必然 404 的入参。
    raw_block = item.get("block_id")
    block_id = None if _row_id_of(raw_block) else raw_block
    if row_id and len(row_ids) > 1:
        # Citation 只存首行（前端契约不动）；整段跨度留给 block_get 拼
        ctx.deps.row_spans[row_id] = row_ids
    return ctx.deps.registry.assign(
        # 去重键用"落点"：block_id 可能是 None（行 id 形态已被剥掉），这时用行 id 顶上 ——
        # 少了它，两个**片段一模一样但页码不同**的段落会并成一个引用号
        key=_chunk_key(paper_id, block_id or row_id, chunk_id, snippet), kind="chunk",
        page=page, section=section, block_id=block_id,
        row_id=row_id or None,
        snippet=snippet, label=_locate(page, section, item.get("chunk_type") or ""),
        ids=(chunk_id,) if chunk_id else (),
        # 行身份（阶段 2b）：检索命中带过来的 pid，复用编号时由 assign 合并（不覆盖）。
        pids=list(item.get("pids") or []))


# --------------------------------------------------------------------------
# ① 检索：找位置，不读内容
# --------------------------------------------------------------------------


async def paper_search(ctx: RunContext[Deps], query: str, top_k: int = 8,
                      mode: str | None = None) -> dict:
    """在本论文内检索，返回**候选位置**（页码、章节、短片段），不返回全文。

    什么时候用：任何"这篇论文里怎么说的 / 讲了什么 / 为什么这么做"的问题，
    先用它定位；拿到结果里的 ref 之后，**先用 `block_get(ref)` 展开最相关的那几块**
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

    参数：
      query  —— 检索词。用**论文里的说法**（术语、方法名）比用口语描述召回好。
      top_k  —— 最多几条，默认 8，上限 12。
      mode   —— `text` / `rag` / `auto`；不传用服务端默认（auto，两路融合）。
    """
    async def _impl() -> dict:
        k = max(1, min(int(top_k or 8), SEARCH_MAX))
        key = k * 3 + _search_mode_slot(mode)
        cached = _cached(ctx, "paper_search", query, key)
        if cached is not None:
            return cached
        ctx.deps.count_search()
        before = len(ctx.deps.registry)
        data = await ctx.deps.host.search(ctx.deps.paper_id, query, top_k=k,
                                          mode=mode)
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
                "snippet": _snippet_around(raw.get("content") or raw.get("translation") or "",
                                           str(query or "")),
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
            out["hint"] = ("这些只是片段：先用 `block_get(ref)` 展开最相关的 2~4 块；"
                           "块里上下文不够时再 `page_markdown_get` 读它所在的页。")
        _remember(ctx, "paper_search", query, key, out)
        return out

    return await _guarded(ctx, "paper_search",
                          {"query": _clip(query, 60), "top_k": top_k,
                           "mode": mode or "auto"}, _impl())


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


async def block_get(ctx: RunContext[Deps], ref: int) -> dict:
    """按 ref 读**单个块/段**的完整原文（含译文与公式 LaTeX）。

    什么时候用：读页结果里某段被截断、或某条检索片段看起来关键，想把这一段完整看清。

    参数：
      ref —— 检索或读页结果里给出的引用编号。
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

    async def _impl() -> dict:
        cit = ctx.deps.registry.by_ref(ref)
        if cit is None:
            return {"ok": False,
                    "error": f"ref={ref} 不存在。只能用工具**实际返回过**的编号。"}
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
                    "error": f"ref={ref} 这一条没有可展开的正文（只有定位信息）："
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
    """列出某一页的**插图与表格**（编号、图注、可嵌入的受控图片地址）。

    什么时候用：要回答"图 3 画了什么 / 表 1 的数字是多少"之前，先看这一页有哪些图形区域。
    **表格也在这里**：解析出的表格正文经常把单元格粘成一串（`1645.2945.16`），
    拿数字要用 kind="table" 那条的 index 去 figure_image_get 看表图。

    参数：
      page —— 页码。不知道图/表在哪一页就先 paper_search（图注文字通常搜得到）。
    """
    async def _impl() -> dict:
        data = await ctx.deps.host.figures(ctx.deps.paper_id, int(page))
        rows = _caption_rows(ctx.deps.paper_id, int(page))
        items = []
        for fig in data.get("items") or []:
            url = fig.get("url") or ""
            kind = str(fig.get("kind") or "figure")
            # 算法块也算"可看的内容区"（2026-12-07）：标签别写成"图 1"，那是伪代码
            label = ({"table": "表 ", "algorithm": "算法 "}.get(kind, "图 ")
                     + str(fig.get("number") or "?"))
            cit = ctx.deps.registry.assign(
                key=f"figure:{page}:{fig.get('index')}", kind=kind, page=int(page),
                snippet=_clip(fig.get("caption") or "", CAPTION_CHARS),
                # 图注行的行 id：前端点 `[n]` 才能闪到那一行（模型侧看不到这个 id）
                row_id=rows.get(str(fig.get("number") or "")),
                image_urls=[url] if url else [],
                label=f"第 {page} 页 · {label}")
            items.append({
                "ref": cit.ref, "index": fig.get("index"), "kind": kind,
                "number": fig.get("number") or "",
                "caption": _clip(fig.get("caption") or "", CAPTION_CHARS),
                "image_url": url,
            })
        out = {"page": int(page), "returned": len(items), "items": items}
        if not items:
            out["hint"] = "这一页没有插图或表格（或图注没被识别）。可以换一页，或直接用 page_image_get 看版面。"
        else:
            out["hint"] = ("看内容用 figure_image_get(page, index)；要在回答里配图，就把 image_url "
                           "**逐字**写进 ![简短说明](image_url)，不要改写、拼接或换成别的地址。")
        return out

    return await _guarded(ctx, "figure_list", {"page": page}, _impl())


async def figure_image_get(ctx: RunContext[Deps], page: int, index: int):
    """取一页里某个图形区域的裁图（插图或**表格**，按图注锚定 600dpi）交给视觉模型看。

    什么时候用：回答涉及图里的具体细节（曲线趋势、结构图）或**表格里的数字**时。
    表格的数字**不要**从 page_markdown_get 的正文里抄 —— 单元格常常粘在一起。

    参数：
      page  —— 页码。
      index —— ``figure_list`` 返回的 index（**不是**图号/表号）。
    """
    async def _impl():
        raw, media = await ctx.deps.host.figure_image(ctx.deps.paper_id, int(page),
                                                      int(index))
        if not raw:
            return {"ok": False, "error": "取不到这个区域的裁图。"}
        kind, number = "figure", ""
        try:                                   # 顺手取编号，让 ref 的标题写成"表 1"而不是"插图 #0"
            data = await ctx.deps.host.figures(ctx.deps.paper_id, int(page))
            hit = next((i for i in (data.get("items") or [])
                        # ⚠️ 别写 `i.get("index") or -1`：序号 0 是 falsy，会被吃成 -1，
                        # 于是第 0 个区域永远匹配不上（踩过：表 1 的 ref 标题退回"图 #0"）
                        if i.get("index") is not None
                        and int(i["index"]) == int(index)), None)
            if hit:
                kind = str(hit.get("kind") or "figure")
                number = str(hit.get("number") or "")
        except Exception as exc:  # noqa: BLE001 —— 拿不到编号不影响看图
            log.debug("取区域编号失败（不影响看图）：%s", exc)
        what = ("表 " if kind == "table" else "图 ") + (number or f"#{index}")
        cit = ctx.deps.registry.assign(key=f"figure_img:{page}:{index}", kind=kind,
                                       page=int(page),
                                       # 图注行的行 id：前端点 `[n]` 闪到那一行
                                       row_id=_caption_rows(ctx.deps.paper_id,
                                                           int(page)).get(number),
                                       snippet=_clip(f"第 {page} 页 · {what}"),
                                       label=f"第 {page} 页 · {what}")
        note = f"已附上第 {page} 页的{what}（ref={cit.ref}）。"
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
        ctx.deps.count_search()
        before = len(ctx.deps.registry)
        data = await ctx.deps.host.kb_search(query, top_k=k)
        items = []
        for raw in list(data.get("items") or [])[:ctx.deps.cfg.evidence_limit]:
            paper_id = str(raw.get("paper_id") or "")
            title = str(raw.get("paper_title") or "") or paper_id
            page = int(raw.get("page_no") or 0) or None
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
                "snippet": _snippet_around(raw.get("content") or "", str(query or "")),
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
                      query: str | None = None,
                      include_inline: bool = False) -> dict:
    """取本论文的**块级公式**（LaTeX + 页码），默认来自该页 Markdown 正文。

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
    回执里的 `display_total` / `inline_total` / `total` **同一口径**：都数「去重后、在筛选范围
    内的全部条目」，`total == display_total + inline_total` 恒成立；`returned` 是本次给了几条，
    `omitted_inline` 是这次按需过滤掉的行内条数（默认就等于 `inline_total`）。
    本次要列的条目超过 40 条时 `truncated: true`。

    ⚠️ `partial=True` 的条目**LaTeX 可能只是公式的一段**（一条公式被切成了几段，我们只拿到
    第一段）。要确认全文就用 `page_image_get(page)` 看那一页的原版面
    （阅读器里这个位置本来也有公式裁图）——**不要照着半条 LaTeX 讲整条公式**。
    """
    async def _impl() -> dict:
        data = await ctx.deps.host.formulas(ctx.deps.paper_id, page=page, query=query,
                                           include_inline=include_inline)
        items = []
        for raw in data.get("items") or []:
            latex = str(raw.get("latex") or "")
            page_no = int(raw.get("page") or 0) or None
            cit = ctx.deps.registry.assign(
                key=f"formula:{page_no}:{raw.get('row_id')}:{_latex_key(latex)}", kind="formula",
                page=page_no, row_id=raw.get("row_id"), snippet=_clip(latex, 160),
                label=_locate(page_no, None, "formula"),
                ids=(f"f{page_no:03d}",) if page_no else ())
            items.append({
                "ref": cit.ref, "page": page_no, "latex": latex,
                "display": bool(raw.get("display")),
                "grade": raw.get("grade"),
                # LaTeX 只是公式的一段 → 要讲全貌就去看原版面
                "partial": bool(raw.get("partial")),
                "source": raw.get("source"),
                "context": raw.get("context") or "",
                "context_zh": raw.get("context_zh") or "",
            })
        display_total = int(data.get("display_total") or 0)
        inline_total = int(data.get("inline_total") or 0)
        #: 这次**按需过滤掉**的行内条数。上游没给（老回执 / 测试桩）时按"默认不列行内"
        #: 兜底：否则回执里一边说"另有 N 条行内公式"、一边计数是 0，两个数自相矛盾。
        omitted_inline = data.get("omitted_inline")
        if omitted_inline is None:
            omitted_inline = 0 if include_inline else inline_total
        out = {"total": data.get("total"), "returned": len(items),
               "source": data.get("source"),
               "truncated": bool(data.get("truncated")),
               # 默认只给块级（完整、可引用）；行内那批按需取
               "display_total": display_total,
               "inline_total": inline_total,
               "omitted_inline": int(omitted_inline or 0),
               "items": items}
        if not items:
            out["hint"] = ("这篇论文里没有提取到符合条件的公式（可能整篇没有公式，或筛选太窄）。"
                           "行内符号可以先用 page_markdown_get 看那一页的正文。")
        else:
            # 五条提示可以**同时成立**（默认只给块级 + 来源含解析兜底 + 全是转录层没有 grade
            # + 有条目只是片段 + 超了上限），拼在一起给，别用 elif 让后面的把前面的吞掉
            hints: list[str] = []
            if not include_inline and out["omitted_inline"] > 0:
                hints.append(f"这些是**块级公式**（完整，可引用）；另有 "
                             f"{out['omitted_inline']} 条行内公式（单符号 / 碎片那种）。"
                             f"查符号定义、单点表达式时用 `include_inline=True`，"
                             f"或者直接 `page_markdown_get` 读那一页看上下文。")
            if include_inline and any(one["display"] for one in items):
                # 用户实测的困惑：传了 include_inline 以后，完整那几条被几十条行内碎片包住，
                # 看起来"块级公式跑到列表末尾去了"。它们确实是文档顺序，只是前面全是碎片。
                hints.append("完整那几条就在这批里（`display: true`），按文档顺序夹在行内碎片"
                             "中间 —— 只想看它们就别传 `include_inline`。")
            if out.get("source") != "markdown":
                hints.append("本次列表含**版面解析兜底**的条目（source=parse/mixed）——"
                             "那几条的 LaTeX 可能只是公式的一段，讲全貌前先用 "
                             "`page_image_get(page)` 核对原版面。")
            if all(one["grade"] is None for one in items):
                hints.append("这些公式来自**转录层**，没有 A/B/C 分级（`grade` 是解析卡的字段，"
                             "不是缺数据）。")
            if any(one["partial"] for one in items):
                hints.append("有条目 partial=true：那几条的 LaTeX 只是公式的一段 —— "
                             "要对着讲就把 `page_image_get(page)` 的原版面拿来核对。")
            if out["truncated"]:
                hints.append(f"全文共 {out['total']} 条，这里只给了前 {len(items)} 条（按文档顺序）；"
                             f"要更全就**按页取**（传 page）。")
            if hints:
                out["hint"] = " ".join(hints)
        return out

    return await _guarded(ctx, "formula_get",
                          {"page": page, "query": _clip(query or "", 40),
                           "include_inline": include_inline}, _impl())


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
                # 读过这一页的译文也算"读过这一页"：`page_markdown_edit` 的先读后写判据看它
                ctx.deps.pages_read.append(page_no)
        if not out_pages:
            return {"ok": False, "truncated": clipped,
                    "error": f"第 {pages} 页还没有译文（这篇论文可能没翻译，或该页未翻译）。"}
        return {"page_total": ctx.deps.page_total or None, "truncated": clipped,
                "pages": out_pages,
                "note": f"另有 {missing} 个块没有译文（未翻译），已跳过。" if missing else ""}

    return await _guarded(ctx, "translate_get", {"pages": _clip(pages, 40)}, _impl())


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


async def library_list(ctx: RunContext[Deps], query: str | None = None) -> dict:
    """列出书架上的论文（按标题 / 作者模糊匹配），返回元数据。

    什么时候用：用户问"我库里有哪些关于 X 的论文 / 有没有讲过 Y 的文章"。
    这是**找论文**，不是找内容；挑出候选之后用 kb_search 去找具体段落。

    参数：
      query —— 关键词（标题或作者）。不传就按最近阅读顺序列前 30 篇。
    """
    async def _impl() -> dict:
        ctx.deps.count_search()
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

    **一条笔记挂在一条高亮上**（`Highlight └── Note`）：所以一条笔记天然同时带着
    `selected_text`（用户**选中的原文**）与 `note`（用户**写的正文**）两个字段 ——
    引用时别把 `selected_text` 当成用户说的话，那是论文的句子；用户说的话在 `note` 里。

    参数：
      query —— 关键词（**选中文本**与**笔记正文**都参与过滤，模糊匹配、大小写不敏感）。
               `kind="all"` 时不该有"与关键词无关的高亮"混进结果里、把命中的笔记挤掉。
      scope —— ``paper``（默认，只看当前论文）｜``all``（整个库）。
      kind  —— ``all``（默认）｜``notes``（只要笔记正文）｜``highlights``（只要高亮）｜
               ``annotations``（AI 批注）。`note` 为空 = 这条高亮上**没写过笔记**，
               不是"笔记正文丢了"。
    """
    async def _impl() -> dict:
        paper_id = None if scope == "all" else ctx.deps.paper_id
        ctx.deps.count_search()
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
                "note": raw.get("note") or "",
                # 笔记自己的 id（上游肯给才给）：模型据此说清"我在讲哪一条笔记"
                "note_id": str(raw.get("note_id") or raw.get("id") or ""),
                "tags": list(raw.get("tags") or []),
            })
        out = {"scope": scope, "kind": kind, "returned": len(items), "items": items}
        if not items:
            out["hint"] = "没有找到匹配的笔记 / 高亮 / 批注。"
        elif kind != "notes" and all(not one["note"] for one in items):
            # "笔记正文永远拿不到"这条投诉就出在这一步：回执全是高亮、note 全空，
            # 模型会以为**检索坏了**，而不是"这些高亮上本来就没写笔记"
            out["hint"] = ("这些是高亮，没有附笔记 —— `note` 为空表示这条高亮上"
                           "**没写过笔记**（不是查不到正文）。要看笔记正文用 "
                           "kind='notes'；`query` 会同时过滤选中原文与笔记正文。")
        return out

    return await _guarded(ctx, "notes_search",
                          {"query": _clip(query or "", 40), "scope": scope,
                           "kind": kind}, _impl())


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


async def page_markdown_edit(ctx: RunContext[Deps], op: str, page: int, side: str,
                             find: str = "", replace: str = "",
                             reason: str = "") -> dict:
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

    改之前请先确认"哪个才是对的"：**OCR 认错**要对着 `page_image_get` 的原版面核对；
    **译文错**要对着原文核对（`page_markdown_get` 一次给两侧）。拿不准就先问用户，
    不要凭猜测改。改完在回答里逐条报告：第几页、哪一侧、原来是什么、改成了什么。

    参数：
      page  —— 页码。
      side  —— `"original"`（A 路英文转录 / OCR 结果）｜`"translation"`（B 路中文译文）。
      find  —— 要改掉的**原文**（必须与 view 的结果逐字一致，且整页唯一）。
      replace —— 改成什么（空串 = 删除这一处）。
      reason —— 一句话说明为什么改（写进改动日志，便于用户回看）。
    """
    async def _impl() -> dict:
        page_no = int(page)
        key = _side_key(side)
        what = "view" if op in ("view", "read", "show") else str(op or "").strip().lower()

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
            return {"ok": False,
                    "error": "元数据已经提交过一次了。要改哪一项就把它带上一起重发一次；"
                             "不要分多次提交（那会让后面的判断和前面写下的互相矛盾）。"}
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


async def highlight_manage(ctx: RunContext[Deps], op: str, anchor: str = "", text: str = "",
                           color: str = "", side: str = "", occurrence: int = 1,
                           highlight_id: str = "", query: str = "", reason: str = "") -> dict:
    """给这篇论文的**高亮**做增删改查、以及"这条高亮在哪"。

    高亮是"用户在某一面上标出来的一段文字"，也是**笔记的锚点**（`Highlight └── Note`）。
    三面各有各的位置模型（正文行 / Markdown 视图 / 原 PDF 页图）—— 你只需要给
    `anchor`（哪一面）+ `text`（要标的**原文**），偏移量由宿主算，不要自己编数字。

    ⚠️ **默认标在"正文行"那一面**：用户读的对照 / 译文视图画的就是它。**但行 id 你拿不到** ——
    `page_markdown_get` / `block_get` 的返回里都没有 `row_id`，**不要自己编 `p009-08` 这种**。
    给 `md:pN`：宿主会**尽量把它挪到这一页对应的正文行**上（见 `annotations.py` 的
    `locate_in_rows`），挪不动才留在 Markdown 面；`md:pN` 的高亮只在 Markdown 视图里出现，
    用户在你面前看不到（实测被报过"高亮没出现"）。原 PDF 那面服务端算不出偏移量（文字层在前端）。

      op="create"  anchor + text（+ color / side / occurrence）→ 建一条高亮。
                   **color 可以是你想要的任何颜色**（用户口径 2026-11-26）：色名 red/orange/yellow/
                   green/blue/indigo/violet 之外，直接给 `#rrggbb`（或 #rgb / rgb() / hsl()）也行。
                   ⚠️ **一律用多彩、尽量不一样**（用户口径 2026-12-06）：一片同色等于没有颜色。
                   做法：动手前先 `op=list` 看这篇已经用过哪些颜色，**挑没出现过/用得最少**的那个；
                   一次标好几处就**逐个换色**，别整批一个色。
                   ✅ **颜色代码可以由你自己给**（2026-12-06 明确）：`#0d9488` / `#f59e0b` /
                   `rgb(20,120,140)` / `hsl(180 60% 40%)` 都能如实存下并原样画出 —— 想按语义
                   自己设计一套配色（这套论文用 teal 系、那套用 indigo 系）就直给 hex，不必迁就
                   那七个色名。常见颜色词（teal / cyan / amber / rose / slate …）也会被认成 hex，
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
      op="locate"  highlight_id → 这条高亮挂在哪一面、第几页、原文是什么（还能顺带看它有没有笔记）。
      op="update"  highlight_id + color（改颜色）或 text（改引用文字）。**改不了位置**：
                   要换个地方标就删了重建。
      op="delete"  highlight_id + reason → 删一条高亮（跨行/跨页的整组一起删，
                   **它挂着的笔记也会一起删**）。删之前先 `op="locate"` 确认是那一条；
                   用户没明确说"删掉这处高亮"就不要删。

    与"解释这段内容"的分工：解释是**读**（不需要动数据）；只有用户说"把这段标出来 / 加个高亮"
    才用本工具写。写完在回答里说清标在哪一页、哪一段文字。
    """
    async def _impl() -> dict:
        what = str(op or "").strip().lower()
        writes = {"create", "add", "new", "update", "delete", "remove"}
        if what in writes:
            ctx.deps.count_edit()
        if what in ("create", "add", "new"):
            data = await ctx.deps.host.highlight_create(ctx.deps.paper_id, {
                "anchor": str(anchor or ""), "text": str(text or ""),
                "color": str(color or "yellow"),
                # **原样透传**：`en` / `zh` 是库里的侧别词表，翻译成 `original` / `translation`
                # 反而会被 `create_highlight` 当成不认识而回落成 `en` —— 用户要标中文，高亮却画在
                # 英文面（agent 报过"未产生高亮"）。空着则宿主按文字自动判（中文 / 公式 → 译文面）。
                "side": str(side or "").strip().lower(),
                "occurrence": int(occurrence or 1)})
            if data.get("ok"):
                hl = data.get("highlight") or {}
                out = {"ok": True, "highlight_id": hl.get("id"), "page": data.get("page"),
                       "scope": data.get("scope"), "text": hl.get("text"),
                       "color": hl.get("color"),
                       "note": "已建立高亮。要给它写笔记就用 note_manage op=create。"}
                # 宿主的两条"别忽略我"的信号必须原样带出去：这里是白名单字典，新增字段
                # 不显式搬一遍就会被丢掉（2026-12-07 实测：跨块落在 Markdown 面的高亮
                # 明明带了 warning，模型侧一个字都没看到）。
                for extra in ("warning", "side_note", "text_note"):
                    if data.get(extra):
                        out[extra] = data[extra]
                return out
            # 失败时把"最接近的原文"贴进 error：模型改对引文只能靠它（near_miss 也原样带上）
            near = [str(item) for item in (data.get("near_miss") or [])][:2]
            error = str(data.get("error") or "建立高亮失败")
            if near:
                error = f"{error}最接近的是：{' / '.join(near)}（照着它逐字改 text 再试一次）"
            return {**data, "error": error}
        if what in ("list", "search"):
            return await ctx.deps.host.highlights(ctx.deps.paper_id, query=str(query or ""))
        if what in ("locate", "info", "show"):
            if not highlight_id:
                return {"ok": False, "error": "locate 要给 highlight_id（先用 op=list 拿）。"}
            return await ctx.deps.host.highlights(ctx.deps.paper_id,
                                                  highlight_id=str(highlight_id))
        if what in ("update", "color", "set"):
            if not highlight_id:
                return {"ok": False, "error": "update 要给 highlight_id。"}
            payload: dict = {}
            if str(color or "").strip():
                payload["color"] = str(color).strip()
            if str(text or "").strip():
                payload["selected_text"] = str(text).strip()
            if not payload:
                return {"ok": False, "error": "update 至少要给 color 或 text。"}
            got = await ctx.deps.host.highlight_update(str(highlight_id), payload)
            return {"ok": True, "highlight": got}
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
                      content: str = "", query: str = "", reason: str = "") -> dict:
    """给**高亮**写笔记 / 查笔记 / 改笔记 / 删笔记 / 看这条笔记对应的高亮。

    关系是固定的：`Highlight └── Note` —— **笔记必须挂在一条高亮上**（高亮是它的位置锚点），
    所以 `op="create"` 一定要 `highlight_id`；没有高亮就先用 `highlight_manage op=create`
    把那段文字标出来，别建"孤儿笔记"（用户口径）。

      op="create"  highlight_id + content（Markdown）→ 加笔记。
      op="list"    query（可选）→ 列这篇的笔记（每条带**它挂靠的高亮原文**与页码）。
      op="locate"  note_id → 这条笔记挂在哪一面的哪一处（"定位到某条笔记对应的原文"）。
      op="update"  note_id + content → 改正文。
      op="delete"  note_id + reason → 删笔记（**高亮不动**：笔记只是附注）。

    正文用 Markdown 写（支持 `$公式$`）。写完在回答里说清"挂在哪一处高亮上"。
    """
    async def _impl() -> dict:
        what = str(op or "").strip().lower()
        if what in {"create", "add", "new", "update", "delete", "remove"}:
            ctx.deps.count_edit()
        if what in ("create", "add", "new"):
            if not highlight_id:
                return {"ok": False, "error": "create 要给 highlight_id：笔记必须挂在一处高亮上"
                                              "（先用 highlight_manage op=create 标出来）。"}
            if not str(content or "").strip():
                return {"ok": False, "error": "create 要给 content（笔记正文）。"}
            data = await ctx.deps.host.note_create(ctx.deps.paper_id, {
                "highlight_id": str(highlight_id), "content": str(content)})
            note = data.get("note") or {}
            return {"ok": True, "note_id": note.get("id"), "page": note.get("page"),
                    "scope": note.get("scope"), "quote": note.get("selected_text")}
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


async def web_search(ctx: RunContext[Deps], query: str, count: int | None = None,
                     need_content: bool | None = None, question: str = "") -> dict:
    """联网搜索公开网页（DeepSeek 官方搜索），返回标题 / 链接 / 站点 / 摘要。

    什么时候用：问题问的是**论文之外**的公开信息 —— 某个方法的官方文档或开源实现、
    数据集在哪下载、这篇论文是否已发表、某个术语业界现在怎么说；或者用户直接让你
    "上网查一下"。需要论文里的论断时**不要**用它：那是 paper_search / kb_search 的活，
    网页不是论文原文。

    参数：
      query  —— 搜索词。支持 `site:arxiv.org`（限站）、`-site:csdn.net`（排除）、
                `"精确短语"`、`after:2025-01-01`（限时间）这些语法；多个站点用 `OR` 连接。
                一次只查一件事，别把好几个问题塞进一个 query。
      count  —— 要几条，1~50。不传就用设置里的默认（现在是 8）。
      need_content —— 是否让每条结果带上网页正文。不传就用设置里的默认（现在是只取摘要）：
                正文动辄上万字，只想看某一篇时用 web_fetch 更省。
      question —— 触发这次搜索的用户原话，帮搜索引擎理解意图；一般值得填。
    """
    async def _impl() -> dict:
        key = int(count or 0) * 3 + _need_content_slot(need_content)
        cached = _cached(ctx, "web_search", query, key)
        if cached is not None:
            return cached
        ctx.deps.count_search()
        data = await ctx.deps.host.web_search(
            query, count=count, need_content=need_content, ori_question=question)
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
        out = {"query": query, "provider": data.get("provider"),
               "returned": len(items), "items": items}
        if not items:
            out["hint"] = ("没有搜到结果：换一组关键词（去掉 `site:` 限制、少用引号）再试一次；"
                           "还是一条都没有，就如实跟用户说没查到，不要凭记忆编。")
        else:
            out["hint"] = ("这些是**外部网页**，不是论文里的材料：回答里引用它们必须写"
                           "`[标题](链接)` 并带上站点名；要看某一篇的正文就说清是哪一条、"
                           "用 web_fetch 读，不要拿摘要冒充读过全文。")
        _remember(ctx, "web_search", query, key, out)
        return out

    return await _guarded(ctx, "web_search", {
        "query": _clip(query, 60), "count": count,
    }, _impl())


async def web_fetch(ctx: RunContext[Deps], url: str, max_chars: int | None = None) -> dict:
    """把一个网页抓成 Markdown 正文（DeepSeek 官方抓取），按需读全文。

    什么时候用：web_search 的结果里有一条看起来正是你要的依据、摘要不够判断；
    或者用户直接给了一个链接让你看内容。**一次只抓真正要看的那一两篇** —— 抓回来的
    正文很占上下文，抓五篇等于把前面的材料全挤掉。

    不要抓：搜索结果页、站点首页、要登录才能看的地址（会报抓不到）。

    参数：
      url       —— 完整的 http/https 地址（web_search 返回的 url 原样传即可）。
      max_chars —— 正文最多保留多少字符。不传就用设置里的默认（现在是 6000）；
                   超出部分会被丢掉，返回里 truncated=true。
    """
    async def _impl() -> dict:
        key = int(max_chars or 0)
        cached = _cached(ctx, "web_fetch", url, key)
        if cached is not None:
            return cached
        ctx.deps.count_search()
        data = await ctx.deps.host.web_fetch(url, max_chars=max_chars)
        content = str(data.get("content") or "")
        out = {
            "url": data.get("url") or url,
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
        _remember(ctx, "web_fetch", url, key, out)
        return out

    return await _guarded(ctx, "web_fetch", {
        "url": _clip(url, 120), "max_chars": max_chars,
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

#: 联网（自带技能 `web-search` 的手脚）：**永远挂载**，没配密钥时返回一句人话错误。
#: 为什么不做成"有密钥才挂"：工具表一变，system 里那份工具说明与函数 schema 也跟着变，
#: 一个设置项就能把整段提示词前缀打掉；而且模型不知道"现在能不能联网"只会少问一句。
WEB_TOOLS = [web_search, web_fetch]

#: 当前挂载的全部工具
AGENT_TOOLS = [*P0_TOOLS, *P1_TOOLS, *EDIT_TOOLS, *ANNOTATION_TOOLS, *WEB_TOOLS]

#: **元数据填写**那一次会话用的工具：只读的读全文能力 + 一个写口。
#: 为什么单独一份、不并进 `AGENT_TOOLS`：问答会话里模型顺手改元数据的场景并不存在，
#: 而"少一个写口"就是少一类越权。
META_TOOLS = [paper_search, page_markdown_get, block_get, paper_meta_write]
