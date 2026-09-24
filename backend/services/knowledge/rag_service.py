"""RAG 问答（规划 §19.3–§19.5、§20.4）：ContextBuilder → 生成 → AnswerValidator。

三段各自的职责：

- **ContextBuilder**：把检索结果整理成**带编号的来源**（`[S1]`…），并把双语都给进去
  （同一 chunk 的原文 + 译文）——中文提问、中文作答时才好做**逐字校验**；
  超出预算的截断要显式记账（`truncated`），不能悄悄丢。
- **生成**：严格提示词（只用来源、必须标来源、不够就说不够）。`allow_general_knowledge`
  关闭时禁止拿通用知识补，打开时必须**标注**「通用知识」。
- **AnswerValidator**：来源编号必须存在、引号里的片段必须**逐字**来自给出的来源
  （归一化后子串匹配）。校验不通过就把 `grounded` 置 False 并列出编造片段 ——
  "来源 100% 可校验"是规划 §19.5 的硬要求，宁可不答，也不能看起来像有依据。

**已知边界**：中文侧是按比例切的近似译文（见 `knowledge_service`），所以校验用的是
**送进上下文的原文**；只要答案引用的是我们给过的文本就能对上，但不要把它当成
"原文里一定逐字如此"的证明。
"""

from __future__ import annotations

import re
from typing import Iterator

from backend.core.logging import get_logger
from backend.core.utils import truncate
from backend.repositories.vector.lancedb_store import vector_store
from backend.services.knowledge import retrieval
from backend.services.knowledge.knowledge_service import _decorate_items
from backend.services.model.model_manager import model_manager

log = get_logger(__name__)

MAX_SOURCES = 8
CONTEXT_MAX_CHARS = 7000
# ⚠️ 不要在这里写死 max_tokens：思考模式下思维链会先把预算吃光，正文返回空串
# （项目在翻译那轮已经踩过，见 docs/进度.md §4.2）。默认让 provider 用它自己配置的
# max_tokens，只有调用方显式传 max_tokens 时才覆盖。

CITE_RE = re.compile(r"\[S(\d+)\]")
# 引号片段（中文引号 / 英文双引号 / 反引号）；太短的（<4 字）不当引用处理
_QUOTE_RE = re.compile(r"「([^」]{4,})」|“([^”]{4,})”|\"([^\"]{4,})\"|`([^`]{4,})`")
_NORM_RE = re.compile(r"[\s*_`~$\\{}（）()\[\]【】]+")
# 含这些记号 → 引的是公式。模型会**重排 LaTeX**（`E_{y,Q}[Q(x)]` 写成 `Ey, QQ(x)`），
# 逐字校验必然假阳性（实测：一个诚实的"库里没有"回答被标成 5 处编造），
# 所以含公式的引用单独记账、不参与 grounded 判定。非公式引用一律照旧逐字校验。
_MATH_HINT = re.compile(r"\$|\\[a-zA-Z]+|\^|_|\{|\}")
# 短于这个长度的引号按"词"处理，不做逐字校验：实测 `“校准数据”`/`“量子计算”` 这类
# **提问里的说法**被模型拿来复述，若当成引用校验就会被误判成编造
MIN_QUOTE_CHARS = 8
# 引号内容与来源的**字符重合度**达到这个比例就算"有依据"（不是逐字，但确实来自来源）。
# 实测：模型会用「」标**概念名**甚至把中英术语拼在一起，例如
# 「data-oblivious/数据无关、在线适用」—— 逐字比对会把它判成编造，而它其实完全来自来源。
# 真正编造的整句重合度通常远低于此。
QUOTE_OVERLAP_MIN = 0.7

SYSTEM = (
    "你是论文阅读助手。你只能依据用户提供的【来源】回答，不要使用来源之外的任何知识，"
    "不要凭印象补充细节。"
)

_RULES = """回答要求（必须遵守）：
1. **只依据下面的来源**作答。每个事实后面立刻标出来源编号，例如 [S1]；可以用多个，例如 [S1][S3]。
2. 直接引用原文时用「」包住，并且必须**逐字**来自来源，不要改写引号里的内容。
3. 如果来源不足以回答，**只输出**一段以「【无法回答】」开头的说明（缺什么、为什么答不了），
   **不要**再给出答案；能回答时**不要**使用这个标记。**任何情况下都不要编造**。
4. 用中文回答：先给结论（2~3 句），再列依据（每条带来源编号）。公式保留 LaTeX 原样。
5. 不要输出"根据我的知识"这类表述。"""

_GENERAL_RULE = (
    "\n6. 已允许补充通用知识：凡是不来自来源的内容，必须显式写成「通用知识：…」，"
    "并单独放在最后一段。"
)


# --------------------------------------------------------------------------
# ContextBuilder
# --------------------------------------------------------------------------


def _pair_texts(item: dict) -> tuple[str, str]:
    """取同一 chunk 的原文与译文（双语语料下两行都在库里）。"""
    text = str(item.get("content") or "").strip()
    lang = item.get("lang") or "en"
    en, zh = (text, "") if lang == "en" else ("", text)
    pair_id = (item.get("metadata") or {}).get("pair_id")
    if pair_id:
        other = "zh" if lang == "en" else "en"
        try:
            rows = vector_store.get_rows([f"{pair_id}:{other}"])
        except Exception:  # noqa: BLE001 —— 取不到另一语言不影响回答
            rows = []
        if rows:
            value = str(rows[0].get("content") or "").strip()
            if other == "en":
                en = value
            else:
                zh = value
    return en, zh


def build_context(items: list[dict], *, max_sources: int = MAX_SOURCES,
                  max_chars: int = CONTEXT_MAX_CHARS) -> dict:
    """编号来源 + 拼成给模型的上下文。返回 ``{sources, text, truncated}``。"""
    sources: list[dict] = []
    blocks: list[str] = []
    used = 0
    truncated = 0
    for item in items:
        if len(sources) >= max_sources:
            truncated += 1
            continue
        en, zh = _pair_texts(item)
        if not en and not zh:
            continue
        index = len(sources) + 1
        head = (f"[S{index}] 论文《{item.get('paper_title') or item.get('paper_id')}》"
                f" 第 {item.get('page') or '?'} 页"
                f" · 类型 {item.get('chunk_type')}"
                f"{' · 小节 ' + str(item['section']) if item.get('section') else ''}")
        body = []
        if zh:
            body.append(f"译文：{zh}")
        if en:
            body.append(f"原文：{en}")
        block = head + "\n" + "\n".join(body)
        if used + len(block) > max_chars and sources:
            truncated += 1
            continue
        used += len(block)
        blocks.append(block)
        # 片段用**这一条自己的语言**：双语来源都会进上下文，但展示的缩略要跟语言徽标一致
        # （真实跑出来过：S4 标着「原」，片段却是中文 —— 因为无条件优先取了译文）
        own = zh if (item.get("lang") or "en") == "zh" else en
        sources.append({
            "index": index, "label": f"S{index}",
            "chunk_id": item.get("chunk_id"), "paper_id": item.get("paper_id"),
            "paper_title": item.get("paper_title"),
            "block_id": item.get("block_id"), "page": item.get("page"),
            "section": item.get("section"), "chunk_type": item.get("chunk_type"),
            "lang": item.get("lang") or "en",
            "snippet": truncate(own or zh or en, 200),
            "text": "\n".join([t for t in (zh, en) if t]),
            "score": (item.get("scores") or {}).get("rerank"),
        })
    return {"sources": sources, "text": "\n\n".join(blocks),
            "truncated": truncated, "chars": used}


def build_prompt(question: str, context: dict, *, allow_general: bool = False) -> str:
    rules = _RULES + (_GENERAL_RULE if allow_general else "")
    return (f"【来源】\n{context['text']}\n\n"
            f"【问题】{question}\n\n{rules}")


# --------------------------------------------------------------------------
# AnswerValidator（规划 §19.5：来源 100% 可校验）
# --------------------------------------------------------------------------


def _norm(text: str) -> str:
    return _NORM_RE.sub("", (text or "")).lower()


def _overlap_ratio(needle: str, blob: str) -> float:
    """``needle`` 有多少字符以**公共子序列**的形式出现在 ``blob`` 里（0~1）。"""
    from difflib import SequenceMatcher

    if not needle or not blob:
        return 0.0
    matcher = SequenceMatcher(None, needle, blob, autojunk=False)
    matched = sum(i2 - i1 for tag, i1, i2, _, _ in matcher.get_opcodes() if tag == "equal")
    return matched / len(needle)


def validate(answer: str, sources: list[dict], question: str | None = None) -> dict:
    """校验引用：编号必须存在；引号里的片段必须逐字来自来源。

    三类引号**不参与**逐字判定（都会单独记账，不是悄悄放过）：
    · **含公式**的：模型会重排 LaTeX，逐字比对必然误报；
    · **过短**的（< ``MIN_QUOTE_CHARS``）：处理的是"词"，不是可校验的断言；
    · **复述提问**的：助手常写"来源没有出现'校准数据'一词"，这是提问里的说法，不是引用。
    """
    text = answer or ""
    cited = sorted({int(m) for m in CITE_RE.findall(text)})
    valid = {int(s["index"]) for s in sources}
    unknown = [c for c in cited if c not in valid]

    quotes: list[str] = []
    for match in _QUOTE_RE.finditer(text):
        piece = next((g for g in match.groups() if g), "")
        if piece:
            quotes.append(piece)
    unsupported: list[dict] = []
    math_unchecked: list[str] = []
    short_ignored: list[str] = []
    echoed: list[str] = []
    fuzzy: list[str] = []
    q_norm = _norm(question or "")
    norm_sources = [(int(s["index"]), _norm(s.get("text") or "")) for s in sources]
    for quote in quotes:
        needle = _norm(quote)
        if not needle:
            continue
        if len(needle) < MIN_QUOTE_CHARS and not _MATH_HINT.search(quote):
            short_ignored.append(truncate(quote, 60))
            continue
        if q_norm and needle in q_norm:
            echoed.append(truncate(quote, 60))
            continue
        if any(needle in blob for _, blob in norm_sources):
            continue
        best = max((_overlap_ratio(needle, blob) for _, blob in norm_sources), default=0.0)
        if best >= QUOTE_OVERLAP_MIN:
            fuzzy.append(truncate(quote, 60))
            continue
        if _MATH_HINT.search(quote):
            math_unchecked.append(truncate(quote, 120))
        else:
            unsupported.append({"quote": truncate(quote, 120), "matched": None,
                                "overlap": round(best, 3)})

    problems: list[str] = []
    if not cited:
        problems.append("答案没有标注任何来源编号")
    if unknown:
        problems.append(f"引用了不存在的来源：{[f'S{i}' for i in unknown]}")
    if unsupported:
        problems.append(f"{len(unsupported)} 处引号内容在来源里找不到（疑似编造）")

    notes: list[str] = []
    if math_unchecked:
        notes.append(f"{len(math_unchecked)} 处含公式的引用未做逐字校验"
                     f"（模型会重排 LaTeX，逐字比对必然误报）")
    if short_ignored:
        notes.append(f"{len(short_ignored)} 处短引号（词级）未做逐字校验")
    if echoed:
        notes.append(f"{len(echoed)} 处引号是在复述提问里的说法")
    if fuzzy:
        notes.append(f"{len(fuzzy)} 处引号与来源只是高重合（≥"
                     f"{int(QUOTE_OVERLAP_MIN * 100)}%），不是逐字一致")

    return {
        "grounded": not problems,
        "cited": [f"S{i}" for i in cited],
        "unknown_citations": [f"S{i}" for i in unknown],
        "quotes": len(quotes),
        "unsupported_quotes": unsupported,
        "math_quotes_unchecked": math_unchecked,
        "short_quotes_ignored": short_ignored,
        "echoed_quotes": echoed,
        "fuzzy_quotes": fuzzy,
        "problems": problems,
        "notes": notes,
    }


# --------------------------------------------------------------------------
# 编排
# --------------------------------------------------------------------------


def _scope_of(payload: dict) -> tuple[str, str | None]:
    raw = str(payload.get("scope") or "knowledge_base")
    paper_id = payload.get("paper_id") or None
    if raw in ("current_paper", "paper"):
        return "paper", paper_id
    if raw in ("cross",):
        return "cross", paper_id
    return "all", None


def _refusal(message: str, *, retrieval_meta: dict | None = None) -> dict:
    return {"implemented": True, "answer": message, "sources": [], "grounded": False,
            "insufficient": True,
            "validation": {"grounded": False, "cited": [], "unknown_citations": [],
                           "quotes": 0, "unsupported_quotes": [],
                           "problems": ["没有可用的来源"]},
            "retrieval_metadata": retrieval_meta or {}, "usage": {}}


#: 模型"答不了"时的机器可读标记（写在提示词第 3 条里）
INSUFFICIENT_MARK = "【无法回答】"


def is_insufficient(answer: str) -> bool:
    return (answer or "").lstrip().startswith(INSUFFICIENT_MARK)


def retrieve(payload: dict) -> dict:
    """检索 + 组装上下文（非流式与流式共用这一步）。"""
    question = str(payload.get("question") or payload.get("query") or "").strip()
    if not question:
        from backend.core.errors import ValidationError

        raise ValidationError("缺少 question")
    scope, paper_id = _scope_of(payload)
    if scope == "paper" and not paper_id:
        from backend.core.errors import ValidationError

        raise ValidationError("scope=current_paper 时必须给 paper_id")
    top_k = payload.get("top_k")
    result = retrieval.search(question, mode="qa", scope=scope, paper_id=paper_id,
                              top_k=int(top_k) if top_k else None)
    items = _decorate_items(result["items"])
    context = build_context(items)
    allow_general = bool(retrieval._setting("allow_general_knowledge", False))
    return {"question": question, "scope": scope, "paper_id": paper_id,
            "items": items, "context": context, "allow_general": allow_general,
            "retrieval": result["retrieval"]}


def _meta_payload(prep: dict) -> dict:
    return {"question": prep["question"], "scope": prep["scope"],
            "sources": [_public(s) for s in prep["context"]["sources"]],
            "retrieval": prep["retrieval"],
            "allow_general_knowledge": prep["allow_general"]}


def _public(source: dict) -> dict:
    """来源下发前裁掉内部字段（`text` 供校验、`content`/`metadata` 供上下文）。"""
    drop = ("text", "content", "metadata")
    return {k: v for k, v in source.items() if k not in drop}


def answer(payload: dict) -> dict:
    """非流式问答：检索 → 生成 → 校验。"""
    prep = retrieve(payload)
    sources = prep["context"]["sources"]
    if not sources:
        return _refusal("库内文献不足以回答这个问题（没有检索到可用来源）。先确认论文已解析、"
                        "已翻译并建过索引。", retrieval_meta=prep["retrieval"])

    prompt = build_prompt(prep["question"], prep["context"], allow_general=prep["allow_general"])
    provider = model_manager.get_generation()
    kwargs = {"max_tokens": int(payload["max_tokens"])} if payload.get("max_tokens") else {}
    result = provider.generate(prompt, system=SYSTEM, **kwargs)
    validation = validate(result.text, sources, question=prep["question"])
    return {
        "implemented": True,
        "answer": result.text,
        "sources": [_public(s) for s in sources],
        "grounded": validation["grounded"],
        "insufficient": is_insufficient(result.text),
        "validation": validation,
        "retrieval_metadata": prep["retrieval"],
        "context": {"sources": len(sources), "chars": prep["context"]["chars"],
                    "truncated": prep["context"]["truncated"]},
        "usage": {"model": result.model or provider.model_name,
                  "prompt_tokens": result.prompt_tokens,
                  "completion_tokens": result.completion_tokens,
                  "reasoning_tokens": result.reasoning_tokens,
                  "latency_ms": result.latency_ms},
    }


def _sse(event: str, data: dict) -> str:
    from backend.core.utils import json_dumps

    return f"event: {event}\ndata: {json_dumps(data)}\n\n"


def stream_answer(payload: dict) -> Iterator[str]:
    """SSE 流：``rag_meta`` → 若干 ``rag_token`` → ``rag_done``（同步生成器，
    由 Starlette 放到线程池里迭代，正好适配同步 SDK 的流式接口）。"""
    try:
        prep = retrieve(payload)
    except Exception as exc:  # noqa: BLE001
        yield _sse("rag_error", {"message": str(exc)[:300]})
        return
    yield _sse("rag_meta", _meta_payload(prep))

    sources = prep["context"]["sources"]
    if not sources:
        done = _refusal("库内文献不足以回答这个问题（没有检索到可用来源）。")
        yield _sse("rag_done", {**done, "retrieval_metadata": prep["retrieval"]})
        return

    prompt = build_prompt(prep["question"], prep["context"], allow_general=prep["allow_general"])
    provider = model_manager.get_generation()
    chunks: list[str] = []
    try:
        kwargs = {"max_tokens": int(payload["max_tokens"])} if payload.get("max_tokens") else {}
        for piece in provider.stream(prompt, system=SYSTEM, **kwargs):
            chunks.append(piece)
            yield _sse("rag_token", {"text": piece})
    except Exception as exc:  # noqa: BLE001
        log.warning("RAG 流式生成失败：%s", exc)
        yield _sse("rag_error", {"message": f"{type(exc).__name__}: {str(exc)[:200]}",
                                 "partial": "".join(chunks)})
        return

    text = "".join(chunks)
    validation = validate(text, sources, question=prep["question"])
    yield _sse("rag_done", {
        "implemented": True, "answer": text,
        "sources": [_public(s) for s in sources],
        "grounded": validation["grounded"], "validation": validation,
        "insufficient": is_insufficient(text),
        "retrieval_metadata": prep["retrieval"],
        "usage": {"model": provider.model_name, "streamed": True},
    })
