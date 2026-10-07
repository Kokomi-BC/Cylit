"""论文元数据抽取 / 补全的回归测试。

为什么要专门测：这几个字段以前**根本没人抽**（论文库里「会议 / 期刊」「年份」「摘要」
长期为空），一旦抽错又会直接写进论文行（比空值更难发现）。这里锁住三件事：

1. 首页竖排的 arXiv 水印**不能**被当成标题（实测字号 20pt > 标题 17.2pt，
   这篇论文的标题在库里就一直显示成 ``arXiv:1706.03762v5 …``）；
2. 摘要只取摘要那一段，不能把引言吞进来；
3. 写回只填空字段（手改过的值不能被覆盖）。
"""

from __future__ import annotations

from pathlib import Path

import pymupdf as fitz
import pytest

from backend.parsing.metadata import PaperMeta, extract_paper_meta, meta_from_text

# 首页排版：抬头（会议）+ 标题 + 两栏作者 + 旋转的 arXiv 水印 + 摘要 + 引言
_FIRST_PAGE = [
    (72, 60, 9.0, "Published as a conference paper at ICLR 2026"),
    (72, 110, 17.0, "TURBOQUANT: ONLINE VECTOR QUANTIZATION"),
    (72, 132, 17.0, "WITH NEAR-OPTIMAL DISTORTION RATE"),
    (72, 170, 10.0, "Amir Zandieh"),
    (300, 170, 10.0, "Google Research"),
    (72, 190, 10.0, "Majid Daliri"),
    (300, 190, 10.0, "New York University"),
    (72, 210, 10.0, "zandieh@google.com"),
    (72, 260, 12.0, "ABSTRACT"),
    (72, 280, 10.0, "Vector quantization aims to quantize high-dimensional vectors while"),
    (72, 292, 10.0, "minimizing distortion in their geometric structure. We propose a"),
    (72, 304, 10.0, "data-oblivious quantizer with near-optimal distortion rates."),
    (72, 350, 12.0, "1 Introduction"),
    (72, 370, 10.0, "Vector quantization (VQ) in Euclidean space is crucial for efficiently"),
    (72, 382, 10.0, "handling high-dimensional vectors across many domains."),
]

_ABSTRACT = ("Vector quantization aims to quantize high-dimensional vectors while "
             "minimizing distortion in their geometric structure. We propose a "
             "data-oblivious quantizer with near-optimal distortion rates.")


@pytest.fixture()
def fake_pdf(tmp_path: Path) -> Path:
    """造一份"排版正确"的单页 PDF：抬头 + 标题 + 作者 + 摘要 + 竖排水印。"""
    doc = fitz.open()
    page = doc.new_page(width=612, height=792)
    for x, y, size, text in _FIRST_PAGE:
        page.insert_text((x, y), text, fontsize=size)
    # 旋转 90° 的水印：字号比标题还大，位置在左页边（真实 arXiv 稿件就是这样）
    page.insert_text((20, 700), "arXiv:2605.12345v1  [cs.LG]  13 May 2026",
                     fontsize=20, rotate=90)
    doc.save(str(tmp_path / "fake.pdf"))
    doc.close()

    out = tmp_path / "fake.pdf"
    assert out.is_file()
    return out


def test_extracts_title_authors_venue_year_abstract(fake_pdf: Path) -> None:
    with fitz.open(str(fake_pdf)) as doc:
        meta = extract_paper_meta(doc, fake_pdf)
    assert meta.title == "TURBOQUANT: ONLINE VECTOR QUANTIZATION WITH NEAR-OPTIMAL DISTORTION RATE"
    assert "Amir Zandieh" in meta.authors and "Majid Daliri" in meta.authors
    assert "Google Research" not in meta.authors          # 机构不是作者
    assert "zandieh@google.com" not in meta.authors       # 邮箱不是作者
    assert meta.venue == "ICLR 2026"
    assert meta.year == 2026
    assert meta.abstract == _ABSTRACT                     # 摘要**不含**引言那句


def test_rotated_watermark_is_not_a_title(fake_pdf: Path) -> None:
    """回归：竖排 arXiv 水印字号比标题大，不能被选成标题。"""
    with fitz.open(str(fake_pdf)) as doc:
        meta = extract_paper_meta(doc, fake_pdf)
    assert "arXiv" not in meta.title


def test_meta_from_text_stops_at_next_section() -> None:
    """扫描件：视觉 OCR 的 Markdown 里同样要能抽出摘要与会议。"""
    md = ("# Some Paper\n\n## ABSTRACT\n\nWe study quantization for retrieval and\n"
          "propose a scalable method with near optimal rates. Our approach randomly\n"
          "rotates the input vectors, applies optimal scalar quantizers per coordinate,\n"
          "and stays unbiased for inner product estimation at every bit width.\n\n"
          "## 1 Introduction\n\nThis paragraph must not be part of the abstract at all.\n")
    meta = meta_from_text(md, title="Some Paper")
    assert meta.title == "Some Paper"
    assert meta.abstract.startswith("We study quantization")
    assert "must not be part" not in meta.abstract


def test_fills_skips_empty_values() -> None:
    meta = PaperMeta(title="T", venue="", year=None, abstract="A")
    assert meta.fills() == {"title": "T", "abstract": "A"}


def test_normalize_meta_drops_junk() -> None:
    """模型返回的脏值（瞎编的年份、一句话假摘要）必须被丢掉。"""
    from backend.services.paper.metadata_fill import normalize_meta

    assert normalize_meta({"venue": "", "year": 0, "tags": [], "abstract_zh": "太短"}) == {}
    ok = normalize_meta({"venue": "ICLR 2026", "year": 2026, "tags": ["量化", "量化", "检索"],
                         "abstract_zh": "这是一段足够长的中文摘要。" * 8})
    assert ok["venue"] == "ICLR 2026" and ok["year"] == 2026
    assert ok["tags"] == ["量化", "检索"]                     # 去重（列表形态由 update_paper 落成 CSV）
    assert ok["abstract_zh"].startswith("这是一段足够长的中文摘要。")


def test_normalize_meta_keeps_the_new_fields() -> None:
    """2026-10-04 新增的字段：中英文关键词 / 领域 / 概括 / 创新点 / 前置知识 /
    难度 / 类型 / 阅读时间 / DOI / 开源地址。"""
    from backend.services.paper.metadata_fill import normalize_meta

    ok = normalize_meta({
        "keywords_en": ["vector quantization", "retrieval"],
        "domain": "向量检索与量化", "one_liner": "用两阶段量化器消除内积偏差。",
        "contributions": ["两阶段量化器", "近最优失真率"],
        "prereq": ["乘积量化", "内积估计"],
        "difficulty": "进阶", "paper_type": "方法", "read_time_min": 42,
        "doi": "https://doi.org/10.48550/arXiv.2605.12345",
        "code_url": "https://github.com/google-research/turboquant",
    })
    assert ok["keywords_en"] == ["vector quantization", "retrieval"]
    assert ok["domain"] == "向量检索与量化"
    assert ok["contributions"] == ["两阶段量化器", "近最优失真率"]
    assert ok["difficulty"] == "进阶" and ok["paper_type"] == "方法"
    assert ok["read_time_min"] == 42
    assert ok["doi"] == "10.48550/arXiv.2605.12345"          # 去掉 doi.org 前缀
    assert ok["code_url"] == "https://github.com/google-research/turboquant"


def test_normalize_meta_rejects_values_outside_the_closed_sets() -> None:
    """难度 / 类型是**闭集**（自由文本会让界面没法按它筛选），非法值一律丢掉。"""
    from backend.services.paper.metadata_fill import normalize_meta

    assert normalize_meta({"difficulty": "简单", "paper_type": "论文",
                           "read_time_min": 9999, "code_url": "ftp://x/y"}) == {}
    assert normalize_meta({"difficulty": "入门", "paper_type": "综述",
                           "read_time_min": 0}) == {"difficulty": "入门", "paper_type": "综述"}


def test_offline_extras_reads_doi_and_code_url_only_from_the_first_pages() -> None:
    """DOI / 开源地址只在**前两页**里找：翻遍全文会捞到参考文献里别人的。"""
    from backend.services.paper import metadata_fill

    rows = [(1, "para", "Published at https://doi.org/10.48550/arXiv.2605.12345 .", ""),
            (1, "para", "代码在 https://github.com/google-research/turboquant。", ""),
            (7, "para", "https://github.com/someone-else/other", "")]
    got = metadata_fill.offline_extras(rows)
    assert got["doi"] == "10.48550/arxiv.2605.12345"
    assert got["code_url"] == "https://github.com/google-research/turboquant"
    assert metadata_fill.offline_extras([(9, "para", "https://github.com/x/y", "")]) == {}


def test_scan_rows_reads_the_right_row_keys(monkeypatch) -> None:
    """`scan_rows` 必须按行的真实键（`original` / `translation`）取文本，且**两侧都带回来**。

    这条测的就是一个真实 bug：第一版写成 `row["text"]`，函数永远返回空列表 ——
    阅读时间永远填不上、DOI/开源地址永远抽不到（参考文献靠 Markdown 兜底所以看着还正常）。
    2026-10-07 起返回 4 元组（原文 + 译文）：参考文献要拿译文里的《中文标题》跟原文对齐着切。
    """
    from backend.services.paper import metadata_fill, reader_service

    monkeypatch.setattr(reader_service, "page_translations", lambda pid: [{
        "page": 1,
        "rows": [
            {"id": "p001-00", "type": "heading", "original": "# 标题", "translation": "# 标题"},
            {"id": "p001-01", "type": "para", "original": "正文 A", "translation": "正文 A 的中文"},
            {"id": "p001-02", "type": "para", "translation": "只有译文的一行"},
        ],
    }])
    rows = metadata_fill.scan_rows("p1")
    assert rows == [
        (1, "heading", "# 标题", "# 标题"),
        (1, "para", "正文 A", "正文 A 的中文"),
        (1, "para", "", "只有译文的一行"),
    ]
    # 连起来才够"至少 500 字"，所以这里顺便钉住阅读时间确实按这些行算
    assert metadata_fill.estimate_read_time([(1, "para", "字" * 600, "")]) == 1


def test_estimate_read_time_ignores_references_and_needs_real_text() -> None:
    """阅读时间按**正文**字数估；参考文献/图注不算，没有页产物时干脆不给。"""
    from backend.services.paper import metadata_fill

    assert metadata_fill.estimate_read_time([(1, "para", "太短", "")]) is None
    rows = [(1, "para", "字" * 9000, ""), (2, "ref", "字" * 90000, "")]
    assert metadata_fill.estimate_read_time(rows) == 10


def test_split_ref_text_reads_the_chinese_title_and_hides_authors() -> None:
    """一条参考文献的「原文 + 译文」→ 中文标题 / 英文标题 / 作者段（用户口径 2026-10-07）。

    约定（提示词里写死的）：译文**只把标题换成《中文标题》**，作者与会议/年份/链接逐字照抄原文。
    于是两边是同一副骨架：`《…》` 之前是作者段、之后是会议/年份/链接，中间剩下的就是英文标题。
    """
    from backend.services.paper import metadata_fill

    raw = ("Josh Achiam, Steven Adler, Sandhini Agarwal. GPT-4 technical report. "
           "arXiv preprint arXiv:2303.08774, 2023.")
    translated = ("Josh Achiam, Steven Adler, Sandhini Agarwal. 《GPT-4 技术报告》. "
                  "arXiv preprint arXiv:2303.08774, 2023.")
    got = metadata_fill.split_ref_text(raw, translated)
    assert got["title"] == "GPT-4 技术报告"
    assert got["authors"] == "Josh Achiam, Steven Adler, Sandhini Agarwal."
    assert got["title_en"] == "GPT-4 technical report"      # 结尾那个句号/分隔符会剥掉
    # 中文标题永远认得出来；模型没照抄作者段时，英文标题**退到规则切法**（见下一条测试）
    loose = metadata_fill.split_ref_text(raw, "某作者. 《GPT-4 技术报告》. 某会议, 2023.")
    assert loose["title"] == "GPT-4 技术报告"
    assert loose["title_en"] == "GPT-4 technical report"
    assert loose["authors"] == "Josh Achiam, Steven Adler, Sandhini Agarwal."
    # 压根没翻译（没有《》）→ 中文标题没有，但规则依旧切得出英文标题（作者段照旧收起来）
    plain = metadata_fill.split_ref_text(raw, raw)
    assert plain["title"] is None
    assert plain["title_en"] == "GPT-4 technical report"
    assert plain["authors"] == "Josh Achiam, Steven Adler, Sandhini Agarwal."
    assert metadata_fill.split_ref_text(raw, None)["title"] is None


def test_split_ref_text_anchors_on_the_chinese_book_title() -> None:
    """**以中文那侧的《》为标准**（用户口径 2026-10-07）：书名号外面那两段去原文里定位，
    容忍空白 / 强调记号的差异 —— 定位到的边界夹出来的就是英文标题。"""
    from backend.services.paper import metadata_fill

    raw = ("Ran Hadad and Uri Erez. Dithered quantization via orthogonal transformations. "
           "*IEEE Transactions on Signal Processing*, 64(22):5887–5900, 2016.")
    # ① 中文侧抄成两个空格（原文一个）→ 归一化后仍定位得到
    got = metadata_fill.split_ref_text(
        raw, "Ran Hadad and  Uri Erez. 《正交变换下的抖动量化》. "
             "IEEE Transactions on Signal Processing, 64(22):5887–5900, 2016.")
    assert got["title"] == "正交变换下的抖动量化"
    assert got["title_en"] == "Dithered quantization via orthogonal transformations"
    assert got["authors"] == "Ran Hadad and  Uri Erez."
    # ② 原文给出处加了斜体、中文侧没有 → 出处的**尾部**照样能定位（旧实现直接把它算进标题）
    got = metadata_fill.split_ref_text(
        raw, "Ran Hadad and Uri Erez. 《正交变换下的抖动量化》. "
             "IEEE Transactions on Signal Processing, 64(22):5887–5900, 2016.")
    assert got["title_en"] == "Dithered quantization via orthogonal transformations"
    # ③ 中文侧没抄出处 → 只夹作者段那一头，剩下的整段都算标题
    got = metadata_fill.split_ref_text(raw, "Ran Hadad and Uri Erez. 《正交变换下的抖动量化》.")
    assert got["title_en"].startswith("Dithered quantization via orthogonal transformations")
    # ④ 出处对不上（模型改写了出处）→ 不硬切：只回中文标题，英文标题交给规则
    got = metadata_fill.split_ref_text(raw, "某作者. 《正交变换下的抖动量化》. 某会议, 2016.")
    assert got["title"] == "正交变换下的抖动量化"


def test_reference_pairs_reads_the_section_rows(monkeypatch) -> None:
    """有页产物时**直接取"参考文献那一节"的行**：原文与译文天然成对，中文标题不会因为
    两套切分（Markdown bbox 切 vs 行块切）对不上而丢（用户前后报了两次）。"""
    from backend.services.paper import metadata_fill, reader_service

    monkeypatch.setattr(reader_service, "build_outline", lambda _pid: {"items": [
        {"page": 10, "level": 1, "text": "REFERENCES"},
        {"page": 12, "level": 1, "text": "A APPENDIX"},
    ]})
    rows = [
        (10, "para", "Old conclusion text that is long enough to count as a paragraph.",
         "旧的结论正文，长度也够"),
        (10, "heading", "# References", "参考文献"),
        (10, "para", "Ran Hadad and Uri Erez. Dithered quantization via orthogonal "
                     "transformations. IEEE TSP, 2016.",
         "Ran Hadad and Uri Erez. 《正交变换下的抖动量化》. IEEE TSP, 2016."),
        (11, "formula", "$$x = 1$$", "$$x = 1$$"),
        (12, "para", "Appendix text that is long enough to count as a paragraph.", "附录正文"),
    ]
    got = metadata_fill.reference_pairs("p", rows)
    assert len(got) == 1, got                    # 标题之前的行不算、公式不算、附录页不算
    assert got[0][2] == 10                       # 页码带上（侧栏将来要用）
    assert metadata_fill.split_ref_text(got[0][0], got[0][1])["title"] == "正交变换下的抖动量化"
    assert got[0][0].startswith("Ran Hadad")


def test_reference_pairs_claims_translations_across_parser_artifacts(monkeypatch) -> None:
    """没有编号的参考文献走 Markdown 兜底时要**认领译文**（对上了才有中文标题）。

    两边是同一段文字，只差解析器/排版留下的痕迹：句号处丢空格、重音写成独立字符、
    行尾连字符换行（实测三类都让 turboquant 的条目拿不到中文标题）。
    """
    from backend.services.paper import metadata_fill

    row = ("Guangxuan Xiao, Ji Lin, and Song Han. Smoothquant: Accurate and efficient "
           "post-training quantization for large language models. In International "
           "Conference on Machine Learning, 2023.")
    zh = ("Guangxuan Xiao, Ji Lin, and Song Han. 《Smoothquant：面向大语言模型的精确高效训练后量化》. "
          "In International Conference on Machine Learning, 2023.")
    monkeypatch.setattr(metadata_fill, "_references_from_markdown",
                        lambda _pid: [row.replace("models. In", "models.In"),
                                      "Peter Richt´arik and Dan Alistarh. Pushing the "
                                      "lim- its of quantization. ICLR, 2022."])
    got = metadata_fill.reference_pairs("p", [(14, "para", row, zh)])
    assert got[0][1] is not None and "Smoothquant" in got[0][1], "丢空格的条目也要认领到译文"
    assert got[0][2] == 14, "认领到就把页码一起带上"
    assert got[0][0].startswith("Guangxuan Xiao") and "models. In" in got[0][0], \
        "raw 用行里那份（Markdown 那份在句号处丢了空格）"
    assert got[1][1] is None and got[1][2] is None, "行里没有的条目原样保留、没有译文"


def test_reference_rule_hides_the_author_block() -> None:
    """用户口径（2026-10-07）：`作者名… .` 之后是题名的 → 只显示文献名称；形状对不上就整条原文。"""
    from backend.services.paper.metadata_fill import split_ref_by_rule

    assert split_ref_by_rule(
        "Ran Hadad and Uri Erez. Dithered quantization via orthogonal transformations. "
        "IEEE Transactions on Signal Processing, 64(22):5887–5900, 2016."
    ) == {"title": None, "title_en": "Dithered quantization via orthogonal transformations",
          "authors": "Ran Hadad and Uri Erez."}
    # 名字**中间**的缩写不能当作者段的结尾（实测踩过：`Max Belitsky, Dawid J. Kopiczko` 被切成
    # 作者=`Max Belitsky, Dawid J.`、标题=`Kopiczko…`）
    mid = split_ref_by_rule("Max Belitsky, Dawid J. Kopiczko, and Yuki M. Asano. "
                            "Kv cache steering for inducing reasoning in small language models, "
                            "2025. URL https://arxiv.org/abs/2507.08799.")
    assert mid["authors"] == "Max Belitsky, Dawid J. Kopiczko, and Yuki M. Asano."
    assert mid["title_en"] == "Kv cache steering for inducing reasoning in small language models"
    # 斜体书名 / 缩写 / 一堆作者（GPT-3 那种 481 字符的作者段）
    assert split_ref_by_rule(
        "Thomas M Cover. *Elements of information theory*. John Wiley & Sons, 1999."
    )["title_en"] == "Elements of information theory"
    assert split_ref_by_rule(
        "Tom B. Brown, Benjamin Mann, and Dario Amodei. Language models are few-shot "
        "<!-- ===== page 11 ===== --> learners. In Advances in Neural Information Processing "
        "Systems, 2020.")["title_en"] == "Language models are few-shot learners"
    # 形状对不上 → None（前端保持"整条原文"）
    assert split_ref_by_rule("Pgvector search., 2025. https://github.com/pgvector/pgvector/.") is None
    assert split_ref_by_rule("https://www.elastic.co/x Elasticsearch.,2025. vector-search.") is None
    assert split_ref_by_rule("") is None


def test_references_from_markdown_splits_numbered_entries(monkeypatch, tmp_path: Path) -> None:
    """没有页产物（还没翻译）时退回 Markdown：按编号切条目、遇到下一个标题就停。"""
    from backend.services.paper import metadata_fill, paper_service

    md = tmp_path / "paper.md"
    md.write_text(
        "# 5 Conclusion\n\nDone.\n\n## References\n\n"
        "[1] Zandieh A. TurboQuant: online vector quantization. ICLR, 2026.\n"
        "    With near-optimal distortion rates.\n"
        "[2] Vaswani A. Attention is all you need. NeurIPS, 2017.\n\n"
        "## Appendix\n\n[9] 这不在参考文献小节里\n", encoding="utf-8")
    monkeypatch.setattr(paper_service, "paper_paths", lambda pid: {"markdown": md})
    items = metadata_fill._references_from_markdown("p1")
    assert len(items) == 2
    assert items[0].startswith("[1] Zandieh") and "near-optimal" in items[0]
    assert items[1].startswith("[2] Vaswani")


def test_parse_ref_section_handles_unnumbered_entries_with_block_markers() -> None:
    """用户 2026-10-07 报的"这篇论文没有参考文献"（侧栏显示 0 条）：条目**没有编号**。

    实测那篇（ICLR-26）的产物长这样：`## REFERENCES` 下面每个块都是
    `<!-- blk:… type:ref … bbox:108.0,… -->` + `<a id=…></a>` + 一行条目，
    续行缩进（x0 108 → 118/258）。旧实现只认 `[12]` / `12.` 开头 → **一条都切不出来**。
    """
    from backend.services.paper import metadata_fill

    md = (
        "## REFERENCES\n\n"
        "<!-- blk:p1 type:heading page:1 bbox:108.0,10.0,200.0,20.0 -->\n"
        "<a id=\"blk-p1\"></a>\n"
        "REFERENCES\n\n"
        "<!-- blk:p2 type:ref page:1 bbox:258.4,30.0,500.0,40.0 -->\n"
        "<a id=\"blk-p2\"></a>\n"
        "https://www.elastic.co/enterprise-search/\n\n"
        "<!-- blk:p3 type:ref page:1 bbox:108.0,30.0,350.0,60.0 -->\n"
        "<a id=\"blk-p3\"></a>\n"
        "Elastic search., 2025. https://www.elastic.co/enterprise-search/\n\n"
        "<!-- blk:p4 type:ref page:1 bbox:118.0,60.0,350.0,70.0 -->\n"
        "<a id=\"blk-p4\"></a>\n"
        "SIAM, 2020.\n\n"
        "<!-- blk:p5 type:ref page:1 bbox:108.0,80.0,350.0,110.0 -->\n"
        "<a id=\"blk-p5\"></a>\n"
        "Josh Achiam, Steven Adler. GPT-4 technical report. 2023.\n\n"
        "## Appendix\n\n"
        "<!-- blk:p6 type:ref page:1 bbox:108.0,120.0,350.0,140.0 -->\n"
        "这不在参考文献小节里\n"
    )
    items = metadata_fill._parse_ref_section(md)
    # 顶格（x0=108）才开新条：右栏那半截 URL 归到第一条、缩进的 SIAM 归到它上一条
    assert len(items) == 2, items
    assert items[0].startswith("https://www.elastic.co") and "Elastic search" in items[0]
    assert items[0].endswith("SIAM, 2020.")
    assert items[1].startswith("Josh Achiam")
    assert all("这不在参考文献小节里" not in t for t in items)   # 下一个标题就停


def test_parse_ref_section_unnumbered_without_block_markers() -> None:
    """更老的产物没有结构标记：退回"空行分段"，一段一条（同样不要求编号）。"""
    from backend.services.paper import metadata_fill

    md = ("## References\n\n"
          "Elastic search. 2025. https://www.elastic.co/\n\n"
          "GPT-4 technical report. 2023.\n")
    assert metadata_fill._parse_ref_section(md) == [
        "Elastic search. 2025. https://www.elastic.co/",
        "GPT-4 technical report. 2023.",
    ]
    assert metadata_fill._parse_ref_section("## Introduction\n\n没有参考文献节。\n") == []


def test_apply_fills_only_fills_empty(monkeypatch) -> None:
    """写回只填空字段：用户手改过的元数据不能被自动值盖掉。"""
    from backend.services.paper import metadata_fill, paper_service

    row = {"authors": "我手填的作者", "venue": "", "year": None, "tags": [],
           "abstract": "已有的摘要", "contributions": []}
    patch: dict = {}
    sources: list[str] = []
    monkeypatch.setattr(paper_service, "get_paper",
                        lambda pid, include_deleted=False: dict(row))

    def _update(pid, payload, source="manual"):
        patch.update(payload)
        sources.append(source)
        return {}

    monkeypatch.setattr(paper_service, "update_paper", _update)

    applied = metadata_fill.apply_fills(
        "p1", {"authors": "抽出来的作者", "venue": "ICLR 2026", "year": 2026,
               "tags": "量化,检索", "abstract": "新摘要",
               "contributions": ["两阶段量化器"]}, source="pdf")
    assert applied == {"venue": "ICLR 2026", "year": 2026, "tags": "量化,检索",
                       "contributions": ["两阶段量化器"]}
    assert "authors" not in applied and "abstract" not in applied
    assert patch == applied
    # 来源要一路带到论文行（meta_sources_json 靠它）：默认 manual、这里显式给了 pdf
    assert sources == ["pdf"]

    patch.clear()
    forced = metadata_fill.apply_fills("p1", {"authors": "抽出来的作者"}, overwrite=True)
    assert forced == {"authors": "抽出来的作者"} and patch["authors"] == "抽出来的作者"
    assert sources[-1] == "model"


def test_structured_tolerates_python_style_dict(monkeypatch) -> None:
    """回归：真实调用里模型交回过 ``{'venue': '', 'year': 0}``（单引号），
    以前 `json.loads` 失败 → 整次结构化输出作废（元数据/简报都拿不到）。"""
    from backend.providers.generation.openai_compatible import OpenAICompatibleGeneration

    provider = OpenAICompatibleGeneration({"model_name": "fake"})

    class _Result:
        text = "```json\n{'venue': 'ICLR 2026', 'year': 2026, 'tags': ['量化']}\n```"

    monkeypatch.setattr(provider, "generate", lambda *a, **k: _Result())
    data = provider.structured("prompt", {"type": "object", "properties": {}})
    assert data["venue"] == "ICLR 2026" and data["year"] == 2026


def test_brief_writes_back_meta_in_same_call(monkeypatch, tmp_path: Path) -> None:
    """用户口径：元数据"和术语一同生成" —— 简报那一次调用就要把字段写回论文行。"""
    from backend.services.paper import paper_service
    from backend.services.translation import brief as brief_mod
    from backend.services.translation import translation_service

    monkeypatch.setattr(paper_service, "paper_paths",
                        lambda pid: {"root": tmp_path})
    monkeypatch.setattr(paper_service, "get_paper",
                        lambda pid, include_deleted=False: {"id": pid, "title": "T",
                                                           "venue": "", "year": None,
                                                           "tags": [], "abstract": ""})
    patch: dict = {}
    monkeypatch.setattr(paper_service, "update_paper",
                        lambda pid, payload, source="manual": patch.update(payload) or {})
    monkeypatch.setattr(brief_mod, "sample_paper_text", lambda pid, limit=6000: "标题：T")
    monkeypatch.setattr(brief_mod, "_model_id", lambda: "mock")
    monkeypatch.setattr(translation_service, "load_glossary",
                        lambda pid, limit=24: [])

    class _Provider:
        def structured(self, prompt, schema, system=""):
            assert "meta" in schema["properties"]          # 元数据与术语同一份 schema
            return {"domain": "向量量化", "glossary": [{"en": "quantization", "zh": "量化"}],
                    "meta": {"venue": "ICLR 2026", "year": 2026, "tags": ["量化"],
                             "abstract_zh": "中文摘要。" * 20}}

    brief = brief_mod.generate_brief("p1", provider=_Provider(), force=True)
    assert brief.meta["venue"] == "ICLR 2026"
    assert patch["venue"] == "ICLR 2026" and patch["year"] == 2026
    assert patch["tags"] == ["量化"]            # 列表归一在写库那一层（`update_paper`）做
    assert "ICLR 2026" in brief_mod.load_brief("p1").meta["venue"]   # 落盘可复查

    # 简报前缀必须**字节级稳定**：元数据不参与 prompt（否则人工改元数据会打掉前缀缓存）
    assert "ICLR 2026" not in brief.prompt_block()
