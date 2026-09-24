"""知识库服务（产线 C：chunk 化 → 向量化 → 入库）。

规划 §11 / §21.6：chunk 化 → Embedding → 写入 MySQL chunk 元数据 → 写 LanceDB。
检索（向量/词法/混合/重排/RAG 问答）在阶段 14–17 的后续轮次实现；
本模块负责 chunk 与索引链路，保证「删除论文 → 清索引」与「重建索引」可用。

**双语 chunk（本轮新增）**：同一个 chunk 出 ``zh`` / ``en`` 两行（chunk id 加 ``:zh`` /
``:en`` 后缀，``lang`` 进 ``metadata_json``）—— **不改表结构**（不动 MySQL 列，
避免 ALTER 迁移）。理由见 ``build_chunks_for_paper`` 的 docstring。
"""

from __future__ import annotations

import re

from backend.core.errors import NotFoundError, ValidationError
from backend.core.logging import get_logger
from backend.core.utils import json_dumps, new_id, now, sha256_text, truncate
from backend.repositories.mysql.models import Block, Chunk, Paper
from backend.repositories.mysql.session import session_scope
from backend.services.model.model_manager import model_manager
from backend.services.paper import paper_service, reader_service

log = get_logger(__name__)

# 四类 chunk（规划 §10 ⑫ chunk_type）
CHUNK_TYPES = ("text", "formula", "figure", "table")
# 目标 chunk 字符数（段合并上限）
TARGET_CHUNK_CHARS = 900
MIN_CHUNK_CHARS = 40
# 双语：同一 chunk 的两种语言入库行（顺序即入库顺序，zh 在前便于人肉核对）
LANGS = ("zh", "en")
# 中文侧的最小长度门槛按**字**算：中文 1 字的信息量约等于英文 2~3 字符，沿用 40 字符
# 会把大量正常的中文块误杀（实测把 "## B.2 QJL：1 比特内积量化" 这类小节标题全丢了）。
# 门槛只用来兜"取到的是残渣"，不承担质量判断 —— 对不上译文的行压根不会出（见 _lookup_zh）。
MIN_ZH_CHARS = 8

_WS_RE = re.compile(r"\s+")
# 只保留小写字母 / 数字 / 汉字
_COMPACT_RE = re.compile(r"[^0-9a-z\u4e00-\u9fff]+")
# 页内对齐的最小可信重叠（签名字符数）
_MIN_ALIGN_CHARS = 12


def _block_sig(text: str) -> str:
    """块匹配签名：只留小写字母 / 数字 / 汉字。

    为什么这么狠：**入库的原文来自解析产物**（``md/full.md``），**译文来自页级产物**
    （``pages.json`` 的 ``original_md`` / ``translation_md``，由视觉模型按页图转写）——
    同一页的两次转写，标题层级符（``#`` vs ``##``）、冒号后空格、LaTeX 里的空格
    都会有差异。第一版只压空白，在真实论文上正文块只有 **52%** 能对上（实测），
    换成"只留字母数字汉字"后这些差异全部消失，块与块之间仍然区分得开。
    """
    return _COMPACT_RE.sub("", (text or "").lower())


def _zh_index(pages: list[dict]) -> dict[int, list[tuple[str, str]]]:
    """``{页号: [(行签名, 译文), …]}``：页级翻译产物的译文索引（保持行序）。

    为什么不直接建 ``{(页号, 签名): 译文}`` 字典就完事：解析块与译文行的**切分边界
    不一样**（同一个段落，解析切成"…quantizer's output." + "Thus, we aim…"，页级产物
    切成一整行）。实测只按签名精确匹配，正文块覆盖率只有 55% —— 剩下的不是"没翻译"，
    而是**切分不同**。所以索引保留整页的行序列，由 `_lookup_zh` 做页内对齐。
    """
    out: dict[int, list[tuple[str, str]]] = {}
    for page in pages or []:
        try:
            pno = int(page.get("page") or 0)
        except (TypeError, ValueError):
            continue
        rows: list[tuple[str, str]] = []
        for row in page.get("rows") or []:
            src = _block_sig(row.get("original"))
            dst = str(row.get("translation") or "").strip()
            if src and dst:
                rows.append((src, dst))
        if rows:
            out[pno] = rows
    return out


def _overlap(a: str, b: str) -> int:
    """两个签名的最长公共子序列长度（用 difflib，字符级，够用且稳）。"""
    return _overlap_span(a, b)[0]


def _overlap_span(a: str, b: str) -> tuple[int, int, int]:
    """``(公共字符数, 在 b 中的起始位置, 结束位置)``。"""
    from difflib import SequenceMatcher

    matcher = SequenceMatcher(None, a, b, autojunk=False)
    total, lo, hi = 0, -1, -1
    for tag, _, _, j1, j2 in matcher.get_opcodes():
        if tag != "equal":
            continue
        total += j2 - j1
        lo = j1 if lo < 0 else min(lo, j1)
        hi = max(hi, j2)
    return total, lo, hi


_SENT_END = "。！？；\n"
_SENT_END_ASCII = ".!?;"


def _snap_sentence(text: str, pos: int, *, forward: bool) -> int:
    """把比例切点吸附到最近的句子边界（找不到就原样返回）。"""
    n = len(text)
    pos = max(0, min(n, pos))
    step = 1 if forward else -1
    i = pos
    while 0 < i < n:
        ch = text[i - 1]
        if ch in _SENT_END or (ch in _SENT_END_ASCII and (i >= n or text[i] in " \n")):
            return i
        i += step
    return pos


def _slice_zh(text: str, lo: float, hi: float) -> str:
    """按比例切一段译文，切点吸附到句子边界。

    为什么需要：解析块比页级行**细**（同一段被切成两三个块，页级产物只有一整行），
    不切的话同一个块会拿到整行译文 —— 实测 turboquant 有 21/56 个中文 chunk 的
    token 数是英文侧的两倍以上、还有 6 条内容完全重复（同一段中文被塞进多个 chunk）。
    译文与原位序一致，所以按"该段在行内的字符占比"切，再吸附到最近句号，
    既不重复也不留空档。
    """
    n = len(text)
    if n <= 1:
        return text
    edge = max(3, int(0.05 * n))
    raw_lo, raw_hi = int(round(lo * n)), int(round(hi * n))
    # 贴边的直接用整段边界：否则会从"向|量化"这种词中间切进去（实测发生过）
    start = 0 if raw_lo <= edge else _snap_sentence(text, raw_lo, forward=False)
    end = n if raw_hi >= n - edge else _snap_sentence(text, raw_hi, forward=True)
    if end <= start:
        start, end = raw_lo, raw_hi
    piece = text[start:end].strip()
    return piece or text.strip()


def _lookup_zh(zh: dict[int, list[tuple[str, str]]], page: int, text: str) -> str | None:
    """取某段原文的中文：先精确签名，再在**本页内**做字符级对齐取重叠最多的行。

    为什么是"本页内 + 取最优行"：跨页对齐毫无必要（同一页的行序天然单调），
    而重叠最多的一行就是这段原文所在的行 —— 即使两侧切分不同也不会配到别的小节去。
    门槛（重叠 ≥ 12 字符且 ≥ 较短一侧的一半）保证**宁可漏配也不错配**；
    命中后若"行比这段大得多"（行粗、块细），按比例切片而不是整行端走。
    """
    rows = zh.get(page)
    sig = _block_sig(text)
    if not rows or not sig:
        return None
    for row_sig, dst in rows:
        if row_sig == sig:
            return dst
    if len(sig) < _MIN_ALIGN_CHARS:
        return None
    best: tuple[int, str, str, int, int] | None = None
    for row_sig, dst in rows:
        hit, j1, j2 = _overlap_span(sig, row_sig)
        if best is None or hit > best[0]:
            best = (hit, dst, row_sig, j1, j2)
    if best is None:
        return None
    hit, dst, row_sig, j1, j2 = best
    if hit < _MIN_ALIGN_CHARS or hit * 2 < min(len(sig), len(row_sig)):
        return None
    if len(row_sig) <= len(sig) * 1.2 or j1 < 0:   # 行不比这段大多少 → 整行端走
        return dst
    return _slice_zh(dst, j1 / len(row_sig), j2 / len(row_sig))


def _chunk_variants(base: list[dict], zh: dict[int, list[tuple[str, str]]],
                    langs: tuple[str, ...]) -> list[dict]:
    """把「单语 chunk」展开成 zh/en 两行。

    译文缺失的处理：正文块 / 图注块**翻译没做就不出 zh 行**（不拿原文冒充译文，
    否则索引里会出现"中文行其实是英文"的脏数据）；公式块的语言无关（只有 LaTeX），
    两侧同文本、照出。
    """
    out: list[dict] = []
    for c in base:
        parts: list[tuple[int, str]] = c.get("_parts") or []
        for lang in langs:
            if lang == "en":
                content = c["content"]
            elif c["chunk_type"] == "formula":
                content = c["content"]            # LaTeX 无语言之分
            else:
                pieces = [_lookup_zh(zh, p, t) for p, t in parts]
                pieces = [p for p in pieces if p]
                # 相邻块共用一个译文行（切分不同时的正常现象）→ 去重，别把同一段中文塞两次
                deduped: list[str] = []
                for piece in pieces:
                    if not deduped or deduped[-1] != piece:
                        deduped.append(piece)
                pieces = deduped
                if not pieces or not parts:
                    continue                      # 还没翻译 → 不出 zh 行
                if len(pieces) < len(parts):
                    log.debug("chunk %s 的 zh 侧只对上 %d/%d 块（其余未翻译或切分不同）",
                              c["id"], len(pieces), len(parts))
                content = "\n\n".join(pieces)
            # 长度门槛只对原文侧用；中文侧按字数另算（MIN_ZH_CHARS）
            floor = MIN_CHUNK_CHARS if lang == "en" else MIN_ZH_CHARS
            if c["chunk_type"] == "text" and len(content) < floor:
                continue
            out.append({
                **{k: v for k, v in c.items() if not k.startswith("_")},
                "id": f"{c['id']}:{lang}",
                "lang": lang,
                "content": content,
                "content_hash": sha256_text(content),
                "content_preview": truncate(content, 200),
                "token_count": _approx_tokens(content),
                "metadata": {**(c.get("metadata") or {}), "lang": lang,
                             "pair_id": c["id"]},
            })
    return out


# --------------------------------------------------------------------------
# Chunk 化（规划 §21.6 步骤 1）
# --------------------------------------------------------------------------


def build_chunks_for_paper(paper_id: str, *,
                           langs: tuple[str, ...] = LANGS) -> list[dict]:
    """按 IR + Markdown 生成四类 chunk，并展开成 zh/en 双语行。

    为什么要双语：库内正文的事实源是**页级产物**（``pages.json`` 有原文 + 译文），
    而中文提问与英文原文之间隔着一次跨语检索 —— 模型的跨语能力能用，但实测（§17.33）
    中文问句对中文段落的相似度明显更"实"；反之英文术语（MSE、inner product）在英文行上
    更准。两行都入库、由重排统一裁决，是召回最稳的做法。

    **不改表结构**：chunk id 加 ``:zh`` / ``:en`` 后缀，``lang`` 只写进 ``metadata_json``
    （MySQL ``chunk`` 表不加列，省一次 ALTER 迁移）。
    """
    return _chunk_variants(_collect_chunks(paper_id),
                           _zh_index(reader_service.page_translations(paper_id)),
                           langs)


def _collect_chunks(paper_id: str) -> list[dict]:
    """单语（原文）chunk：与旧实现一致，只是额外留下 ``_parts`` 供译文对齐。"""
    md_map = reader_service.load_markdown_map(paper_id)
    from backend.parsing.ir.ir_reader import IRReader

    ir = IRReader(paper_service.paper_paths(paper_id)["ir"])
    formulas = {f["block_id"]: f for f in ir.formulas()}
    figures = {f["block_id"]: f for f in ir.figures()}
    tables = {t["block_id"]: t for t in ir.tables()}

    with session_scope() as sess:
        blocks = (sess.query(Block).filter(Block.paper_id == paper_id)
                  .order_by(Block.order_global.asc()).all())
        rows = [_block_row(b, md_map) for b in blocks]

    chunks: list[dict] = []
    buffer: list[dict] = []
    buffer_chars = 0

    def flush_text() -> None:
        nonlocal buffer, buffer_chars
        if not buffer:
            return
        text = "\n\n".join(b["text"] for b in buffer if b["text"])
        if len(text) >= MIN_CHUNK_CHARS:
            chunks.append(_make_chunk(
                paper_id, "text", text, buffer[0], buffer[-1],
                section=buffer[0].get("section") or "",
                parts=[(b["page"], b["text"]) for b in buffer if b["text"]],
                metadata={"block_ids": [b["id"] for b in buffer],
                          "pages": sorted({b["page"] for b in buffer})},
            ))
        buffer = []
        buffer_chars = 0

    for b in rows:
        bid = b["id"]
        if b["type"] == "heading":
            flush_text()
            buffer = [b]
            buffer_chars = len(b["text"])
            continue
        if b["type"] in ("ref",):
            continue
        if bid in formulas:
            flush_text()
            f = formulas[bid]
            latex = f.get("latex_final") or f.get("latex_repaired") or ""
            cands = f.get("latex_candidate") or []
            text = f"公式 {bid}：{latex or (cands[0] if cands else f.get('raw_text') or '')}"
            chunks.append(_make_chunk(
                paper_id, "formula", text, b, b, section=b.get("section") or "",
                metadata={"latex": latex, "grade": f.get("grade"),
                          "raw_text": f.get("raw_text"),
                          "crop": f.get("image_path")},
            ))
            continue
        if bid in figures:
            flush_text()
            f = figures[bid]
            caption = f.get("caption") or ""
            text = f"插图 {bid}：{caption}"
            chunks.append(_make_chunk(
                paper_id, "figure", text, b, b, section=b.get("section") or "",
                # 图注本身是可翻译的正文块 → 拿它的签名去译文索引里取中文图注
                parts=[(b["page"], caption)] if caption else [],
                metadata={"caption": caption, "image_path": f.get("image_path")},
            ))
            continue
        if bid in tables:
            flush_text()
            t = tables[bid]
            caption = t.get("caption") or ""
            text = f"表格 {bid}：{caption}"
            chunks.append(_make_chunk(
                paper_id, "table", text, b, b, section=b.get("section") or "",
                parts=[(b["page"], caption)] if caption else [],
                metadata={"caption": caption, "image_path": t.get("image_path")},
            ))
            continue

        text = b["text"] or ""
        if not text:
            continue
        buffer.append(b)
        buffer_chars += len(text)
        if buffer_chars >= TARGET_CHUNK_CHARS:
            flush_text()
    flush_text()
    return chunks


def _block_row(b: Block, md_map: dict | None = None) -> dict:
    import json

    meta = {}
    try:
        meta = json.loads(b.meta_json or "{}")
    except ValueError:
        meta = {}
    # 正文只在 Markdown（规划 §8 铁律），MySQL 里不存正文
    text = ((md_map or {}).get(b.id, {}) or {}).get("text") or b.preview or ""
    return {"id": b.id, "page": b.page_no, "type": b.type, "order": b.order_global,
            "text": text, "hash": b.content_hash,
            "section": meta.get("section_title") or b.section_id or ""}


def _make_chunk(paper_id: str, chunk_type: str, content: str, first: dict,
                last: dict, *, section: str = "", metadata: dict | None = None,
                parts: list[tuple[int, str]] | None = None) -> dict:
    cid = f"{paper_id}:{chunk_type}:{first['id']}"
    if chunk_type == "text" and first["id"] != last["id"]:
        cid = f"{paper_id}:text:{first['id']}-{last['id']}"
    return {
        "id": cid, "paper_id": paper_id, "chunk_type": chunk_type,
        "page_no": first["page"], "order_global": first["order"],
        "block_id": first["id"], "end_block_id": last["id"],
        "section": section, "content": content,
        "content_hash": sha256_text(content),
        "content_preview": truncate(content, 200),
        "token_count": _approx_tokens(content),
        "metadata": metadata or {},
        "_parts": parts or [],
    }


def _approx_tokens(text: str) -> int:
    cjk = sum(1 for c in text if "\u4e00" <= c <= "\u9fff")
    return int(cjk / 1.5 + (len(text) - cjk) / 4) + 1


# --------------------------------------------------------------------------
# 入库（规划 §21.6 步骤 2–5）
# --------------------------------------------------------------------------


def index_paper(paper_id: str, *, rebuild: bool = False,
                embed: bool = True, progress=None) -> dict:
    """EMBED + INDEX：写 MySQL chunk 元数据 + 生成向量 + 写 LanceDB。"""
    paper_service.get_paper(paper_id)
    chunks = build_chunks_for_paper(paper_id)
    if not chunks:
        raise ValidationError(
            f"没有可用 chunk：{paper_id} 是否尚未解析？")

    cfg = model_manager.get_config(None, "embedding")
    model_id = cfg.get("id")
    dim = (cfg.get("parameters") or {}).get("dim")

    # 索引版本号（换模型即重建，规划 §11.3）
    with session_scope() as sess:
        row = sess.get(Paper, paper_id)
        prev = sess.query(Chunk).filter(Chunk.paper_id == paper_id).first()
        index_version = int(prev.index_version or 0) + 1 if prev else 1
        sess.query(Chunk).filter(Chunk.paper_id == paper_id).delete()
        for c in chunks:
            sess.add(Chunk(
                id=c["id"], paper_id=paper_id, block_id=c["block_id"],
                chunk_type=c["chunk_type"], page_no=c["page_no"],
                order_global=c["order_global"], section=c["section"],
                content_preview=c["content_preview"],
                content_hash=c["content_hash"], token_count=c["token_count"],
                index_version=index_version, embedding_model_id=model_id,
                metadata_json=json_dumps(c["metadata"]),
            ))
        if row is not None and row.status not in ("translated", "indexed"):
            if row.status == "parsed":
                row.status = "parsed"

    embedded = 0
    vector_error = None
    if embed:
        try:
            provider = model_manager.get_embedding()
            batch = int((cfg.get("parameters") or {}).get("batch") or 32)
            vectors: list[list[float]] = []
            for i in range(0, len(chunks), batch):
                part = chunks[i:i + batch]
                vectors.extend(provider.embed([c["content"] for c in part]))
                if progress:
                    progress(min(i + batch, len(chunks)), len(chunks))
            dim = len(vectors[0]) if vectors else dim
            model_manager.remember_dim(cfg, dim)
            from backend.repositories.vector.lancedb_store import vector_store

            vector_store.upsert_chunks(chunks, vectors, index_version,
                                       embedding_model_id=model_id)
            embedded = len(vectors)
        except Exception as exc:  # noqa: BLE001
            vector_error = f"{type(exc).__name__}: {str(exc)[:300]}"
            log.warning("向量入库失败（MySQL chunk 元数据已写入）：%s", exc)

    return {
        "paper_id": paper_id, "chunks": len(chunks),
        "embedded": embedded, "index_version": index_version,
        "model_id": model_id, "dim": dim,
        "by_type": _count_by_type(chunks),
        "by_lang": _count_by_lang(chunks),
        "vector_error": vector_error,
        "rebuilt": rebuild,
        "at": now().isoformat(timespec="seconds"),
    }


def _count_by_type(chunks: list[dict]) -> dict:
    out: dict[str, int] = {}
    for c in chunks:
        out[c["chunk_type"]] = out.get(c["chunk_type"], 0) + 1
    return out


def _count_by_lang(chunks: list[dict]) -> dict:
    out: dict[str, int] = {}
    for c in chunks:
        lang = c.get("lang") or "en"
        out[lang] = out.get(lang, 0) + 1
    return out


def overview() -> dict:
    """知识库概览（规划 §20.5）。"""
    from sqlalchemy import func

    with session_scope() as sess:
        papers = sess.query(Paper).filter(Paper.deleted_at.is_(None)).all()
        chunk_count = sess.query(func.count(Chunk.id)).scalar() or 0
        by_type = dict(sess.query(Chunk.chunk_type, func.count(Chunk.id))
                       .group_by(Chunk.chunk_type).all())
        # 双语行数按 id 后缀数（`…:zh` / `…:en`）—— 不用解析 metadata_json，
        # 免得依赖 JSON 的序列化空格这种细节（规划 §8：一处事实源）
        by_lang = {
            "zh": int(sess.query(func.count(Chunk.id))
                      .filter(Chunk.id.like("%:zh")).scalar() or 0),
            "en": int(sess.query(func.count(Chunk.id))
                      .filter(Chunk.id.like("%:en")).scalar() or 0),
        }
        index_versions = dict(sess.query(Chunk.paper_id, func.max(Chunk.index_version))
                              .group_by(Chunk.paper_id).all())
        model_ids = [m for (m,) in sess.query(Chunk.embedding_model_id).distinct().all()]
    vector = None
    try:
        from backend.repositories.vector.lancedb_store import vector_store

        vector = vector_store.stats()
    except Exception:  # noqa: BLE001
        vector = {"available": False}
    return {
        "papers_total": len(papers),
        "papers_in_kb": sum(1 for p in papers if p.in_knowledge_base),
        "chunks": int(chunk_count),
        "chunks_by_type": by_type,
        "chunks_by_lang": by_lang,
        "embedding_models": [m for m in model_ids if m],
        "index_versions": index_versions,
        "vector_store": vector,
        "items": _overview_items(papers),
    }


def _overview_items(papers: list[Paper]) -> list[dict]:
    from sqlalchemy import func

    with session_scope() as sess:
        counts = dict(sess.query(Chunk.paper_id, func.count(Chunk.id))
                      .group_by(Chunk.paper_id).all())
    return [{"id": p.id, "title": p.title, "status": p.status,
             "in_knowledge_base": bool(p.in_knowledge_base),
             "chunks": int(counts.get(p.id, 0))} for p in papers]


def set_kb_membership(paper_id: str, include: bool) -> dict:
    with session_scope() as sess:
        row = sess.get(Paper, paper_id)
        if row is None or row.deleted_at is not None:
            raise NotFoundError(f"论文不存在：{paper_id}")
        row.in_knowledge_base = include
        sess.flush()
        return {"paper_id": paper_id, "in_knowledge_base": include}


def index_status(paper_id: str | None = None) -> dict:
    from sqlalchemy import func

    with session_scope() as sess:
        q = sess.query(Chunk)
        if paper_id:
            q = q.filter(Chunk.paper_id == paper_id)
        total = q.count()
        with_hash = q.filter(Chunk.content_hash.isnot(None)).count()
        versions = (sess.query(Chunk.paper_id, func.max(Chunk.index_version))
                    .group_by(Chunk.paper_id).all())
    vector = None
    try:
        from backend.repositories.vector.lancedb_store import vector_store

        vector = {"available": True, "count": vector_store.count(paper_id=paper_id)}
    except Exception as exc:  # noqa: BLE001
        vector = {"available": False, "error": str(exc)[:200]}
    return {"paper_id": paper_id, "chunks": total, "chunks_with_hash": with_hash,
            "index_versions": dict(versions), "vector": vector}


def new_chunk_id(paper_id: str, chunk_type: str) -> str:
    return f"{paper_id}:{chunk_type}:{new_id('c')[-8:]}"
