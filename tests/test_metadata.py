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
    assert ok["tags"] == "量化,检索"                       # 去重
    assert ok["abstract"].startswith("这是一段足够长的中文摘要。")


def test_apply_fills_only_fills_empty(monkeypatch) -> None:
    """写回只填空字段：用户手改过的元数据不能被自动值盖掉。"""
    from backend.services.paper import metadata_fill, paper_service

    row = {"authors": "我手填的作者", "venue": "", "year": None, "tags": [],
           "abstract": "已有的摘要"}
    patch: dict = {}
    monkeypatch.setattr(paper_service, "get_paper",
                        lambda pid, include_deleted=False: dict(row))
    monkeypatch.setattr(paper_service, "update_paper",
                        lambda pid, payload: patch.update(payload) or {})

    applied = metadata_fill.apply_fills(
        "p1", {"authors": "抽出来的作者", "venue": "ICLR 2026", "year": 2026,
               "tags": "量化,检索", "abstract": "新摘要"})
    assert applied == {"venue": "ICLR 2026", "year": 2026, "tags": "量化,检索"}
    assert "authors" not in applied and "abstract" not in applied
    assert patch == applied

    patch.clear()
    forced = metadata_fill.apply_fills("p1", {"authors": "抽出来的作者"}, overwrite=True)
    assert forced == {"authors": "抽出来的作者"} and patch["authors"] == "抽出来的作者"


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
                        lambda pid, payload: patch.update(payload) or {})
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
    assert patch["tags"] == "量化"
    assert "ICLR 2026" in brief_mod.load_brief("p1").meta["venue"]   # 落盘可复查

    # 简报前缀必须**字节级稳定**：元数据不参与 prompt（否则人工改元数据会打掉前缀缓存）
    assert "ICLR 2026" not in brief.prompt_block()
