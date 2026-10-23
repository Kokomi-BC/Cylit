"""给 Agent 用的**只读投影**（工具面唯一的数据出口，规划 §7.5）。

这个模块存在的理由就是"什么东西能被 agent 看到只有一个审批点"：

- 检索：不发 `retrieval` 元数据（`vector_hits/fused/rrf_k/rerank.degraded` 是给我们排查用的）、
  不发 `scores`、不发 bbox；
- 读页：按**块**返回，并带上块在正文行里的锚点（`row_id`），引用才能跳到高亮那一行；
- 插图：`url` 一律取 `figure_regions.media_of_page` 造好的那条（带 `v=<区域哈希>`），
  **不自己拼地址** —— 拼出来的地址没有版本串，区域算法一变就会拿浏览器缓存里的旧图。

⚠️ 本模块属于**主后端**，不能 import `backend.agent.*`：那样 pydantic-ai 会进入主后端的
导入图，"独立进程"的意义就没了。所以页码解析这类小函数在这里独立实现。
"""

from __future__ import annotations

import re

from backend.core.errors import NotFoundError, ValidationError
from backend.services.knowledge import retrieval
from backend.services.paper import anchor_map, paper_service, reader_service

#: 一次投影最多给几页（agent 侧还会再截一次）
PAGES_MAX = 30
#: 检索片段给模型看的长度
SNIPPET_CHARS = 400
#: 图注给模型看的长度上限：拼接后的完整图注最长约 700 字符，400 太重（会把句子截在半句）
CAPTION_CHARS = 800

_PAGES_RE = re.compile(r"^\s*(?P<a>\d+)\s*(?:[-~–]\s*(?P<b>\d+))?\s*$")

#: 页级**行 id**（阅读器正文的一行）：`p017-00`；公式锚点还会带一个 `row-` 前缀
#: （`row-p017-02`，见 `knowledge_service` 里造公式 chunk 那段）。
#:
#: 为什么需要判它：写侧把 `chunk.block_id` 填的是 `first["id"]`，而那是**行 id** ——
#: 读侧拿它去 `sess.get(Block, …)` 必然 `NOT_FOUND`（实测 633 条 chunk 里 614 条中招，
#: 正文页与附录页一视同仁）。所以下发时要分名字空间：**行 id 走 `row_id`，只有真 Block id
#: 才进 `block_id`**，别让下游拿行 id 去查块表。
_ROW_ID_RE = re.compile(r"^p\d{3,}-\d{2}$")
_ROW_PREFIX = "row-"


def as_row_id(raw: object) -> str:
    """`p017-02` / `row-p017-02` → `p017-02`；不像行 id 就返回空串。"""
    text = str(raw or "").strip()
    if text.startswith(_ROW_PREFIX):
        text = text[len(_ROW_PREFIX):]
    return text if _ROW_ID_RE.match(text) else ""


def anchor_ids(item: dict) -> dict:
    """一条检索命中该下发哪些 id（行 / 块分开，见上面 `_ROW_ID_RE` 的说明）。

    metadata 里本来就有 `row_ids` / `row_span` / `pages` —— 以前只下发 block_id，
    等于把"这条命中到底覆盖了哪几行"这个信息丢了，模型只能拿到一个查不到的块 id。
    """
    meta = item.get("metadata") or {}
    rows = [rid for rid in (as_row_id(x) for x in (meta.get("row_ids") or [])) if rid]
    span = meta.get("row_span") or []
    if not rows and span:
        rows = [rid for rid in (as_row_id(x) for x in span) if rid]
    #: 写侧塞在 block_id 里的那个值：是行 id 形态就不能当 Block id 用
    row_form = as_row_id(item.get("block_id"))
    page = int(item.get("page_no") or 0) or next(
        (int(x) for x in (meta.get("pages") or []) if int(x or 0)), 0)
    return {
        # 真 Block id（不是行 id 形态）才下发，否则下游必然 NOT_FOUND
        "block_id": None if row_form else (item.get("block_id") or None),
        "row_id": rows[0] if rows else None,
        "row_ids": rows or None,
        "page_no": page or None,
    }
#: Markdown 图片。**必须过滤**：页级产物里的图片行写的是相对路径
#: （``../figures/<paper>-p03-x01.png``），模型照着嵌进回答就是一个前端打不开的地址
#: （实测模型确实在权衡要不要用它）。只保留受控的 ``/api/v1/papers/...`` 形式。
_MD_IMAGE_RE = re.compile(r"!\[(?P<alt>[^\]]*)\]\((?P<url>[^)\s]+)[^)]*\)")


def _neutralize_images(text: str) -> str:
    """把非受控图片地址换成一句说明，别让模型照着嵌。"""

    def repl(m: re.Match) -> str:
        url = m.group("url")
        if url.startswith("/api/v1/papers/"):
            return m.group(0)
        alt = (m.group("alt") or "").strip() or "插图"
        return f"（此处插图：{alt}；要配图请用 figure_list 取受控地址）"

    return _MD_IMAGE_RE.sub(repl, text or "")


def _clip(text: str | None, limit: int = SNIPPET_CHARS) -> str:
    t = re.sub(r"\s+", " ", (text or "").strip())
    return t if len(t) <= limit else t[:limit - 1] + "…"


def clip_around(text: str | None, query: str, limit: int = SNIPPET_CHARS) -> str:
    r"""片段**以命中处为中心**截（默认从块首截，命中落在块尾就整段证据都截掉了）。

    实测 2026-12-07：搜 `QR decomposition`、`i.i.d Normal entries`，命中的是第 5 页那段**结尾**
    的句子，可下发片段是块首「## 1.1 MSE OPTIMAL TURBOQUANT Let $x \in …$」—— 400 字符的窗口
    从块首起算，命中处根本不在窗口里，回执看着像"没搜到"。这里改成围着命中处取窗口。

    ⚠️ 与 `backend/agent/tools.py` 的 `_snippet_around` **不能合并**：那边跑在 agent 进程里，
    不能 import 服务层的 DB 依赖；它拿到的已经是这里中心化过的文本，再夹一次是无害的。
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
    inner = max(1, limit - 2)
    start = max(0, min(at - inner // 3, len(body) - inner))
    end = min(len(body), start + inner)
    return ("…" if start else "") + body[start:end] + ("…" if end < len(body) else "")


def parse_pages(spec: str, total: int, limit: int = PAGES_MAX) -> list[int]:
    """``"3"`` / ``"3-8"`` / ``"all"`` / ``"3,5,7"`` → 页码列表（越界与超量都截断）。"""
    text = (spec or "").strip().lower()
    if not text:
        return []
    if text in ("all", "*", "全部", "全文"):
        return list(range(1, max(total, 1) + 1))[:limit]
    pages: list[int] = []
    for part in text.split(","):
        m = _PAGES_RE.match(part)
        if not m:
            continue
        a = int(m.group("a"))
        b = int(m.group("b")) if m.group("b") else a
        pages.extend(range(min(a, b), max(a, b) + 1))
    seen: list[int] = []
    for p in pages:
        if p >= 1 and p <= (total or 10 ** 6) and p not in seen:
            seen.append(p)
    return sorted(seen)[:limit]


# --------------------------------------------------------------------------
# 档案（第一轮之前预注入，§9）
# --------------------------------------------------------------------------


def archive(paper_id: str) -> dict:
    """论文档案 + 大纲 + 规模。

    档案来自 `translation_brief.json`（翻译流程里已经产出的那一份，**零额外模型调用**），
    大纲来自 `reader_service.build_outline`（纯结构计算）。所以这个接口很便宜，
    每轮问答前预注入得起。
    """
    from backend.services.translation import brief as brief_mod

    paper = paper_service.get_paper(paper_id)
    brief = brief_mod.load_brief(paper_id)
    outline = reader_service.build_outline(paper_id)
    return {
        "paper_id": paper_id,
        "title": paper.get("title") or "",
        "page_count": int(paper.get("page_count") or 0),
        # `page_total` 与 `page_count` 是同一个数：档案块的渲染读的是前者（历史命名），
        # 只给 `page_count` 时提示词里的"篇幅"永远是空的。
        "page_total": int(paper.get("page_count") or 0),
        "status": paper.get("status"),
        "brief": brief.to_dict() if brief is not None else {},
        "brief_source": (brief.source if brief is not None else "none"),
        "outline": list(outline.get("items") or []),
        "has_markdown": bool(paper_service.get_markdown(paper_id)),
        # 现有元数据：元数据填写那次会话要它（"哪些字段已经有值了，别重复提交"）。
        # 对问答是纯背景，`archive_block` 不渲染它。
        "meta": {k: paper.get(k) for k in (
            "title", "authors", "venue", "year", "tags", "abstract", "abstract_zh",
            "keywords_en", "domain", "one_liner", "contributions", "prereq",
            "difficulty", "read_time_min", "paper_type", "doi", "code_url")},
    }


# --------------------------------------------------------------------------
# 按页读块（`page_markdown_get`）
# --------------------------------------------------------------------------


def pages(paper_id: str, spec: str, lang: str = "both") -> dict:
    """按页返回**块级正文**（可选只要原文 / 只要译文）。

    ``lang``：``both``（默认）｜``en``（只回原文）｜``zh``（只回译文，`translate_get` 用它）。
    只回译文时**不省略没有译文的块** —— 缺译文本身就是信息（模型该说"这页还没翻译"，
    而不是以为这页没内容）。

    **内容优先取页级产物**（用户口径 2026-10-03）：A 路英文转录 / B 路中文翻译是阅读器与索引
    共用的同一份读模型，也是**事实源**。解析侧的表格会把单元格粘成一串
    （`KV SizeSingleQAMultiQA…`、`1645.2945.16`），而转录过的表格是规规矩矩的 Markdown 表格
    （实测 turboquant p9 表 1：`| Method | KV Size | SingleQA | …`）。
    **没有页级产物的页才退回解析块**（`md/markdown.md` + `block` 表）—— 未翻译的论文、
    正在跑转录的页都属于这一类，不能因为"没有转录"就不给内容。

    为什么按块/行而不是整页文本：引用要能"跳到那一行并高亮"。块与行都是分块与高亮的共同
    边界；解析侧的行锚点由 `anchor_map.build_block_row_map` 读时计算（与阅读器"跳回原文"
    同一套逻辑），转录侧则**直接用行的 id**（阅读器正文的 `data-row-id` 就是它）。

    ``source`` 字段告诉工具层这条是转录行还是解析块 —— 两者的 ref 语义不同
    （转录行没有 `block_id`，`block_get` 要走"按行展开"那条路）。
    """
    from backend.repositories.mysql.models import Block
    from backend.repositories.mysql.session import session_scope

    paper = paper_service.get_paper(paper_id)
    total = int(paper.get("page_count") or 0)
    wanted = parse_pages(spec, total)
    if not wanted:
        raise NotFoundError(f"页码无法解析：{spec!r}")
    mode = lang if lang in ("both", "en", "zh") else "both"

    # ---- ① 页级产物（转录 / 翻译）：与阅读器逐字同源 ----
    by_page: dict[int, list[dict]] = {}
    for page_no in wanted:
        payload = reader_service.merged_page(paper_id, page_no) or {}
        units: list[dict] = []
        for item in payload.get("rows") or []:
            original = str(item.get("original") or "").strip()
            translation = str(item.get("translation") or "").strip()
            if mode == "en" and not original:
                continue
            if not original and not translation:
                continue
            row_id = str(item.get("id") or "")
            unit: dict = {"id": row_id, "type": item.get("type") or "para",
                          "section": item.get("section"), "row_id": row_id,
                          "source": "transcript"}
            if mode in ("both", "en"):
                unit["text"] = _neutralize_images(original)
            if mode in ("both", "zh"):
                unit["translation"] = translation or None
            units.append(unit)
        if units:
            by_page[page_no] = units

    # ---- ② 其余页：解析块（老路径） ----
    rest = [p for p in wanted if p not in by_page]
    if rest:
        with session_scope() as sess:
            rows = (sess.query(Block)
                    .filter(Block.paper_id == paper_id, Block.page_no.in_(rest))
                    .order_by(Block.page_no.asc(), Block.order_global.asc()).all())
            blocks = [{"id": b.id, "page": int(b.page_no), "type": b.type,
                       "section": b.section_id, "preview": b.preview} for b in rows]

        md_map = reader_service.load_markdown_map(paper_id)
        # 行锚点的输入形状是 ``{"id","page","text"}``（见 `anchor_map.build_block_row_map`）——
        # 文本必须与引用里给模型看到的那一份一致，否则"块 → 行"的匹配会按另一个文本做，
        # 出现"引用跳到了别的段落"（这里传错过一次：传了 preview，映射整片为空）。
        anchor_blocks = []
        texts: dict[str, str] = {}
        for block in blocks:
            entry = md_map.get(block["id"]) or {}
            text = str(entry.get("text") or block.get("preview") or "").strip()
            texts[block["id"]] = text
            anchor_blocks.append({"id": block["id"], "page": block["page"], "text": text})

        rows_by_page: dict[int, list[dict]] = {}
        for page_no in sorted({b["page"] for b in blocks}):
            payload = reader_service.merged_page(paper_id, page_no) or {}
            page_rows = payload.get("rows") or []
            if page_rows:
                rows_by_page[page_no] = page_rows
        anchors = anchor_map.build_block_row_map(anchor_blocks, rows_by_page)

        for block in blocks:
            entry = md_map.get(block["id"]) or {}
            raw_text = texts.get(block["id"]) or ""
            translation = str(entry.get("translation") or "").strip()
            # 只要译文时，原文仍参与"这个块有没有内容"的判断，但不下发
            if mode == "en" and not raw_text:
                continue
            if mode == "zh" and not (raw_text or translation):
                continue
            if not raw_text and not translation:
                continue
            row_id = anchors.get(str(block["id"]))
            out: dict = {"id": block["id"], "type": block["type"],
                         "section": block["section"], "source": "parse"}
            if row_id:
                out["row_id"] = row_id
            if mode in ("both", "en"):
                out["text"] = _neutralize_images(raw_text)
            if mode in ("both", "zh"):
                out["translation"] = translation or None
            by_page.setdefault(int(block["page"]), []).append(out)

    return {
        "paper_id": paper_id, "page_total": total, "lang": mode,
        "requested": wanted,
        "pages": [{"page": p, "blocks": by_page.get(p, [])} for p in wanted],
    }


def row(paper_id: str, page: int, row_id: str) -> dict:
    """页级产物里的**一行**（`block_get` 展开"转录段落"用的）。

    转录行的 id 就是阅读器正文里的 `data-row-id`（``p009-00`` 这种位置编号），
    所以"按 ref 展开"必须能按这个 id 取回同一行 —— 否则模型看到引用却展不开。
    行号是位置编号：**重转录后可能变了**，变了就明确报"这一行不在了"，不要瞎猜下一页。
    """
    paper_service.get_paper(paper_id)
    payload = reader_service.merged_page(paper_id, int(page)) or {}
    for item in payload.get("rows") or []:
        if str(item.get("id")) == str(row_id):
            return {
                "id": str(row_id), "page": int(page),
                "type": item.get("type") or "para",
                "text": _neutralize_images(str(item.get("original") or "").strip()),
                "translation": str(item.get("translation") or "").strip() or None,
                "source": "transcript",
            }
    raise NotFoundError(f"第 {page} 页没有 {row_id} 这一行（可能重转录过、行号变了）")


# --------------------------------------------------------------------------
# 检索（`paper_search` / P1 的 `kb_search`）
# --------------------------------------------------------------------------


#: 检索方式（**对外的叫法**）→ 实际要跑的召回来源。
#: 为什么另起名字：`retrieval.search` 的 `mode` 是**查询指令口径**（qa / similar），
#: 拿它当"语义/关键词"开关会同时改掉查询编码，见那条 docstring 的警告。
SEARCH_SELECTS: dict[str, list[str] | None] = {
    "auto": None,               # 两路召回 → RRF 融合 → 重排（默认，既有行为）
    "rag": ["vector"],          # 只跑向量：语义近似
    "text": ["lexical"],        # 只跑 BM25：精确词
}


def norm_select(value: str | None) -> str:
    text = str(value or "auto").strip().lower()
    if text not in SEARCH_SELECTS:
        raise ValidationError(
            f"检索方式只能是 {' / '.join(SEARCH_SELECTS)}（收到 {value!r}）："
            f"text = 词法（精确词），rag = 向量（语义近似），auto = 两路融合")
    return text


def search(paper_id: str, query: str, *, top_k: int = 8,
           chunk_types: list[str] | None = None, scope: str = "paper",
           select: str = "auto") -> dict:
    """**本论文内**检索，返回候选位置（chunk id / 页码 / 章节 / 片段）。

    ⚠️ `mode="qa"` 不能省：`retrieval.query_vector` 里只有 ``mode == "qa"`` 才用
    `QUERY_INSTRUCT`，否则用 `SIMILAR_INSTRUCT`（那是"找相似技术"那个用法的口径）。
    默认值 `similar` 用在问答上，查询编码口径不对，**白丢一截召回且看不出来**。
    注意 `mode` **不是"语义/关键词"开关**，别把前端筛选 UI 的语义套过来 ——
    要选"用哪种召回"请传 `select`（`auto` / `rag` / `text`，见 `SEARCH_SELECTS`）。
    """
    chosen = norm_select(select)
    paper = paper_service.get_paper(paper_id)
    total = int(paper.get("page_count") or 0)
    result = retrieval.search(query, mode="qa", scope=scope,
                             paper_id=paper_id if scope != "all" else None,
                             top_k=max(1, min(int(top_k or 8), 20)),
                             chunk_types=chunk_types,
                             sources=SEARCH_SELECTS[chosen])
    items = list(result.get("items") or [])
    zh_previews = _paired_zh_previews(items)
    out: list[dict] = []
    for item in items:
        chunk_id = str(item.get("chunk_id") or "")
        hit = {
            "chunk_id": chunk_id,
            # 行 / 块分开下发：写侧把行 id 塞进了 block_id，原样透传等于给下游一个查不到的 id
            **anchor_ids(item),
            "section": item.get("section"),
            "chunk_type": item.get("chunk_type"),
            "lang": item.get("lang"),
            "content": clip_around(_neutralize_images(item.get("content")), query),
            # 中文预览（同 chunk 的 zh 行）：**引用片段优先用译文**，用户看得懂；
            # 拿不到就是 None，绝不现译一次（那会让"检索"变成"翻译"）。
            "translation": zh_previews.get(chunk_id),
        }
        # 行身份（阶段 2b）：引用带着 pid 才能在重新翻译/重建索引后认回同一行。
        # 存量 chunk 还没有 pids → 不写这个键（不写空列表假装有）。
        pids = _chunk_pids(item)
        if pids:
            hit["pids"] = pids
        out.append(hit)
    retr = result.get("retrieval") or {}
    return {"paper_id": paper_id, "page_total": total, "query": query,
            "returned": len(out), "items": out,
            # 这次实际用的检索方式：模型没传时也回显，省得它猜"到底跑的是哪种"
            "select": chosen,
            # 各路命中数：**只给端点与排查用**（工具不会把它塞进给模型的返回里，
            # 见模块头"不发 retrieval 元数据"），但它能一眼看出"这次只跑了一边"
            "retrieval": {"select": chosen,
                          "vector_hits": int(retr.get("vector_hits") or 0),
                          "lexical_hits": int(retr.get("lexical_hits") or 0),
                          "fused": int(retr.get("fused") or 0)},
            # 证据闸用的元信息（**不发给模型**，见 §7.5）：向量检索永远返回 top-k，
            # "有没有命中"不带信息，只有重排分数才区分得开"有依据"和"胡说"。
            "top_score": max((float(i.get("rerank_score") or 0) for i in items), default=0.0),
            "rerank_degraded": bool((result.get("retrieval") or {}).get("rerank", {})
                                    .get("degraded", True))}


def kb_search(query: str, *, top_k: int = 8) -> dict:
    """**跨库**检索（不限论文）：返回候选位置 + **它属于哪篇论文**。

    与 `search(scope="paper")` 共用同一条链路，只有 scope 不同 —— 但它们是**两个工具**：
    "这篇论文里怎么说的"与"我库里还有谁讲过这个"是两种问题，合并会让模型在单篇问答里
    就拿到全库视野，scope 语义跟着糊（规划 §7.0）。

    ⚠️ 跨库命中带的 `paper_id` 不是当前论文，引用必须**带上它自己那篇**的 id
    （否则前端会跳到当前论文的同一页 —— 一个完全不相干的段落）。
    """
    result = retrieval.search(query, mode="qa", scope="all",
                              top_k=max(1, min(int(top_k or 8), 20)))
    items = list(result.get("items") or [])
    titles = _paper_titles({str(i.get("paper_id") or "") for i in items})
    zh_previews = _paired_zh_previews(items)
    out: list[dict] = []
    for item in items:
        paper_id = str(item.get("paper_id") or "")
        chunk_id = str(item.get("chunk_id") or "")
        hit = {
            "paper_id": paper_id, "paper_title": titles.get(paper_id, ""),
            "chunk_id": chunk_id,
            # 跨库命中同样按名字空间下发（行 id 不进 block_id）
            **anchor_ids(item),
            "section": item.get("section"), "chunk_type": item.get("chunk_type"),
            "lang": item.get("lang"),
            "content": clip_around(_neutralize_images(item.get("content")), query),
            "translation": zh_previews.get(chunk_id),
        }
        # 同上：行身份来自这条命中自己的 chunk metadata（跨库命中属于它自己那篇）。
        pids = _chunk_pids(item)
        if pids:
            hit["pids"] = pids
        out.append(hit)
    return {"query": query, "returned": len(out), "items": out,
            "top_score": max((float(i.get("rerank_score") or 0) for i in items), default=0.0),
            "rerank_degraded": bool((result.get("retrieval") or {}).get("rerank", {})
                                    .get("degraded", True))}


def _chunk_pids(item: dict) -> list[str]:
    """这一条命中记住的**行身份**（chunk 的 ``metadata.pids``，阶段 2b）。

    **不额外查库**：检索结果本来就带着 ``metadata``（``rrf_fuse`` 的 content_fields 里有它，
    值来自 ``metadata_json``），pids 就在里面。存量 chunk 的 metadata 还没有这个字段 ——
    拿不到就是空列表，调用方据此**不写** ``pids`` 键（写空列表等于假装"这一条没有身份"）。
    """
    meta = item.get("metadata") or {}
    return [str(p) for p in (meta.get("pids") or []) if p]


def _paper_titles(paper_ids: set[str]) -> dict[str, str]:
    from backend.repositories.mysql.models import Paper
    from backend.repositories.mysql.session import session_scope

    ids = {p for p in paper_ids if p}
    if not ids:
        return {}
    with session_scope() as sess:
        return {r.id: (r.title or "") for r in
                sess.query(Paper).filter(Paper.id.in_(ids)).all()}


def _paired_zh_previews(items: list[dict]) -> dict[str, str]:
    """按论文分组后逐篇查中文孪生预览（跨库检索会一次命中多篇）。"""
    by_paper: dict[str, list[dict]] = {}
    for item in items:
        paper_id = str(item.get("paper_id") or "")
        if paper_id:
            by_paper.setdefault(paper_id, []).append(item)
    out: dict[str, str] = {}
    for paper_id, group in by_paper.items():
        out.update(_paired_zh_previews_one(paper_id, group))
    return out


def _paired_zh_previews_one(paper_id: str, items: list[dict]) -> dict[str, str]:
    """给英文命中找它的中文孪生行预览（命中 ``chunk.content_preview``，200 字）。

    **不能靠改 id 字符串猜**：chunk id 是 ``{paper_id}:{road}:{type}:{块范围}:{lang}``，
    中文那一份不只是 ``:en``→``:zh``，**road 也从 ``a`` 变成 ``b``**
    （实测：``…:a:unit:p005-01-p005-02:en`` ↔ ``…:b:unit:p005-01-p005-02:zh``）。
    所以按"同一论文 + 同一块 + 同一类型"回库查，并且**查不到就不给**
    —— 现拼一个 id 就当有译文，会把别的 chunk 的预览贴到这条命中上，比没有译文更糟。
    """
    from backend.repositories.mysql.models import Chunk
    from backend.repositories.mysql.session import session_scope

    wanted: dict[tuple[str, str], list[str]] = {}
    for item in items:
        chunk_id = str(item.get("chunk_id") or "")
        block_id = item.get("block_id")
        if not chunk_id or chunk_id.endswith(":zh") or not block_id:
            continue
        wanted.setdefault((str(block_id), str(item.get("chunk_type") or "")),
                          []).append(chunk_id)
    if not wanted:
        return {}
    with session_scope() as sess:
        rows = (sess.query(Chunk)
                .filter(Chunk.paper_id == paper_id,
                        Chunk.block_id.in_({b for b, _ in wanted}),
                        Chunk.id.like("%:zh"))
                .order_by(Chunk.page_no.asc(), Chunk.id.asc()).all())
    found: dict[tuple[str, str], str] = {}
    for row in rows:
        key = (str(row.block_id), str(row.chunk_type or ""))
        if row.content_preview and key not in found:
            found[key] = row.content_preview
    out: dict[str, str] = {}
    for key, chunk_ids in wanted.items():
        preview = found.get(key)
        if preview:
            for chunk_id in chunk_ids:
                out[chunk_id] = preview
    return out


# --------------------------------------------------------------------------
# 插图（`figure_list`）
# --------------------------------------------------------------------------


def figures(paper_id: str, page: int | None = None) -> dict:
    """某一页的**图形区域**清单：插图与表格（图号 / 图注 / 可嵌入地址 / 区域序号）。

    ``index`` 是 `page_regions` 里的**区域序号**，`figure_image_get` 要的就是它。
    url 仍然只由 `media_of_page` 生成（版本串 `v=` 不会被这里绕过）；
    这里**不按 kind 过滤**，所以位置序号天然等于区域序号。

    **表格和插图走同一条路**（2026-10-03）：解析出来的表格正文经常把单元格粘成一串
    （`1645.2945.16` 其实是 `16 | 45.29 | 45.16`），列对齐已经不可恢复 ——
    而 `page_regions` 里表格本来就登记着区域 bbox，能按需裁一张 600dpi 的表图交给
    视觉模型。以前只列 `kind == "figure"`，模型于是"知道这篇有表 1、却拿不到表"
    （实测它退到整页 `page_image_get`，并在回答里抱怨工具够不到表格）。
    """
    from backend.services.paper import figure_regions

    paper_service.get_paper(paper_id)
    page_no = int(page or 1)
    media = figure_regions.media_of_page(paper_id, page_no, figures_only=False)
    items: list[dict] = []
    for index, item in enumerate(media):
        items.append({
            "index": index,
            "kind": str(item.get("kind") or "figure"),
            "number": item.get("number") or "",
            # 图注上限 800：读时**拼接**后完整图注实测最长约 700 字符，400 会把句子截在
            # 半句（14/138 条中招）；800 是给将来略长的图注留的余量，超过仍然截断。
            "caption": _clip(item.get("caption") or "", CAPTION_CHARS),
            "url": item.get("url") or "",
            "card_url": item.get("card_url") or "",
            "part": item.get("part"), "parts": item.get("parts"),
        })
    return {"paper_id": paper_id, "page": page_no, "items": items}


# --------------------------------------------------------------------------
# 公式（`formula_get`）
# --------------------------------------------------------------------------

#: 一次最多回几条公式（Attention 这类论文全篇 10~30 条，够用；超长论文要筛）
FORMULA_LIMIT = 40


#: 公式卡的**文档顺序**：解析卡 id 形如 `turboquanton-p03-b05`（页 + 块序号），
#: markdown 抽出来的 `row_id` 形如 `p005-03` —— 两者都能从尾部数字还原先后。
#: bbox 在 formula 表里有，但 `_parse_formula_item` 没把它带进投影（改它就是动阅读器那份
#: 契约），所以这里用 id 的顺序，视觉上等价。
_DOC_SEQ_RE = re.compile(r"(\d{1,4})$")


def _doc_key(item: dict) -> tuple[int, int, str]:
    raw = str(item.get("row_id") or item.get("id") or "")
    found = _DOC_SEQ_RE.search(raw)
    return (int(item.get("page") or 0), int(found.group(1)) if found else 0, raw)


def formulas(paper_id: str, *, page: int | None = None, query: str | None = None,
             limit: int = FORMULA_LIMIT, include_inline: bool = False) -> dict:
    r"""本论文的公式清单（LaTeX + 所在页 + 行锚点 + grade + 来源）。

    **默认读页级 Markdown 里的块级公式**（`display=true`）—— 那就是阅读器「公式」栏里
    用户看到、也复制得到的那批**完整公式**（实测全篇 346 条抽取里有 29 条算块级：
    `$$…$$` 的 24 条 + 列表项里"整行只有这条公式"的 5 条，后者见
    `reader_service._promote_to_display`；这 29 条条条完整：`D_{\text{mse}} := \mathbb{E}_Q[…]`、
    含 `\begin{aligned}` 的多行推导）。

    - `include_inline=True` 才把**行内**公式（`display=false`）也列出来；默认不要 ——
      伪代码里的 `\qquad` / `b-1` 碎片就是这一批；
    - **解析卡只做兜底**：某页在 markdown 源里一条块级公式都没有时，才用该页的 `formula` 表
      补上（条目带 `source:"parse"`）。卡片的 LaTeX 常常只是公式的一段 ——
      `partial=True` 就是在说这件事，此时该去看 `page_image_get(page)` 的原版面；
    - `grade` 只有解析卡有，markdown 条目留空（不为一个评级去 join 表）；
    - 排序是**文档顺序**（页 → 块序号）；超 `limit` 返回 `truncated`；
    - `display_total` / `inline_total` / `total` **同一口径**：都数「去重后、在筛选范围内的
      全部条目」，不随 `include_inline` 变 —— 不变式 `total == display_total + inline_total`
      任何时候都成立；`returned` 才是「这次给了几条」，`omitted_inline` 是这次按需过滤掉的行内条数。

    ⚠️ **没有"编号"字段**：论文自己的 `(3)` 编号只在 `context` 里，按编号找请先
    `page_markdown_get` 定位那一页，别让模型以为有个 `number` 参数。
    """
    from backend.services.paper import reader_service

    paper_service.get_paper(paper_id)
    md_items = [dict(one) for one in
                (reader_service.formulas_payload(paper_id, "markdown") or {}).get("items") or []]

    def _in_scope(one: dict) -> bool:
        if page and int(one.get("page") or 0) != int(page):
            return False
        if query:
            needle = str(query).strip().lower()
            if needle not in " ".join(str(one.get(k) or "") for k in
                                      ("latex", "context", "context_zh")).lower():
                return False
        return True

    scoped_md = [one for one in md_items if _in_scope(one)]
    items = list(scoped_md)
    #: 这些页在 markdown 源里已经有块级公式了 → 不再用解析卡补
    pages_with_display = {int(one.get("page") or 0) for one in md_items if one.get("display")}

    #: 转录层已经给出的公式（去空白）：解析卡若只是它的**一段**，就别再列一遍 ——
    #: 实测 2026-12-07 第 22 页：行内完整版 `\\boldsymbol{K},\\boldsymbol{V}\\in\\mathbb{R}^{n\\times d}` 已在列表里，
    #: 兜底又把提取卡的碎片 `n\\times d` 当块级公式列了一次（同一式子出现两条，还带 grade A）。
    def _squash(text: object) -> str:
        return "".join(str(text or "").split())

    known: dict[int, list[str]] = {}
    for known_page, _row, latex in ((int(i.get("page") or 0), i.get("row_id"), i.get("latex"))
                                    for i in scoped_md):
        known.setdefault(known_page, []).append(_squash(latex))
    for one in (reader_service.formulas_payload(paper_id, "parse") or {}).get("items") or []:
        one_page = int(one.get("page") or 0)
        if one_page in pages_with_display or not _in_scope(one):
            continue
        probe = _squash(one.get("latex"))
        if any(probe and probe in bigger for bigger in known.get(one_page, [])):
            continue
        items.append({**one, "source": "parse"})
    # markdown 那一路是**按出现次数**给的（同一段里同一个 $x$ 出现两三遍 → 两三条条目），
    # 两路合并后也可能撞车。同 (page, row_id, latex) 只留第一条 —— 重复项对"读"与"引用"
    # 都没有价值，还会占掉引用号。解析卡本身基本不会重复，这条主要是给 markdown 兜底用的。
    seen: set[tuple[int, str, str]] = set()
    unique: list[dict] = []
    for item in items:
        key = (int(item.get("page") or 0), str(item.get("row_id") or ""),
               " ".join(str(item.get("latex") or "").split()))
        if key in seen:
            continue
        seen.add(key)
        unique.append(item)
    items = sorted(unique, key=_doc_key)

    #: 三个计数**同一口径**：都数"去重后、在筛选范围内的全部条目"（cap 与
    #: `include_inline` 之前）。以前 display/inline 数的是"去重前的 markdown 源"、total 数的
    #: 是"去重后的合并列表"，于是回执里出现 `display_total=2, inline_total=46, total=46`
    #: （第 18 页，2+46≠46）—— 模型没法据此判断到底还有多少条没给。
    #: 不变式：**total == display_total + inline_total**，任何时候都成立。
    display_total = sum(1 for one in items if one.get("display"))
    inline_total = len(items) - display_total
    #: 这一步只决定"这次列不列行内那批"，不影响上面三个计数（计数说的是"有多少"，
    #: 不是"这次给了多少"——后者由 `returned` 说）。
    listed = [one for one in items if one.get("display") or include_inline]

    cap = max(1, int(limit or FORMULA_LIMIT))
    kept = listed[:cap]
    out = [{
        "page": int(i.get("page") or 0) or None,
        "row_id": i.get("row_id"),
        "latex": str(i.get("latex") or ""),
        "display": bool(i.get("display")),
        "grade": i.get("grade"),
        # 这条 LaTeX 可能只是公式的一段（解析时 candidate 被切段、又没有 final）→ 去看裁图
        "partial": bool(i.get("partial")),
        "source": i.get("source") or "parse",
        "context": _clip(i.get("context") or "", 240),
        "context_zh": _clip(i.get("context_zh") or "", 240),
    } for i in kept]
    #: `source` 说的是"这一份列表**从哪来**"，所以要看**筛选后的全集**（`items`），
    #: 而不是这次真正发出去的 `out`：第 8 页只有一条行内公式，默认（不要行内）时 `out` 是空的，
    #: 以前就因此落到 else 分支报 `mixed` —— 明明一条解析卡都没有（实测 2026-12-07）。
    sources = {one.get("source") for one in items}
    if not sources:
        sources = {"markdown"} if md_items else {"parse"}
    return {"paper_id": paper_id, "total": len(items), "returned": len(out),
            # 这一份列表**从哪来**：全解析卡 = parse（可信），全 markdown = markdown，
            # 两路混 = mixed。模型据此决定要不要再 page_markdown_get 复核
            "source": ("parse" if sources == {"parse"}
                       else "markdown" if sources == {"markdown"} else "mixed"),
            # 只有"上限把这次要列的条目切掉了"才叫 truncated；`include_inline=False`
            # 不算截断 —— 行内那批是**按需过滤**掉的，条数写在 `omitted_inline` 里。
            "truncated": len(listed) > len(out),
            # 三个计数同一口径（都数去重后的全部在范围条目），不变式 total = display + inline：
            # 块级 = 完整公式（可引用），行内 = 碎片/单符号（不适合当引用来源）
            "display_total": display_total, "inline_total": inline_total,
            "omitted_inline": 0 if include_inline else inline_total,
            "items": out}


# --------------------------------------------------------------------------
# 书架（`library_list`）
# --------------------------------------------------------------------------

#: 一次最多列几篇（模型只要"挑出相关的几篇"，不需要整库）
LIBRARY_LIMIT = 30


def library(*, q: str | None = None, status: str | None = None,
            limit: int = LIBRARY_LIMIT) -> dict:
    """书架检索（标题 / 作者的模糊匹配）——"我库里有谁讲过 X"的第一步。

    只回元数据，不回正文：模型据此挑出候选论文，再用 `kb_search` 去找具体段落。
    """
    data = paper_service.list_papers(q=q or None, status=status or None,
                                     page_size=max(1, min(int(limit or LIBRARY_LIMIT), 100)))
    items = []
    for row in data.get("items") or []:
        items.append({
            "paper_id": row.get("id"), "title": row.get("title") or "",
            "authors": _clip(row.get("authors") or "", 160),
            "venue": row.get("venue") or "", "year": row.get("year") or 0,
            "status": row.get("status") or "",
            "page_count": int(row.get("page_count") or 0),
            "tags": list(row.get("tags") or []),
        })
    return {"total": int(data.get("total") or 0), "returned": len(items), "items": items}


# --------------------------------------------------------------------------
# 笔记 / 高亮 / 批注（`notes_search`）
# --------------------------------------------------------------------------

#: 一次最多回几条
NOTES_LIMIT = 40


def notes(*, query: str | None = None, paper_id: str | None = None,
          kind: str = "all", limit: int = NOTES_LIMIT) -> dict:
    """笔记 / 高亮 / 批注（可按论文限定）。

    用户自己的笔记是**第一人称证据**："我在这篇里记过什么"只有这里能答。
    返回里带 `paper_id` 与 `block_id`，所以引用能跳回原文那一行。
    """
    from backend.services.annotation import annotation_service as svc

    kinds = ("all", "notes", "highlights", "annotations")
    want = kind if kind in kinds else "all"
    out: list[dict] = []

    def _note_row(n: dict, pid: str, title: str = "", hl_text: str = "") -> dict:
        """一条笔记。`selected_text` 优先用笔记自己的，其次用它所挂高亮的原文 ——
        用户眼里「标了这句 + 写了一条笔记」是**一条**东西，两个字段都得在。"""
        row = {"kind": "note", "note_id": n.get("id"), "paper_id": pid,
               "highlight_id": n.get("highlight_id"),
               "block_id": n.get("block_id"),
               "selected_text": _clip(n.get("selected_text") or hl_text or "", 200),
               "note": _clip(n.get("content_md") or "", 600),
               "tags": list(n.get("tags") or [])}
        if title:
            row["paper_title"] = title
        return row

    def _highlight_row(h: dict, pid: str, note: dict | None, title: str = "") -> dict:
        """一条高亮。**挂着的笔记正文要带上**：`note` 为空只能意味着"这条高亮上没写过
        笔记"，不能是"查不到正文" —— 用户报的「笔记正文拿不到」就是这两件事被混了。"""
        row = {"kind": "highlight", "note_id": (note or {}).get("id"), "paper_id": pid,
               "block_id": h.get("block_id"),
               "selected_text": _clip(h.get("text") or "", 200),
               "note": _clip((note or {}).get("content_md") or "", 600),
               "tags": []}
        if title:
            row["paper_title"] = title
        return row

    def _listed(out_rows: list[dict]) -> set[str]:
        return {one.get("note_id") for one in out_rows if one.get("kind") == "note"}

    if paper_id:
        paper_service.get_paper(paper_id)
        #: 关键词要**同时**过滤笔记正文与高亮原文：以前 `list_highlights` 没有 `q`，查询在
        #: 那一支被静默丢掉 → `kind=all` + 关键词回来一堆无关高亮、命中的笔记被挤到后面
        #: （用户看到的就是"搜不到自己写的笔记"）。
        attached = {n.get("highlight_id"): n
                    for n in (svc.list_notes(paper_id).get("items") or [])
                    if n.get("highlight_id")}
        hl_text = {h.get("id"): h.get("text") or ""
                   for h in (svc.list_highlights(paper_id).get("items") or [])}
        if want in ("all", "notes"):
            for n in (svc.list_notes(paper_id, q=query or None).get("items") or []):
                out.append(_note_row(n, paper_id, "",
                                     hl_text.get(n.get("highlight_id")) or ""))
        if want in ("all", "highlights"):
            listed = _listed(out)
            for h in (svc.list_highlights(paper_id, q=query or None).get("items") or []):
                note = attached.get(h.get("id"))
                # kind=all：这条高亮已经作为笔记行出现过了（同一条标注），别列两遍
                if want == "all" and note is not None and note.get("id") in listed:
                    continue
                out.append(_highlight_row(h, paper_id, note))
        if want in ("all", "annotations"):
            #: 批注那一支的 q 也是 2026-12 补的：以前只传 paper_id，关键词被静默丢掉 ——
            #: 问「我关于 KL 的批注」回来的是全部批注，模型把无关条目当命中引用。
            for a in (svc.list_annotations(paper_id, q=query or None).get("items") or []):
                out.append({"kind": "annotation", "paper_id": paper_id,
                            "block_id": a.get("block_id"),
                            # AI 批注没有"选中文本"字段（只有 `content_md`），别硬凑
                            "selected_text": "",
                            "note": _clip(a.get("content_md") or "", 600),
                            "tags": [a.get("type")] if a.get("type") else []})
    else:
        overview = svc.notes_overview()
        # 论文已被删掉的孤儿批注拿不到标题 → 回落 paper_id（总比空白强，模型至少能说清出处）
        def _title(row: dict) -> str:
            return str(row.get("paper_title") or row.get("paper_id") or "")

        all_notes = overview.get("notes") or []
        attached = {n.get("highlight_id"): n for n in all_notes if n.get("highlight_id")}
        hl_text = {h.get("id"): h.get("text") or ""
                   for h in (overview.get("highlights") or [])}
        if want in ("all", "notes"):
            for n in all_notes:
                out.append(_note_row(n, str(n.get("paper_id") or ""), _title(n),
                                     hl_text.get(n.get("highlight_id")) or ""))
        if want in ("all", "highlights"):
            listed = _listed(out)
            for h in (overview.get("highlights") or []):
                note = attached.get(h.get("id"))
                if want == "all" and note is not None and note.get("id") in listed:
                    continue
                out.append(_highlight_row(h, str(h.get("paper_id") or ""), note,
                                          _title(h)))
        if want in ("all", "annotations"):
            for a in (overview.get("annotations") or []):
                out.append({"kind": "annotation", "paper_id": a.get("paper_id"),
                            "paper_title": _title(a),
                            "block_id": a.get("block_id"),
                            "selected_text": "",
                            "note": _clip(a.get("content_md") or "", 600),
                            "tags": [a.get("type")] if a.get("type") else []})

    if query and not paper_id:
        # 跨库时在上面那次查询里无法按词过滤（`notes_overview` 不接受 q），在这里补一层
        needle = str(query).strip().lower()
        out = [x for x in out
               if needle in " ".join(str(x.get(k) or "") for k in
                                     ("selected_text", "note", "paper_title")).lower()]
    kept = out[:limit]
    #: total / truncated 与 formula_get / page_markdown_get 同一口径：以前只给 returned
    #: （上限 40），被截掉的那部分对模型等于不存在 —— 它会说“你只记过 40 条”。
    return {"paper_id": paper_id, "kind": want, "returned": len(kept),
            "total": len(out), "truncated": len(out) > len(kept),
            "items": kept}
