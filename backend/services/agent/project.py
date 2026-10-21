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

from backend.core.errors import NotFoundError
from backend.services.knowledge import retrieval
from backend.services.paper import anchor_map, paper_service, reader_service

#: 一次投影最多给几页（agent 侧还会再截一次）
PAGES_MAX = 30
#: 检索片段给模型看的长度
SNIPPET_CHARS = 400

_PAGES_RE = re.compile(r"^\s*(?P<a>\d+)\s*(?:[-~–]\s*(?P<b>\d+))?\s*$")
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


def search(paper_id: str, query: str, *, top_k: int = 8,
           chunk_types: list[str] | None = None, scope: str = "paper") -> dict:
    """**本论文内**检索，返回候选位置（chunk id / 页码 / 章节 / 片段）。

    ⚠️ `mode="qa"` 不能省：`retrieval.query_vector` 里只有 ``mode == "qa"`` 才用
    `QUERY_INSTRUCT`，否则用 `SIMILAR_INSTRUCT`（那是"找相似技术"那个用法的口径）。
    默认值 `similar` 用在问答上，查询编码口径不对，**白丢一截召回且看不出来**。
    注意 `mode` **不是"语义/关键词"开关**，别把前端筛选 UI 的语义套过来。
    """
    paper = paper_service.get_paper(paper_id)
    total = int(paper.get("page_count") or 0)
    result = retrieval.search(query, mode="qa", scope=scope,
                             paper_id=paper_id if scope != "all" else None,
                             top_k=max(1, min(int(top_k or 8), 20)),
                             chunk_types=chunk_types)
    items = list(result.get("items") or [])
    zh_previews = _paired_zh_previews(items)
    out: list[dict] = []
    for item in items:
        chunk_id = str(item.get("chunk_id") or "")
        hit = {
            "chunk_id": chunk_id,
            "block_id": item.get("block_id"),
            "page_no": int(item.get("page_no") or 0) or None,
            "section": item.get("section"),
            "chunk_type": item.get("chunk_type"),
            "lang": item.get("lang"),
            "content": _clip(_neutralize_images(item.get("content"))),
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
    return {"paper_id": paper_id, "page_total": total, "query": query,
            "returned": len(out), "items": out,
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
            "chunk_id": chunk_id, "block_id": item.get("block_id"),
            "page_no": int(item.get("page_no") or 0) or None,
            "section": item.get("section"), "chunk_type": item.get("chunk_type"),
            "lang": item.get("lang"),
            "content": _clip(_neutralize_images(item.get("content"))),
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
            "caption": _clip(item.get("caption") or "", 400),
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


def formulas(paper_id: str, *, page: int | None = None, query: str | None = None,
             limit: int = FORMULA_LIMIT) -> dict:
    """本论文的公式清单（LaTeX + 所在页 + 行锚点 + 上下文）。

    数据来自 `reader_service.formulas_payload(…, "markdown")` —— 与阅读器"公式"页签
    **同一个口径**（译文优先、未翻译的页用解析产物兜底）。所以 agent 讲的公式和用户
    在正文里看到的是同一条，不会出现"AI 引用的公式在页面上找不到"。

    ⚠️ **没有"编号"字段**：抽取结果里只有 LaTeX 与上下文，论文自己的 `(3)` 编号在
    `context` 里。所以按编号取公式只能靠关键词筛（`query="(3)"`），这一点写进工具描述里，
    别让模型以为有个 `number` 参数。
    """
    from backend.services.paper import reader_service

    paper_service.get_paper(paper_id)
    data = reader_service.formulas_payload(paper_id, "markdown")
    items = list(data.get("items") or [])
    if page:
        items = [i for i in items if int(i.get("page") or 0) == int(page)]
    if query:
        needle = str(query).strip().lower()
        items = [i for i in items if needle in " ".join(str(i.get(k) or "") for k in
                                                       ("latex", "context", "context_zh")).lower()]
    out = [{
        "page": int(i.get("page") or 0) or None,
        "row_id": i.get("row_id"),
        "latex": str(i.get("latex") or ""),
        "display": bool(i.get("display")),
        "context": _clip(i.get("context") or "", 240),
        "context_zh": _clip(i.get("context_zh") or "", 240),
    } for i in items[:max(1, int(limit or FORMULA_LIMIT))]]
    return {"paper_id": paper_id, "total": len(items), "returned": len(out),
            "source": data.get("source"), "items": out}


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

    if paper_id:
        paper_service.get_paper(paper_id)
        if want in ("all", "notes"):
            for n in (svc.list_notes(paper_id, q=query or None).get("items") or []):
                out.append({"kind": "note", "paper_id": paper_id,
                            "block_id": n.get("block_id"),
                            "selected_text": _clip(n.get("selected_text") or "", 200),
                            "note": _clip(n.get("content_md") or "", 600),
                            "tags": list(n.get("tags") or [])})
        if want in ("all", "highlights"):
            for h in (svc.list_highlights(paper_id).get("items") or []):
                out.append({"kind": "highlight", "paper_id": paper_id,
                            "block_id": h.get("block_id"),
                            "selected_text": _clip(h.get("text") or "", 200),
                            "note": "", "tags": []})
        if want in ("all", "annotations"):
            for a in (svc.list_annotations(paper_id).get("items") or []):
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

        if want in ("all", "notes"):
            for n in (overview.get("notes") or []):
                out.append({"kind": "note", "paper_id": n.get("paper_id"),
                            "paper_title": _title(n),
                            "block_id": n.get("block_id"),
                            "selected_text": _clip(n.get("selected_text") or "", 200),
                            "note": _clip(n.get("content_md") or "", 600),
                            "tags": list(n.get("tags") or [])})
        if want in ("all", "highlights"):
            for h in (overview.get("highlights") or []):
                out.append({"kind": "highlight", "paper_id": h.get("paper_id"),
                            "paper_title": _title(h),
                            "block_id": h.get("block_id"),
                            "selected_text": _clip(h.get("text") or "", 200),
                            "note": "", "tags": []})
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
    return {"paper_id": paper_id, "kind": want, "returned": len(out[:limit]),
            "items": out[:limit]}
