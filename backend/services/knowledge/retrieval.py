"""检索内核（规划 §19.1）：向量召回 + 词法 BM25 → RRF 融合 → 重排降级链。

三层各自的定位（也是为什么不省任何一层）：

- **向量召回**：语义相近的段落（中文问句 ↔ 中文 chunk 靠它）；查询侧带 instruct、
  文档侧入库时不带（Qwen3 系列的约定，见 provider docstring）。
- **词法 BM25**：专有名词、符号、`MSE` / `TurboQuant` 这类"必须在字面上出现"的东西，
  向量召回经常漏；库里只有几千条 chunk，Python 侧 BM25 足够快，不引外部全文索引。
- **RRF 融合**：两路分数不可比（余弦 ∈ [-1,1]、BM25 无上界），只按**名次**融合
  （k=60，Cormack 2009），避免为"分数归一化"再引入一套要调的参数。
- **重排降级链**：`qwen3-vl-rerank` 优先；没有配置模型或调用失败 → 词法启发式重排，
  并在 `retrieval.rerank` 里**标明降级**（不能让"降级结果"看起来像模型结果）。
"""

from __future__ import annotations

import math
import re
from typing import Any

from backend.core.logging import get_logger
from backend.repositories.vector.lancedb_store import vector_store
from backend.services.model.model_manager import model_manager

log = get_logger(__name__)

# RRF 常数（Cormack et al. 2009 的经验值）
RRF_K = 60
# 检索默认规模（/settings 可覆盖：rag_top_k_vector / rag_top_k_lexical /
# rag_top_k_fused / rag_top_n_rerank）
DEFAULT_K = {"vector": 20, "lexical": 20, "fused": 12, "rerank": 6}

# 查询侧 instruct（入库侧**不能**带，否则两侧语义整体偏移）
QUERY_INSTRUCT = "Given a web search query, retrieve relevant passages that answer the query."
SIMILAR_INSTRUCT = "Retrieve semantically similar text."

_WORD_RE = re.compile(r"[a-zA-Z0-9_]+")
_CJK_RE = re.compile(r"[\u4e00-\u9fff]")


# --------------------------------------------------------------------------
# 参数（/settings 存 setting 表；服务层直接读表，不反向依赖 api 层）
# --------------------------------------------------------------------------


#: 设置键的统一前缀。文档与界面一律用 `kb.` 写法（`kb.retrieval_types`、`kb.unit_expand`），
#: 但早期代码读的是**裸键**（`retrieval_types`）—— 于是"文档里承诺可以调的开关其实一直没生效"，
#: 实测这两条都从未被读过（dev-log §17.66）。现在全库只有一条规则：
#: `setting(key)` 先找 `kb.<key>`、再回退裸键 `<key>`，两个都没有才用默认值。
SETTING_PREFIX = "kb."

#: "这条设置不存在"的哨兵 —— 必须与"存在但值是 None"区分开，否则回退链会在第一层就停住。
_MISSING: Any = object()


def _setting_raw(key: str) -> Any:
    """读一条 setting 的原始值；键不存在或读失败都返回 `_MISSING`。"""
    try:
        from backend.core.utils import json_loads
        from backend.repositories.mysql.models import Setting
        from backend.repositories.mysql.session import session_scope

        with session_scope() as sess:
            row = sess.get(Setting, key)
            if row is None:
                return _MISSING
            return json_loads(row.value, row.value)
    except Exception:  # noqa: BLE001 —— 设置读不到不影响检索
        return _MISSING


def setting(key: str, default: Any) -> Any:
    """读设置。``key`` 一律用**裸名**传（`unit_expand`），前缀由这里补。

    为什么不"只认 `kb.` 一种写法"：老库里可能有裸键的行（早期脚本写进去的），
    只认一种写法会让老行**静默失效** —— 而"静默失效"正是这次要修掉的那个毛病。
    """
    for name in (f"{SETTING_PREFIX}{key}", key):
        value = _setting_raw(name)
        if value is not _MISSING:
            return value
    return default


def setting_int(key: str, default: int) -> int:
    """读整数设置；坏值（非数字 / 空）回退默认。"""
    try:
        return int(float(str(setting(key, default)).strip().strip('"')))
    except (TypeError, ValueError):
        return default


def setting_flag(key: str, default: bool) -> bool:
    """读布尔设置：``1/true/yes/on`` 为真、``0/false/no/off`` 为假，其余回退默认。

    `true`/`false` 两种情况都要认，因为写入路径不同：`PUT /settings` 写的是 JSON
    （读出来是真正的 bool），而脚本/手改写的是文本（读出来是字符串）。
    """
    raw = setting(key, default)
    if isinstance(raw, bool):
        return raw
    text = str(raw).strip().strip('"').lower()
    if text in ("1", "true", "yes", "on"):
        return True
    if text in ("0", "false", "no", "off", ""):
        return False
    return default


def rag_k(name: str) -> int:
    return int(setting(f"rag_top_k_{name}" if name != "rerank" else "rag_top_n_rerank",
                       DEFAULT_K[name]) or DEFAULT_K[name])


# --------------------------------------------------------------------------
# 分词（中文靠 jieba，英文走正则；两者可以混在一段里）
# --------------------------------------------------------------------------

_TOKEN_CACHE: dict[str, list[str]] = {}
_TOKEN_CACHE_MAX = 4096


def tokenize(text: str) -> list[str]:
    """切词：中文用 jieba **并补上单字**，英文片段走正则。

    为什么要补单字：jieba 会把"向量量化"切成一个词，于是查询"量化"在词级永远匹配不上
    含"向量量化"的段落（实测 jieba 就是这么切的）。单字兜底解决这一类包含关系，
    高频字（的 / 在 / 是）的噪声由 idf 自然压下去。
    """
    if not text:
        return []
    out: list[str] = []
    if _CJK_RE.search(text):
        try:
            import jieba

            for tok in jieba.lcut(text):
                tok = tok.strip().lower()
                if not tok:
                    continue
                if tok.isalnum() or _CJK_RE.search(tok):
                    out.append(tok)
                out.extend(c for c in tok if _CJK_RE.match(c))
            return out
        except ImportError:  # pragma: no cover - jieba 是必需依赖
            pass
    return [t.lower() for t in _WORD_RE.findall(text)]


def _tokens_cached(content: str) -> list[str]:
    """按**内容**缓存分词结果（不能按 chunk_id：chunk id 是位置编号，
    重新翻译 / 重新解析后同一 id 的内容会变，按 id 缓存会拿到上一版的词）。"""
    key = f"{len(content)}:{hash(content)}"
    hit = _TOKEN_CACHE.get(key)
    if hit is not None:
        return hit
    tokens = tokenize(content)
    if len(_TOKEN_CACHE) > _TOKEN_CACHE_MAX:
        _TOKEN_CACHE.clear()
    _TOKEN_CACHE[key] = tokens
    return tokens


# --------------------------------------------------------------------------
# 1) 向量召回
# --------------------------------------------------------------------------


def query_vector(query: str, *, mode: str = "similar") -> list[float]:
    """把查询编码成向量；查询侧带 instruct（模型不支持就退回普通调用）。"""
    provider = model_manager.get_embedding()
    instruct = QUERY_INSTRUCT if mode == "qa" else SIMILAR_INSTRUCT
    try:
        vectors = provider.embed([query], instruct=instruct)
    except TypeError:
        vectors = provider.embed([query])
    return vectors[0] if vectors else []


def vector_search(query: str, *, top_k: int | None = None, mode: str = "similar",
                  paper_ids: list[str] | None = None,
                  chunk_types: list[str] | None = None,
                  langs: list[str] | None = None,
                  roads: list[str] | None = None,
                  exclude_paper: str | None = None,
                  exclude_chunks: list[str] | None = None) -> list[dict]:
    top_k = top_k or rag_k("vector")
    vec = query_vector(query, mode=mode)
    if not vec:
        return []
    hits = vector_store.search(vec, top_k=top_k, paper_ids=paper_ids,
                               chunk_types=chunk_types, langs=langs, roads=roads,
                               exclude_paper=exclude_paper)
    for h in hits:
        # LanceDB 的 cosine 距离 → 相似度（越大越像）
        distance = h.get("score")
        h["vector_score"] = round(1.0 - float(distance), 6) if distance is not None else None
    if exclude_chunks:
        skip = set(exclude_chunks)
        hits = [h for h in hits if h.get("chunk_id") not in skip]
    return hits


# --------------------------------------------------------------------------
# 2) 词法召回（BM25；整表在内存里打分，本库规模几千条）
# --------------------------------------------------------------------------


def _filter_rows(rows: list[dict], *, paper_ids: list[str] | None,
                 chunk_types: list[str] | None, langs: list[str] | None,
                 roads: list[str] | None = None,
                 exclude_paper: str | None, exclude_chunks: list[str] | None) -> list[dict]:
    skip = set(exclude_chunks or [])
    out = []
    for r in rows:
        if paper_ids and r.get("paper_id") not in paper_ids:
            continue
        if chunk_types and r.get("chunk_type") not in chunk_types:
            continue
        if roads and (r.get("road") or "") not in roads and (r.get("road") or "") != "":
            # 共用块（road=""：图/表/公式）在任何一路的检索里都要出现 —— 它们本来就不属于
            # 某一路，被过滤掉等于"按路检索就检不到公式"。
            continue
        if langs and (r.get("lang") or "en") not in langs and \
                (r.get("lang") or "en") != "any":
            # `any` = 语言无关（公式原子行）：中文、英文检索都必须召回它，
            # 否则用户按语言过滤时公式会凭空消失（旧实现靠 zh/en 各存一份绕过，纯浪费）。
            continue
        if exclude_paper and r.get("paper_id") == exclude_paper:
            continue
        if r.get("chunk_id") in skip:
            continue
        out.append(r)
    return out


def lexical_search(query: str, *, top_k: int | None = None,
                   paper_ids: list[str] | None = None,
                   chunk_types: list[str] | None = None,
                   langs: list[str] | None = None,
                   roads: list[str] | None = None,
                   exclude_paper: str | None = None,
                   exclude_chunks: list[str] | None = None,
                   k1: float = 1.5, b: float = 0.75) -> list[dict]:
    """BM25 词法召回。库小 → 每次整表打分（永远是最新的，没有索引不一致问题）。"""
    top_k = top_k or rag_k("lexical")
    q_tokens = tokenize(query)
    if not q_tokens:
        return []
    rows = _filter_rows(vector_store.all_rows(), paper_ids=paper_ids,
                        chunk_types=chunk_types, langs=langs, roads=roads,
                        exclude_paper=exclude_paper, exclude_chunks=exclude_chunks)
    if not rows:
        return []
    docs: list[list[str]] = []
    df: dict[str, int] = {}
    for r in rows:
        tokens = _tokens_cached(r.get("content") or "")
        docs.append(tokens)
        for term in set(tokens):
            df[term] = df.get(term, 0) + 1
    n = len(docs)
    avgdl = sum(len(d) for d in docs) / n if n else 0.0
    scored: list[tuple[float, int]] = []
    for i, tokens in enumerate(docs):
        if not tokens:
            continue
        tf: dict[str, int] = {}
        for tok in tokens:
            tf[tok] = tf.get(tok, 0) + 1
        score = 0.0
        dl = len(tokens)
        for term in q_tokens:
            f = tf.get(term)
            if not f:
                continue
            idf = math.log(1 + (n - df.get(term, 0) + 0.5) / (df.get(term, 0) + 0.5))
            score += idf * (f * (k1 + 1)) / (f + k1 * (1 - b + b * dl / (avgdl or 1)))
        if score > 0:
            scored.append((score, i))
    scored.sort(key=lambda x: (-x[0], rows[x[1]].get("order_global") or 0))
    out = []
    for score, i in scored[:top_k]:
        row = dict(rows[i])
        row["lexical_score"] = round(score, 6)
        out.append(row)
    return out


# --------------------------------------------------------------------------
# 3) RRF 融合
# --------------------------------------------------------------------------


def rrf_fuse(rankings: dict[str, list[dict]], *, k: int = RRF_K,
             weights: dict[str, float] | None = None,
             top_k: int | None = None) -> list[dict]:
    """按名次融合多路召回（分数不可比，所以只看名次）。

    ``rankings`` 形如 ``{"vector": [...], "lexical": [...]}``，各列表**已按名次排好**。

    **按"逻辑 chunk"聚合**：双语索引里同一个 chunk 有 zh / en 两行，两行都会参与召回。
    若不聚合，同一个 chunk 会用掉 top-k 里的两个位置（实测：查"QJL 1-bit inner product
    quantization"，前两名就是同一个标题块的 en 行与 zh 行）。这里：
      · RRF 分数**按 pair 求和**（两种语言都召回 = 更强的相关信号）；
      · 展示用的内容取**名次最好的那一行**（默认就是与查询同语言的那行）；
      · 记下 ``langs`` 供前端标注"中/英都命中"。
    """
    weights = weights or {}
    merged: dict[str, dict] = {}
    content_fields = ("paper_id", "chunk_type", "page_no", "block_id", "section",
                      "lang", "content", "metadata")
    for source, items in rankings.items():
        weight = float(weights.get(source, 1.0))
        for rank, item in enumerate(items, start=1):
            cid = item.get("chunk_id")
            if not cid:
                continue
            pair = (item.get("metadata") or {}).get("pair_id") or cid
            node = merged.get(pair)
            contribution = weight / (k + rank)
            if node is None:
                node = merged[pair] = {
                    **{f: item.get(f) for f in content_fields},
                    "chunk_id": cid, "pair_id": pair,
                    "langs": [item.get("lang") or "en"],
                    "sources": {source: rank},
                    "rrf": contribution, "best_rank": rank,
                }
            else:
                node["rrf"] += contribution
                node["sources"][source] = min(node["sources"].get(source, 1 << 30), rank)
                lang = item.get("lang") or "en"
                if lang not in node["langs"]:
                    node["langs"].append(lang)
                if rank < node["best_rank"]:      # 这一路名次更好 → 换展示用的那一行
                    node.update({f: item.get(f) for f in content_fields})
                    node["chunk_id"] = cid
                    node["best_rank"] = rank
            for key in ("vector_score", "lexical_score"):
                if item.get(key) is not None:
                    node[key] = max(node.get(key) or float("-inf"), item[key])
    out = sorted(merged.values(), key=lambda x: -x["rrf"])
    for i, node in enumerate(out, start=1):
        node["rrf_rank"] = i
        node["rrf"] = round(node["rrf"], 8)
        node.pop("best_rank", None)
    return out[:top_k] if top_k else out


# --------------------------------------------------------------------------
# 4) 重排（降级链）
# --------------------------------------------------------------------------


def _rerank_call(provider, query: str, documents: list[str], top_n: int,
                 instruct: str | None) -> list[tuple[int, float]]:
    """调用重排；provider 支持 instruct 就带上（词法启发式那类不支持）。"""
    try:
        return provider.rerank(query, documents, top_n=top_n, instruct=instruct)
    except TypeError:
        return provider.rerank(query, documents, top_n=top_n)


def rerank_hits(query: str, candidates: list[dict], *, top_n: int | None = None,
                mode: str = "similar") -> tuple[list[dict], dict]:
    """重排候选并返回 ``(排序后的候选, 元信息)``；失败自动降级到词法启发式。"""
    top_n = top_n or rag_k("rerank")
    if not candidates:
        return [], {"model": None, "degraded": False, "reason": "", "candidates": 0}
    docs = [c.get("content") or "" for c in candidates]
    instruct = QUERY_INSTRUCT if mode == "qa" else SIMILAR_INSTRUCT
    info: dict = {"candidates": len(candidates), "degraded": False, "reason": ""}
    ranked: list[tuple[int, float]] = []
    try:
        cfg = model_manager.get_config(None, "reranker")
        provider = model_manager.get_reranker()
        info["model"] = provider.model_name
        info["provider"] = cfg.get("provider")
        ranked = _rerank_call(provider, query, docs, top_n, instruct)
        if (cfg.get("provider") or "") in ("mock", "heuristic"):
            # 根本没配模型 → 结果就是启发式的，必须标明，别让它看起来像模型打分
            info["degraded"] = True
            info["reason"] = "未配置重排模型，使用词法启发式（规划 §19.1 降级）"
    except Exception as exc:  # noqa: BLE001 —— 重排失败不该让整次检索失败
        from backend.providers.embedding.providers import HeuristicReranker

        log.warning("重排失败，降级词法启发式：%s", exc)
        info.update({"model": "heuristic-overlap", "provider": "heuristic",
                     "degraded": True, "reason": f"{type(exc).__name__}: {str(exc)[:160]}"})
        ranked = HeuristicReranker({}, None).rerank(query, docs, top_n=top_n)

    out: list[dict] = []
    for rank, (idx, score) in enumerate(ranked, start=1):
        if idx < 0 or idx >= len(candidates):
            continue
        node = dict(candidates[idx])
        node["rerank_score"] = round(float(score), 6)
        node["rerank_rank"] = rank
        out.append(node)
    info["reranked"] = len(out)
    return out[:top_n], info


# --------------------------------------------------------------------------
# 5) 编排：向量 + 词法 → RRF → 重排
# --------------------------------------------------------------------------


def search(query: str, *, mode: str = "similar", top_k: int | None = None,
           scope: str = "all", paper_id: str | None = None,
           chunk_types: list[str] | None = None, langs: list[str] | None = None,
           roads: list[str] | None = None,
           exclude_chunks: list[str] | None = None,
           vector_k: int | None = None, lexical_k: int | None = None,
           fused_k: int | None = None) -> dict:
    """完整检索链路。``scope``：``all`` 全库 / ``cross`` 跨文献（排除来源论文）/
    ``paper`` 只在本篇内找。

    **默认候选集**（``chunk_types=None`` 时）：``unit, formula, figure, table``
    —— 单元是检索单位（fine-grained），公式/图/表本来就是不可切的原子块；
    正文 chunk **不进候选**：它和它的单元是同一批行的两种粒度，都进候选就会出现
    "同一段内容两条命中"，Top-K 被自己挤掉（它是上下文单位，由 RAG 侧按需取，见 `rag_service`）。
    过滤后一条都没有（老索引、或没建单元）→ **自动用全部类型重搜一次**，行为回到改造前。

    **`roads`**：``None``（默认）不按路过滤 —— 两路的单元各说各的语言，让**重排**去裁决
    与"中英各出一行"的既有做法一致，也是召回最稳的口径。传 ``["b"]`` 之类才会只在一路里找。
    共用块（图/表/公式，``road=""``）在任何过滤下都会带回来。
    """
    query = (query or "").strip()
    if not query:
        return {"items": [], "retrieval": {"error": "空查询"}}
    paper_ids = [paper_id] if (scope == "paper" and paper_id) else None
    exclude_paper = paper_id if scope == "cross" else None
    explicit_types = chunk_types is not None
    types = chunk_types if explicit_types else default_search_types()
    notes: list[str] = []
    if not explicit_types and types and not _has_chunk_type("unit"):
        # 老索引（还没建单元层）→ 不限制类型：否则正文也被排除，检索会比改造前更差
        notes.append("索引里还没有 unit 行（未重建）→ 本次不限候选类型")
        types = None

    common_kwargs = dict(langs=langs, roads=roads, paper_ids=paper_ids,
                         exclude_paper=exclude_paper, exclude_chunks=exclude_chunks)
    vec_hits = vector_search(query, top_k=vector_k or rag_k("vector"), mode=mode,
                             chunk_types=types, **common_kwargs)
    lex_hits = lexical_search(query, top_k=lexical_k or rag_k("lexical"),
                              chunk_types=types, **common_kwargs)
    fused = rrf_fuse({"vector": vec_hits, "lexical": lex_hits},
                     top_k=fused_k or rag_k("fused"))
    items, rerank_info = rerank_hits(query, fused, top_n=top_k or rag_k("rerank"),
                                     mode=mode)
    fell_back = False
    if not items and not explicit_types and types:
        # 兜底：候选集过滤后空手而归 → 放开类型限制重搜一次（保证"没建单元也检得到"）
        fell_back = True
        vec_hits = vector_search(query, top_k=vector_k or rag_k("vector"), mode=mode,
                                 **common_kwargs)
        lex_hits = lexical_search(query, top_k=lexical_k or rag_k("lexical"),
                                  **common_kwargs)
        fused = rrf_fuse({"vector": vec_hits, "lexical": lex_hits},
                         top_k=fused_k or rag_k("fused"))
        items, rerank_info = rerank_hits(query, fused, top_n=top_k or rag_k("rerank"),
                                         mode=mode)
        notes.append("默认候选集（unit/formula/figure/table）空结果 → 已放开全部类型重搜")
    if langs and not vector_store.has_lang():
        notes.append("索引还没有 lang 列（旧索引）：本次语言过滤未生效，重建索引后可用")
    if roads and not vector_store.has_road():
        notes.append("索引还没有 road 列（旧索引）：本次路过滤未生效，重建索引后可用")
    if rerank_info.get("degraded") and rerank_info.get("reason"):
        notes.append(str(rerank_info["reason"]))
    return {
        "items": items,
        "retrieval": {
            "query": query, "mode": mode, "scope": scope,
            "chunk_types": list(types) if types else None,
            "roads": list(roads) if roads else None,
            "type_fallback": fell_back,
            "vector_hits": len(vec_hits), "lexical_hits": len(lex_hits),
            "fused": len(fused), "returned": len(items),
            "rrf_k": RRF_K,
            "vector_model": _model_name("embedding"),
            "rerank": rerank_info,
            "notes": notes,
        },
    }


def _has_chunk_type(chunk_type: str) -> bool:
    """索引里有没有这一类 chunk（探针失败时**当作有**，避免误退回全类型而改变行为）。"""
    try:
        return bool(vector_store.has_type(chunk_type))
    except Exception:  # noqa: BLE001
        return True


#: 默认检索候选集：单元（细粒度检索单位）+ 三个不可切的原子类型（公式/图/表）
DEFAULT_SEARCH_TYPES = ("unit", "formula", "figure", "table")


def default_search_types() -> list[str]:
    """``kb.retrieval_types``（逗号分隔）覆盖默认候选集；空串 = 不限类型（改造前行为）。

    键名解析见 `setting`（`kb.` 前缀优先、裸键兼容）—— 这一条以前读的是裸键
    `retrieval_types`，于是 `kb.retrieval_types` 从来没生效过（dev-log §17.66）。
    """
    raw = setting("retrieval_types", "")
    if isinstance(raw, (list, tuple)):
        return [str(t).strip() for t in raw if str(t).strip()]
    text = str(raw or "").strip()
    if text:
        return [t.strip() for t in text.split(",") if t.strip()]
    return list(DEFAULT_SEARCH_TYPES)


def _model_name(model_type: str) -> str | None:
    try:
        cfg = model_manager.get_config(None, model_type)
        return f"{cfg.get('provider')}:{cfg.get('model_name')}"
    except Exception:  # noqa: BLE001
        return None
