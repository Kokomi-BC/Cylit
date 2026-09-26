"""RAG 问答测试（ContextBuilder / AnswerValidator / 非流式与流式编排）。

**全部离线**：模型与向量库都用替身。真实端到端（真向量 + 真重排 + 真生成）见 docs/dev-log.md §17.36。
"""

from __future__ import annotations

import asyncio
import json

import pytest

from backend.core.errors import ValidationError
from backend.providers.base import GenerationResult
from backend.services.knowledge import rag_service
from backend.services.knowledge.rag_service import (
    build_context, build_prompt, stream_answer, validate,
)

EN = "Vector quantization compresses high-dimensional vectors and minimizes distortion."
ZH = "向量量化压缩高维向量，并最小化失真。"
QUOTE = "向量量化压缩高维向量"


def _item(index: int = 1, *, lang: str = "zh", pair: bool = True,
          content: str | None = None) -> dict:
    return {
        "chunk_id": f"c{index}:{lang}", "paper_id": "p1", "paper_title": "某论文",
        "block_id": f"b{index}", "page": index, "section": "2 方法",
        "chunk_type": "text", "lang": lang,
        "content": content or (ZH if lang == "zh" else EN),
        "snippet": (content or ZH)[:60],
        "scores": {"rerank": 0.9}, "ranks": {},
        "metadata": {"pair_id": f"c{index}"} if pair else {},
    }


class _FakeGen:
    model_name = "fake-gen"

    def __init__(self, text: str = "", *, pieces: list[str] | None = None,
                 boom: bool = False) -> None:
        self.pieces = pieces or []
        self.text = text or "".join(self.pieces)
        self.boom = boom
        self.prompts: list[str] = []

    def generate(self, prompt, *, system=None, max_tokens=None, **kw):
        self.prompts.append(prompt)
        if self.boom:
            raise RuntimeError("模型 500")
        return GenerationResult(text=self.text, model=self.model_name,
                                prompt_tokens=123, completion_tokens=45,
                                reasoning_tokens=7, latency_ms=88.0)

    def stream(self, prompt, *, system=None, max_tokens=None, **kw):
        self.prompts.append(prompt)
        if self.boom:
            raise RuntimeError("流断了")
        yield from self.pieces


@pytest.fixture()
def twin(monkeypatch):
    """让 `_pair_texts` 能取到另一语言（默认给英文原文）。"""
    def fake_get_rows(ids):
        lang = "en" if ids[0].endswith(":en") else "zh"
        return [{"chunk_id": ids[0], "lang": lang, "content": EN if lang == "en" else ZH,
                 "paper_id": "p1", "chunk_type": "text", "page_no": 1,
                 "block_id": "b1", "section": "2 方法", "metadata": {}}]

    monkeypatch.setattr(rag_service.vector_store, "get_rows", fake_get_rows)


# --------------------------------------------------------------------------
# ContextBuilder
# --------------------------------------------------------------------------


def test_build_context_numbers_sources_and_keeps_both_languages(twin) -> None:
    ctx = build_context([_item(1), _item(2, lang="en")])

    assert [s["label"] for s in ctx["sources"]] == ["S1", "S2"]
    assert ctx["sources"][0]["index"] == 1
    assert ctx["sources"][0]["text"].count(ZH) == 1 and EN in ctx["sources"][0]["text"]
    assert ctx["sources"][1]["text"].count(EN) == 1 and ZH in ctx["sources"][1]["text"]
    assert "[S1] 论文《某论文》" in ctx["text"] and "[S2]" in ctx["text"]
    assert "译文：" in ctx["text"] and "原文：" in ctx["text"]
    assert ctx["truncated"] == 0 and ctx["chars"] > 0


def test_build_context_renders_any_formula_without_language_pairs(twin) -> None:
    """`lang=any`（公式原子行）语言无关：不贴"原文/译文"标签，中英文上下文都能直接用。

    为什么不走 `twin` 的取对侧逻辑：公式没有"对侧语种"，LaTeX 本身就是内容。
    """
    rows = [{"chunk_id": "p1:formula:b7:any", "lang": "any",
             "content": "公式 b7：D_{mse} = E[||x - Q^{-1}(Q(x))||^2]",
             "paper_id": "p1", "chunk_type": "formula", "page_no": 2,
             "block_id": "b7", "section": "2 方法", "metadata": {}}]
    ctx = build_context(rows)
    assert ctx["text"].count("内容：") == 1
    assert "原文：" not in ctx["text"] and "译文：" not in ctx["text"]
    assert "D_{mse}" in ctx["text"]
    assert ctx["sources"][0]["lang"] == "any"
    assert "D_{mse}" in ctx["sources"][0]["snippet"]


def test_build_context_respects_source_cap_and_budget(monkeypatch) -> None:
    no_twin = lambda ids: []                                    # noqa: E731
    monkeypatch.setattr(rag_service.vector_store, "get_rows", no_twin)

    capped = build_context([_item(i) for i in range(1, 6)], max_sources=2)
    assert [s["label"] for s in capped["sources"]] == ["S1", "S2"]
    assert capped["truncated"] == 3

    tight = build_context([_item(i, content="x" * 400) for i in range(1, 5)],
                          max_chars=900)
    assert len(tight["sources"]) < 4 and tight["truncated"] >= 1

    assert build_context([])["sources"] == [] and build_context([])["text"] == ""


def test_build_context_snippet_follows_item_language(twin) -> None:
    """双语来源：片段必须跟 `lang` 一致（英文条目就给英文片段），否则徽标与内容对不上。"""
    ctx = build_context([_item(1, lang="en")])

    src = ctx["sources"][0]
    assert src["lang"] == "en"
    assert src["snippet"].startswith(EN[:20]) and ZH not in src["snippet"]
    assert ZH in src["text"]                              # 校验用文本仍含双语


def test_build_prompt_rules_and_general_knowledge_switch(twin) -> None:
    ctx = build_context([_item()])
    plain = build_prompt("什么是向量量化？", ctx)
    assert "【来源】" in plain and "【问题】什么是向量量化？" in plain
    assert "[S1]" in plain and "不要编造" in plain
    assert "通用知识" not in plain

    general = build_prompt("什么是向量量化？", ctx, allow_general=True)
    assert "已允许补充通用知识" in general


# --------------------------------------------------------------------------
# AnswerValidator
# --------------------------------------------------------------------------


def _sources() -> list[dict]:
    return [{"index": 1, "label": "S1", "paper_id": "p1", "block_id": "b1",
             "snippet": ZH, "text": f"{ZH}\n{EN}"}]


def test_validate_accepts_verbatim_quotes(twin) -> None:
    answer = f"结论：向量量化压缩高维向量 [S1]。依据：「{QUOTE}」[S1]。"
    result = validate(answer, _sources())
    assert result["grounded"] is True
    assert result["cited"] == ["S1"] and result["quotes"] == 1
    assert result["problems"] == []


def test_validate_flags_missing_citations_unknown_and_fabrication() -> None:
    no_cite = validate("向量量化压缩高维向量。", _sources())
    assert no_cite["grounded"] is False
    assert any("没有标注任何来源" in p for p in no_cite["problems"])

    unknown = validate("结论 [S9]。", _sources())
    assert unknown["unknown_citations"] == ["S9"] and unknown["grounded"] is False

    fake = validate("结论：「这段根本没在来源里出现过」[S1]", _sources())
    assert fake["grounded"] is False
    assert fake["unsupported_quotes"] and fake["unsupported_quotes"][0]["matched"] is None
    assert any("编造" in p for p in fake["problems"])


def test_validate_normalizes_whitespace_and_markdown(twin) -> None:
    answer = "依据：「向量 量化 **压缩** 高维向量」[S1]"
    assert validate(answer, _sources())["grounded"] is True


def test_validate_does_not_flag_rerendered_latex_as_fabrication() -> None:
    """含公式的引用不做逐字校验：模型把 `\\mathbb{E}_Q[\\|x-Q^{-1}(Q(x))\\|]` 重排成
    `E_Q[|x-Q^{-1}(Q(x))|]` 是常态，逐字比对会把**诚实回答**误判成编造
    （真实跑出来过：一个"库里没有"的回答被标 5 处编造）。"""
    source = ("公式 p3-b02：(MSE) $D_{\\text{mse}} := \\mathbb{E}_Q[\\|x - Q^{-1}(Q(x))\\|_2^2]$")
    sources = [{"index": 1, "label": "S1", "text": source}]
    answer = "来源只给了公式：「公式 p3-b02：MSE D_mse := E_Q[|x - Q^{-1}(Q(x))|_2^2]」[S1]"

    result = validate(answer, sources)
    assert result["grounded"] is True
    assert result["unsupported_quotes"] == []
    # 要么按"高重合"放过，要么按"含公式不逐字校验"记账 —— 两条都不算编造
    assert result["math_quotes_unchecked"] or result["fuzzy_quotes"]
    assert result["notes"]


def test_validate_records_unmatchable_math_quote_separately() -> None:
    """连高重合都够不着的公式引用 → 单独记账（不判编造，但也不假装校验过）。"""
    sources = [{"index": 1, "label": "S1", "text": "公式 p1：$a + b = c$"}]
    answer = "来源说：「这里给出了一个完全不同的推导过程与结论 $x^2 + y^2 = z^2$」[S1]"

    result = validate(answer, sources)
    assert result["grounded"] is True                     # 公式引用不算编造的硬证据
    assert result["unsupported_quotes"] == []
    assert result["math_quotes_unchecked"] and any("含公式" in n for n in result["notes"])


def test_validate_still_flags_non_math_fabrication() -> None:
    sources = [{"index": 1, "label": "S1", "text": "本文提出一种在线量化方法。"}]
    result = validate("结论：「该论文在量子计算上做了大量实验」[S1]", sources)
    assert result["grounded"] is False
    assert result["math_quotes_unchecked"] == []
    assert len(result["unsupported_quotes"]) == 1


def test_validate_tolerates_label_like_quotes_with_high_overlap() -> None:
    """模型会用「」标概念名（甚至把中英术语拼一起），高重合就算有依据。"""
    sources = [{"index": 1, "label": "S1",
                "text": "Our data-oblivious algorithms are suitable for online "
                        "applications（数据无关、在线应用）。"}]

    result = validate("它属于「data-oblivious/数据无关、在线应用」一类 [S1]", sources)

    assert result["grounded"] is True
    assert result["fuzzy_quotes"] and any("高重合" in n for n in result["notes"])


def test_validate_still_flags_low_overlap_fabrication() -> None:
    sources = [{"index": 1, "label": "S1", "text": "本文提出一种数据无关的在线量化方法。"}]
    result = validate("论文指出：「实验在 8 张 A100 上跑了 3 天，明显优于所有基线」[S1]", sources)
    assert result["grounded"] is False
    assert result["unsupported_quotes"][0]["overlap"] < 0.7


def test_validate_ignores_short_terms_and_question_echoes() -> None:
    """`“校准数据”` 这种**词级**引号、以及复述提问的引号，都不是可校验的断言。"""
    sources = [{"index": 1, "label": "S1", "text": "本文提出一种数据无关的在线量化方法。"}]

    short = validate("来源没有出现“校准数据”一词 [S1]", sources,
                     question="需要校准数据吗？")
    assert short["grounded"] is True
    assert short["short_quotes_ignored"] == ["校准数据"]
    assert any("词级" in n for n in short["notes"])

    echo = validate("来源未直接使用「数据校准流程具体是怎么实现的」这个说法 [S1]", sources,
                    question="它的数据校准流程具体是怎么实现的？")
    assert echo["grounded"] is True
    assert echo["echoed_quotes"] == ["数据校准流程具体是怎么实现的"]
    assert any("复述提问" in n for n in echo["notes"])


# --------------------------------------------------------------------------
# 非流式
# --------------------------------------------------------------------------


def _prep(items: list[dict], monkeypatch, **ctx_kw) -> dict:
    ctx = build_context(items, **ctx_kw)
    return {"question": "向量量化是什么？", "scope": "all", "paper_id": None,
            "items": items, "context": ctx, "allow_general": False,
            "retrieval": {"vector_hits": len(items), "lexical_hits": 1, "fused": 1,
                          "returned": len(items), "notes": []}}


def test_answer_returns_grounded_answer(monkeypatch, twin) -> None:
    monkeypatch.setattr(rag_service, "retrieve", lambda payload: _prep([_item()], monkeypatch))
    fake = _FakeGen(f"结论：向量量化压缩高维向量 [S1]。\n依据：「{QUOTE}」[S1]。")
    monkeypatch.setattr(rag_service.model_manager, "get_generation", lambda model_id=None: fake)

    out = rag_service.answer({"question": "向量量化是什么？", "scope": "knowledge_base"})

    assert out["implemented"] is True and out["grounded"] is True
    assert out["validation"]["cited"] == ["S1"]
    assert out["sources"][0]["label"] == "S1" and "text" not in out["sources"][0]
    assert out["usage"]["completion_tokens"] == 45
    assert out["context"]["sources"] == 1
    assert "【来源】" in fake.prompts[0] and "[S1]" in fake.prompts[0]


def test_answer_marks_insufficient_by_marker(monkeypatch, twin) -> None:
    """模型说"答不了"时用「【无法回答】」开头，后端据此给出机器可读标记。"""
    monkeypatch.setattr(rag_service, "retrieve", lambda payload: _prep([_item()], monkeypatch))
    fake = _FakeGen("【无法回答】来源里没有量子计算的内容 [S1]。")
    monkeypatch.setattr(rag_service.model_manager, "get_generation", lambda model_id=None: fake)

    out = rag_service.answer({"question": "量子计算？"})
    assert out["insufficient"] is True and out["grounded"] is True   # 引用仍可校验

    fake_ok = _FakeGen(f"结论：向量量化压缩高维向量 [S1]。")
    monkeypatch.setattr(rag_service.model_manager, "get_generation", lambda model_id=None: fake_ok)
    assert rag_service.answer({"question": "向量量化？"})["insufficient"] is False


def test_answer_refuses_without_sources_and_skips_model(monkeypatch) -> None:
    monkeypatch.setattr(rag_service, "retrieve", lambda payload: _prep([], monkeypatch))

    def boom(model_id=None):
        raise AssertionError("没有来源时不该调用生成模型")

    monkeypatch.setattr(rag_service.model_manager, "get_generation", boom)
    out = rag_service.answer({"question": "无关问题"})

    assert out["grounded"] is False and out["sources"] == []
    assert "库内文献不足以回答" in out["answer"]


def test_decorate_keeps_content_for_context_and_public_strips_it(monkeypatch) -> None:
    """回归：装饰检索结果时**不能把 content 丢掉** —— 丢了上下文就一条来源都拼不出来
    （真实踩过：RAG 问答一律回"没有检索到可用来源"）。"""
    from backend.services.knowledge import knowledge_service as ks

    monkeypatch.setattr(ks, "_paper_titles", lambda ids: {"p1": "某论文"})
    raw = [{"chunk_id": "c1", "paper_id": "p1", "page_no": 2, "block_id": "b1",
            "content": ZH * 20, "metadata": {"pair_id": "c1"}, "lang": "zh",
            "vector_score": 0.5, "rrf_rank": 1}]

    decorated = ks._decorate_items(raw)
    assert decorated[0]["content"] == raw[0]["content"]
    assert decorated[0]["metadata"] == {"pair_id": "c1"}
    assert decorated[0]["paper_title"] == "某论文"

    public = ks.public_item(decorated[0])
    assert "content" not in public and "metadata" not in public
    assert public["snippet"] and public["scores"]["vector"] == 0.5


def test_retrieve_maps_scope_and_validates(monkeypatch) -> None:
    seen: dict = {}

    def fake_search(question, **kw):
        seen.update(kw, question=question)
        return {"items": [], "retrieval": {}}

    monkeypatch.setattr(rag_service.retrieval, "search", fake_search)
    monkeypatch.setattr(rag_service, "_decorate_items", lambda items: items)
    # 设置读取走真实解析入口（`setting` → `_setting_raw`），把唯一的数据入口替成空表
    monkeypatch.setattr(rag_service.retrieval, "_setting_raw",
                        lambda key: rag_service.retrieval._MISSING)

    prep = rag_service.retrieve({"question": "q", "scope": "current_paper", "paper_id": "p9"})
    assert seen["scope"] == "paper" and seen["paper_id"] == "p9" and seen["mode"] == "qa"
    assert prep["allow_general"] is False

    prep = rag_service.retrieve({"question": "q", "scope": "knowledge_base"})
    assert seen["scope"] == "all" and seen["paper_id"] is None

    with pytest.raises(ValidationError):
        rag_service.retrieve({"question": "q", "scope": "current_paper"})
    with pytest.raises(ValidationError):
        rag_service.retrieve({"scope": "all"})


# --------------------------------------------------------------------------
# 流式
# --------------------------------------------------------------------------


def _frames(frames: list[str]) -> list[tuple[str, dict]]:
    out = []
    for frame in frames:
        head, _, body = frame.partition("\ndata: ")
        out.append((head.replace("event: ", "").strip(), json.loads(body.strip())))
    return out


def test_stream_answer_emits_meta_tokens_and_done(monkeypatch, twin) -> None:
    monkeypatch.setattr(rag_service, "retrieve", lambda payload: _prep([_item()], monkeypatch))
    fake = _FakeGen(pieces=["结论：", "向量量化压缩高维向量 ", "[S1]。"])
    monkeypatch.setattr(rag_service.model_manager, "get_generation", lambda model_id=None: fake)

    frames = _frames(list(stream_answer({"question": "向量量化是什么？"})))

    assert [f[0] for f in frames] == ["rag_meta", "rag_token", "rag_token", "rag_token", "rag_done"]
    assert frames[0][1]["sources"][0]["label"] == "S1"
    assert "text" not in frames[0][1]["sources"][0]           # 内部校验文本不下发
    assert "".join(f[1]["text"] for f in frames if f[0] == "rag_token") == fake.text
    assert frames[-1][1]["answer"] == fake.text
    assert frames[-1][1]["grounded"] is True


def test_stream_answer_reports_errors_and_empty_sources(monkeypatch) -> None:
    monkeypatch.setattr(rag_service, "retrieve", lambda payload: _prep([], monkeypatch))
    frames = _frames(list(stream_answer({"question": "无关"})))
    assert [f[0] for f in frames] == ["rag_meta", "rag_done"]
    assert frames[-1][1]["grounded"] is False

    monkeypatch.setattr(rag_service, "retrieve", lambda payload: _prep([_item()], monkeypatch))
    monkeypatch.setattr(rag_service.model_manager, "get_generation",
                        lambda model_id=None: _FakeGen(boom=True))
    frames = _frames(list(stream_answer({"question": "q"})))
    assert [f[0] for f in frames] == ["rag_meta", "rag_error"]
    assert "流断了" in frames[-1][1]["message"]


# --------------------------------------------------------------------------
# API
# --------------------------------------------------------------------------


def test_rag_query_endpoints_contract(monkeypatch) -> None:
    from backend.api import knowledge as api_knowledge

    monkeypatch.setattr(api_knowledge.__dict__.get("rag_service", None) or rag_service,
                        "answer", lambda payload: {"implemented": True,
                                                   "answer": payload.get("question"),
                                                   "sources": [], "grounded": False})
    res = asyncio.run(api_knowledge.rag_query(None, {"question": "q"}))
    assert res["data"]["implemented"] is True and res["error"] is None

    resp = api_knowledge.rag_query_stream({"question": "q"})
    assert resp.media_type == "text/event-stream"
