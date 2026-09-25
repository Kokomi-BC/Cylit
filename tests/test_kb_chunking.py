"""知识库 chunk 化规则（**离线**：伪造 MySQL 行 / IR / md / 译文，不碰真实库）。

覆盖本轮改造的硬约束：
- 公式整体进正文语义 chunk，且「定义 + 公式 + where 解释」同块；
- 公式原子 chunk 只出 1 行（`lang=any`），并与语义 chunk 双向指认；
- 前沿元数据过滤、图注去重、表格数字行不当标题；
- 硬上限（HARD）与句子边界二次切分；
- 小正文 chunk 向相邻同小节正文块合并（标题 / 公式 / 图 / 表不参与）；
- section / section_path 从最近的 heading 继承。
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from backend.services.knowledge import knowledge_service as ks

PAPER = "demo-paper"
TARGET, MIN_CHARS, HARD = 400, 200, 640

ALIGNED = ("$$\\begin{aligned} D(x,\\hat{x}) &= \\|x - \\hat{x}\\|^2 \\\\ "
           "&= \\sum_i (x_i - \\hat{x}_i)^2 \\end{aligned}$$")


class FakeIR:
    """IR 只提供公式/图/表三类行（正文块来自伪造的 MySQL 行）。"""

    def __init__(self, path, formulas=(), figures=(), tables=()):
        self._f, self._g, self._t = list(formulas), list(figures), list(tables)

    def formulas(self):
        return self._f

    def figures(self):
        return self._g

    def tables(self):
        return self._t


class FakeSession:
    def __init__(self, blocks, paper):
        self._blocks, self._paper = blocks, paper

    def query(self, *_a, **_k):
        return self

    def filter(self, *_a, **_k):
        return self

    def order_by(self, *_a, **_k):
        return self

    def all(self):
        return list(self._blocks)

    def get(self, model, key):
        from backend.repositories.mysql.models import Paper

        return self._paper if model is Paper else None


class Env:
    def __init__(self) -> None:
        self.blocks: list[SimpleNamespace] = []
        self.md: dict[str, dict] = {}
        self.formulas: list[dict] = []
        self.figures: list[dict] = []
        self.tables: list[dict] = []
        self.pages: list[dict] = []
        self.authors = "Amir Zandieh, Majid Daliri, Majid Hadian"

    def add(self, bid: str, page: int, type_: str, text: str, order: int) -> str:
        self.blocks.append(SimpleNamespace(
            id=bid, paper_id=PAPER, page_no=page, type=type_, order_global=order,
            meta_json="{}", preview=text[:200], content_hash=f"h-{bid}",
            section_id=None))
        self.md[bid] = {"text": text, "order": order}
        return bid

    def formula(self, bid: str, page: int, latex: str, raw: str = "", order: int = 99,
                **extra) -> str:
        self.formulas.append({"block_id": bid, "latex_final": latex, "raw_text": raw or latex,
                              "grade": "A", "image_path": f"/tmp/{bid}.png",
                              "latex_candidate": [], **extra})
        return bid

    def figure(self, fid: str, page: int, caption: str, caption_block: str) -> str:
        self.figures.append({"block_id": fid, "page_number": page, "caption": caption,
                             "caption_block_id": caption_block, "image_path": f"/tmp/{fid}.png"})
        return fid


@pytest.fixture()
def env(monkeypatch):
    e = Env()
    monkeypatch.setattr(ks, "chunk_params", lambda: (TARGET, MIN_CHARS, HARD))
    monkeypatch.setattr(ks, "session_scope",
                        lambda: _Ctx(FakeSession(e.blocks, SimpleNamespace(
                            id=PAPER, authors=e.authors))))
    monkeypatch.setattr(ks.paper_service, "paper_paths",
                        lambda pid: {"ir": "/tmp/none.sqlite"})
    monkeypatch.setattr(ks.reader_service, "load_markdown_map", lambda pid: e.md)
    monkeypatch.setattr(ks.reader_service, "page_translations", lambda pid: e.pages)
    import backend.parsing.ir.ir_reader as ir_reader

    monkeypatch.setattr(ir_reader, "IRReader",
                        lambda path: FakeIR(path, e.formulas, e.figures, e.tables))
    return e


class _Ctx:
    def __init__(self, sess):
        self._sess = sess

    def __enter__(self):
        return self._sess

    def __exit__(self, *a):
        return False


def texts(chunks):
    return [c for c in chunks if c["chunk_type"] == "text"]


# --------------------------------------------------------------------------
# 公式：整体进正文 + 原子 chunk
# --------------------------------------------------------------------------


def _formula_section(e: Env) -> None:
    e.add("b1", 1, "heading", "## 2 Method", 1)
    e.add("b2", 1, "para", "The quantization error is defined as:", 2)
    e.add("b3", 1, "formula", ALIGNED, 3)
    e.formula("b3", 1, ALIGNED.replace("$$", ""), raw=ALIGNED, order=3)
    e.add("b4", 1, "para", "where x represents the input vector and x-hat its reconstruction.", 4)


def test_formula_stays_whole_in_semantic_chunk(env) -> None:
    _formula_section(env)
    chunks = ks._collect_chunks(PAPER)
    body = texts(chunks)
    assert len(body) == 1, "定义 + 公式 + where 必须在同一条正文 chunk 里"
    content = body[0]["content"]
    assert "\\begin{aligned}" in content and "\\end{aligned}" in content
    assert "defined as" in content and "where x represents" in content
    assert body[0]["metadata"]["formula_ids"] == ["b3"]


def test_formula_atomic_chunk_links_back(env) -> None:
    _formula_section(env)
    chunks = ks._collect_chunks(PAPER)
    atom = [c for c in chunks if c["chunk_type"] == "formula"]
    assert len(atom) == 1
    assert atom[0]["id"] == f"{PAPER}:formula:b3"
    assert atom[0]["metadata"]["text_chunk_id"] == texts(chunks)[0]["id"]


def test_noise_formula_neither_chunked_nor_inlined(env) -> None:
    env.add("b1", 1, "heading", "## ABSTRACT", 1)
    env.add("b2", 1, "para", "Vector quantization matters for high-dimensional data.", 2)
    env.add("b3", 1, "formula", "$$ * † $$", 3)
    env.formula("b3", 1, "* †", raw="∗†", order=3)
    chunks = ks._collect_chunks(PAPER)
    assert not [c for c in chunks if c["chunk_type"] == "formula"]
    assert all("* †" not in c["content"] for c in chunks)


def test_formula_only_buffer_makes_no_text_chunk(env, monkeypatch) -> None:
    """整段只有公式时不再造一条和原子 chunk 重复的正文行。"""
    monkeypatch.setattr(ks, "chunk_params", lambda: (200, 200, 640))
    env.add("b1", 1, "heading", "## 3 Proofs", 1)
    env.add("b2", 1, "para", "We now prove the bound for the residual vector. " * 7, 2)
    env.add("b3", 1, "formula", ALIGNED, 3)
    env.formula("b3", 1, ALIGNED.replace("$$", ""), raw=ALIGNED, order=3)
    env.add("b4", 1, "formula", ALIGNED, 4)
    env.formula("b4", 1, ALIGNED.replace("$$", ""), raw=ALIGNED, order=4)
    chunks = ks._collect_chunks(PAPER)
    assert [c["chunk_type"] for c in chunks] == ["text", "formula", "formula"]
    assert "aligned" not in texts(chunks)[0]["content"]


# --------------------------------------------------------------------------
# 前沿元数据 / Markdown 清理 / 图注
# --------------------------------------------------------------------------


def test_front_matter_dropped_abstract_kept(env) -> None:
    env.add("b1", 1, "heading", "## Attention Is All You Need", 1)
    env.add("b2", 1, "para", "Amir Zandieh Majid Daliri Majid Hadian", 2)
    env.add("b3", 1, "para", "Google Research New York University", 3)
    env.add("b4", 1, "para", "zandieh@google.com", 4)
    abstract = ("The dominant sequence transduction models are based on complex recurrent or "
                "convolutional neural networks that include an encoder and a decoder. " * 3)
    env.add("b5", 1, "para", abstract, 5)
    env.add("b6", 1, "heading", "## 1Introduction", 6)
    env.add("b7", 1, "para", "Recurrent neural networks work on sequences.", 7)
    chunks = ks._collect_chunks(PAPER)
    blob = "".join(c["content"] for c in chunks)
    assert "Amir Zandieh" not in blob and "Google Research" not in blob
    assert "zandieh@google.com" not in blob
    assert "dominant sequence transduction" in blob, "摘要不能被误杀"
    assert "Recurrent neural networks" in blob


def test_markdown_cleaned_but_citations_kept(env) -> None:
    env.add("b1", 1, "heading", "## 3 Results", 1)
    env.add("b2", 1, "para",
            "**Figure 1** shows [13](https://arxiv.org/abs/1) and ![f](../figures/a.png) plus "
            "the comparison in [7, 8] for long sequences and memory bounds.", 2)
    chunks = ks._collect_chunks(PAPER)
    body = texts(chunks)[0]["content"]
    assert "**" not in body and "](" not in body
    assert "[13]" in body and "[7, 8]" in body
    assert "Figure 1" in body


def test_attached_caption_not_indexed_twice(env) -> None:
    caption = "**Figure 1: The Transformer - model architecture.**"
    env.add("b1", 3, "heading", "## 3 Model", 1)
    env.add("b2", 3, "figure", caption, 2)
    env.add("b3", 3, "caption", caption, 3)
    env.add("b4", 3, "para", "We stack six identical layers on each side of the model.", 4)
    env.figure("b2", 3, caption.strip("*").strip(), "b3")
    chunks = ks._collect_chunks(PAPER)
    body = "".join(c["content"] for c in texts(chunks))
    assert "model architecture" not in body, "已挂接的图注不该再进正文"
    assert "six identical layers" in body


def test_unattached_caption_stays(env) -> None:
    """算法标题不是图注（没有 figure/table 挂它），必须留在正文里。"""
    env.add("b1", 5, "caption", "**Algorithm 2** The third line follows by Lemma 4.", 1)
    env.add("b2", 5, "para", "We now prove the claim about the residual vector.", 2)
    chunks = ks._collect_chunks(PAPER)
    blob = "".join(c["content"] for c in texts(chunks))
    assert "Algorithm 2" in blob and "residual vector" in blob


def test_table_number_row_is_not_a_heading(env) -> None:
    env.add("b1", 9, "heading", "## 2 Experiments", 1)
    env.add("b2", 9, "para", "Our method achieves better recall on all datasets with the same "
                             "bit-width as the baseline approaches.", 2)
    env.add("b3", 9, "heading", "1645.2945.16", 3)
    env.add("b4", 9, "para", "We also report the variance of the inner-product error across "
                             "different random seeds and projection matrices.", 4)
    chunks = ks._collect_chunks(PAPER)
    assert len(texts(chunks)) == 1, "表格数字行不该 flush 正文 buffer"


# --------------------------------------------------------------------------
# 粒度：硬上限 / 二次切分 / 小 chunk 合并
# --------------------------------------------------------------------------


def test_no_chunk_exceeds_hard_and_long_block_is_split(env) -> None:
    long_para = "".join(f"Sentence number {i} explains the quantization error in detail. "
                        for i in range(40))
    assert len(long_para) > HARD * 2
    env.add("b1", 1, "heading", "## 2 Method", 1)
    env.add("b2", 1, "para", long_para, 2)
    chunks = ks._collect_chunks(PAPER)
    body = texts(chunks)
    assert len(body) > 1
    for c in body:
        assert len(c["content"]) <= HARD, f"{c['id']} 超过硬上限"
    assert "Sentence number 0" in body[0]["content"]


def test_small_chunks_merge_within_section(env, monkeypatch) -> None:
    """小块往前并：flush 出来的两条小块在同小节 → 合成一条。"""
    monkeypatch.setattr(ks, "chunk_params", lambda: (60, 200, 640))
    env.add("b1", 1, "heading", "## 4 Results", 1)
    env.add("b2", 1, "para", "Short first paragraph about recall on LongBench at 2 bits.", 2)
    env.add("b3", 1, "para", "Short second paragraph about the variance of the error.", 3)
    chunks = ks._collect_chunks(PAPER)
    body = texts(chunks)
    assert len(body) == 1, "同小节的两条小块应合并"
    assert body[0]["metadata"]["block_ids"] == ["b1", "b2", "b3"]
    assert body[0]["id"] == f"{PAPER}:text:b1-b3"


def test_merge_respects_hard_cap(env, monkeypatch) -> None:
    """合起来会超过硬上限就不合并（小块保留，不为了长度破上限）。"""
    monkeypatch.setattr(ks, "chunk_params", lambda: (200, 200, 300))
    env.add("b1", 1, "heading", "## 4 Results", 1)
    env.add("b2", 1, "para", "First paragraph about recall on LongBench. " * 6, 2)
    env.add("b3", 1, "para", "Second short paragraph about the variance of the error.", 3)
    chunks = ks._collect_chunks(PAPER)
    body = texts(chunks)
    assert len(body) == 2
    assert all(len(c["content"]) <= 300 for c in body)


def test_small_chunks_do_not_merge_across_sections(env) -> None:
    env.add("b1", 1, "heading", "## 4 Results", 1)
    env.add("b2", 1, "para", "Short paragraph about recall on LongBench at two bits.", 2)
    env.add("b3", 1, "heading", "## 5 Conclusion", 3)
    env.add("b4", 1, "para", "Short paragraph about what we conclude from the study.", 4)
    chunks = ks._collect_chunks(PAPER)
    body = texts(chunks)
    assert len(body) == 2, "跨小节不合并（section_path 不同）"
    assert all(c["metadata"]["section_path"] for c in body)


def test_heading_only_chunk_is_not_merged(env) -> None:
    """标题后紧跟原子块 → 这条只有标题的 chunk 不参与合并。"""
    env.add("b1", 1, "heading", "## 5 Training", 1)
    env.add("b2", 1, "figure", "ignored", 2)
    env.figure("b2", 1, "Figure 6: training curves", "b3")
    env.add("b3", 1, "caption", "**Figure 6: training curves**", 3)
    env.add("b4", 1, "para", "This section describes the training regime for our models "
                             "and the hardware schedule we used.", 4)
    chunks = ks._collect_chunks(PAPER)
    body = texts(chunks)
    assert body[0]["metadata"]["heading_only"] is True
    assert body[0]["id"] == f"{PAPER}:text:b1"
    assert len(body) == 2


def test_section_path_inherits_nearest_heading(env) -> None:
    env.add("b1", 1, "heading", "## 2 Background", 1)
    env.add("b2", 1, "heading", "### 2.3 Multi-Head Attention", 2)
    env.add("b3", 1, "para", "Instead of performing a single attention function with keys and "
                             "values, we project them several times.", 3)
    env.add("b4", 1, "heading", "## 3 Model", 4)
    env.add("b5", 1, "para", "Most competitive neural sequence transduction models have an "
                             "encoder-decoder structure.", 5)
    chunks = ks._collect_chunks(PAPER)
    body = texts(chunks)
    paths = {c["section"]: c["metadata"]["section_path"] for c in body}
    assert paths["2.3 Multi-Head Attention"] == "2 Background / 2.3 Multi-Head Attention"
    assert paths["3 Model"] == "3 Model"
    assert all(c["section"] for c in body), "正文 chunk 的 section 不该为空"


def test_section_kind_marks_appendix_but_keeps_content(env) -> None:
    """附录内容**要进知识库**，但必须能认出来（parser 的状态机标 section_kind）。"""
    env.add("b1", 1, "heading", "## 2 Method", 1)
    env.add("b2", 1, "para", "The quantizer maps vectors to bit strings with bounded error.", 2)
    env.add("b3", 15, "heading", "## AEXTENDED PRIOR WORK", 3)
    env.add("b4", 15, "para", "For a random variable x we denote its differential entropy as "
                              "h(x) and use it to bound the distortion.", 4)
    env.blocks[2].meta_json = '{"section_kind": "appendix"}'      # parser 的标注
    env.blocks[3].meta_json = '{"section_kind": "appendix"}'
    chunks = ks._collect_chunks(PAPER)
    body = texts(chunks)
    kinds = {c["metadata"]["section_kind"] for c in body}
    assert kinds == {"body", "appendix"}
    appendix = [c for c in body if c["metadata"]["section_kind"] == "appendix"]
    assert "differential entropy" in appendix[0]["content"], "附录内容不能被丢掉"


# --------------------------------------------------------------------------
# 双语行：公式只出 any 一行
# --------------------------------------------------------------------------


def test_variants_emit_single_any_row_for_formula(env) -> None:
    _formula_section(env)
    env.pages = [{"page": 1, "rows": [
        {"original": "The quantization error is defined as:", "translation": "量化误差定义为："},
        {"original": "where x represents the input vector and x-hat its reconstruction.",
         "translation": "其中 x 表示输入向量，x-hat 是其重建。"},
    ]}]
    rows = ks.build_chunks_for_paper(PAPER)
    langs = [r["lang"] for r in rows]
    assert langs.count("any") == 1
    assert langs.count("zh") == 1 and langs.count("en") == 1
    formula = [r for r in rows if r["chunk_type"] == "formula"][0]
    assert formula["lang"] == "any" and "pair_id" not in formula["metadata"]
    zh = [r for r in rows if r["lang"] == "zh"][0]
    assert "\\begin{aligned}" in zh["content"], "中文侧也要原样带上公式"
    assert "量化误差定义为" in zh["content"]


def test_variants_keep_zh_en_pair_id(env) -> None:
    _formula_section(env)
    env.pages = [{"page": 1, "rows": [
        {"original": "The quantization error is defined as:", "translation": "量化误差定义为："},
        {"original": "where x represents the input vector and x-hat its reconstruction.",
         "translation": "其中 x 表示输入向量。"},
    ]}]
    rows = ks.build_chunks_for_paper(PAPER)
    zh = [r for r in rows if r["lang"] == "zh"][0]
    en = [r for r in rows if r["lang"] == "en"][0]
    assert zh["metadata"]["pair_id"] == en["metadata"]["pair_id"] == zh["id"].rsplit(":", 1)[0]


def test_missing_translation_yields_only_en_row(env) -> None:
    env.add("b1", 1, "heading", "## 2 Method", 1)
    env.add("b2", 1, "para", "This paragraph has no translation at all in the page product.",
            2)
    env.pages = []
    rows = ks.build_chunks_for_paper(PAPER)
    assert {r["lang"] for r in rows} == {"en"}


# --------------------------------------------------------------------------
# 参数（setting 覆盖 + 钳制）
# --------------------------------------------------------------------------


def test_chunk_params_reads_settings_and_clamps(monkeypatch) -> None:
    values = {"kb.chunk_target_chars": 1600, "kb.chunk_min_chars": 10,
              "kb.chunk_hard_chars": 100}
    monkeypatch.setattr(ks, "_setting_int", lambda key, default: values.get(key, default))
    target, floor, hard = ks.chunk_params()
    assert target == 1600
    assert floor == ks.RESIDUE_CHARS, "min 不得低于残渣门槛"
    assert hard == 1600, "hard 不得小于 target"


def test_chunk_params_defaults(monkeypatch) -> None:
    monkeypatch.setattr(ks, "_setting_int", lambda key, default: default)
    assert ks.chunk_params() == (ks.TARGET_CHUNK_CHARS, ks.MIN_CHUNK_CHARS,
                                 ks.HARD_CHUNK_CHARS)
    assert (ks.TARGET_CHUNK_CHARS, ks.MIN_CHUNK_CHARS, ks.HARD_CHUNK_CHARS) == (1400, 200, 2240)
