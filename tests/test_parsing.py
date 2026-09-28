"""解析层单元测试（不依赖数据库与网络）。

覆盖：几何/文本工具、段落切分统计判据、公式矢量层审计、LaTeX 候选与三重校验、
Markdown 往返（译文回写与解析）、block_id 稳定重绑定。
"""

from __future__ import annotations

import pytest

from backend.core.utils import ends_sentence, join_lines, normalize_text, sha256_text
from backend.parsing.extract.vector_layer import Drawing
from backend.parsing.formula.audit import audit_formula_vectors, find_horizontal_bars
from backend.parsing.formula.latex_recover import literal_candidate, recover_candidates
from backend.parsing.formula.validate import (
    extract_constants, grade_formula, validate_formula,
)
from backend.parsing.ir.rebind import (
    BlockFingerprint, assign_ids_in_order, rebind_block_ids,
)
from backend.parsing.md.generator import (
    extract_translations, parse_markdown, upsert_translation,
)
from backend.parsing.models import Block, ParseResult, Rect, Span, TextLine
from backend.parsing.structure.paragraph import (
    build_line_profile, classify_text, group_lines_into_paragraphs, is_body_line,
)
from backend.parsing.qa.consistency import extract_constants_from_text


# --------------------------------------------------------------------------
# 基础工具
# --------------------------------------------------------------------------


def test_join_lines_merges_hyphenation():
    assert join_lines(["distribu-", "tion of bits"]) == "distribution of bits"


def test_join_lines_cjk_no_space():
    assert join_lines(["向量量化", "是一个问题"]) == "向量量化是一个问题"


def test_ends_sentence():
    assert ends_sentence("This is a sentence.")
    assert ends_sentence('He said "done."')
    assert not ends_sentence("a broken line that continues")
    assert ends_sentence("这是一个中文句子。")


def test_rect_helpers():
    a = Rect(0, 0, 10, 10)
    b = Rect(5, 5, 20, 20)
    assert a.intersects(b)
    assert a.union(b).to_list() == [0.0, 0.0, 20.0, 20.0]
    assert Rect(0, 0, 10, 10).overlap_ratio(Rect(0, 0, 5, 10)) == pytest.approx(0.5)


# --------------------------------------------------------------------------
# 段落切分（统计判据）
# --------------------------------------------------------------------------


def _line(text: str, y: float, size: float = 10.0, x: float = 100.0) -> TextLine:
    return TextLine(spans=[Span(text=text, x0=x, y0=y, x1=x + len(text) * 5,
                                y1=y + size, font="Body", size=size,
                                origin_y=y + size * 0.8)],
                    page_no=1)


def test_paragraph_break_on_lead_jump():
    lines = [
        _line("First paragraph line one with enough characters here.", 100),
        _line("line two continues the same paragraph as expected.", 111),
        # 段间距（lead=21 >> 1.45×median 11）
        _line("Second paragraph starts after a bigger gap here.", 132),
    ]
    prof = build_line_profile(lines, 612, 792)
    groups = group_lines_into_paragraphs(lines, prof)
    assert len(groups) == 2
    assert len(groups[0]) == 2


def test_inline_math_does_not_split_paragraph():
    """正文中夹一个数学符号不应切段（PyMuPDF 行分组最常见的坑）。"""
    lines = [
        _line("the bound is 1 4b for any b ≥0 as we discussed earlier.", 100),
        _line("which means the error decays exponentially in b here.", 111),
    ]
    prof = build_line_profile(lines, 612, 792)
    groups = group_lines_into_paragraphs(lines, prof)
    assert len(groups) == 1


def test_is_body_line_filters_noise():
    prof = build_line_profile([_line("x" * 40, 100)], 612, 792)
    assert is_body_line(_line("12", 700), prof)[1] == "page_number"
    assert is_body_line(_line("(3)", 700), prof)[1] == "equation_number"
    assert is_body_line(_line("a@b.com", 700), prof)[1] == "email"
    assert is_body_line(_line("normal body text here", 300), prof)[0] is True


def test_classify_heading_strictness():
    common = dict(body_size=10.0, is_bold=False, page_width=612.0, rect_width=200.0)
    assert classify_text("2.1 MSE Optimal TurboQuant", font_size=10.0,
                         line_count=1, char_count=25, **common) == "heading"
    # 长正文即便字号稍大也不应判为标题
    long_text = "This is a long body paragraph that goes on and on with many words."
    assert classify_text(long_text, font_size=10.5, line_count=2,
                         char_count=len(long_text), **common) == "para"
    assert classify_text("Figure 3: Architecture of the model", font_size=9.0,
                         line_count=1, char_count=34, **common) == "caption"


# --------------------------------------------------------------------------
# 公式：矢量层审计与候选
# --------------------------------------------------------------------------


def _bar(x0: float, x1: float, y: float, width: float = 0.8) -> Drawing:
    return Drawing(kind="line", rect=Rect(x0, y - 0.4, x1, y + 0.4),
                   p0=(x0, y), p1=(x1, y), width=width, stroke=True, fill=False)


def test_find_horizontal_bars_filters():
    bars = [_bar(10, 20, 50), _bar(10, 60, 60), _bar(10, 60, 75, width=4.0)]
    found = find_horizontal_bars(bars, font_height=10.0)
    # 长度 10pt（在 [0.5, 3]×字高 内）且细 → 合格；长度 50pt 超上限；线宽 4pt 超上限
    assert len(found) == 1


def test_audit_radical_scope():
    """规划 §13.3 C1：横杠只覆盖 3，π 被排除 → radical 审计记录 covered/excluded。"""
    spans = [
        Span(text="3", x0=12, y0=52, x1=18, y1=62, size=10, origin_y=60),
        Span(text="π", x0=26, y0=52, x1=32, y1=62, size=10, origin_y=60),
    ]
    audit = audit_formula_vectors([_bar(10, 21, 50)], spans, Rect(10, 45, 40, 65),
                                 font_height=10.0)
    assert audit.kind == "radical"
    assert audit.radical_bars
    assert "3" in audit.radical_bars[0].covered_glyphs
    assert "π" in audit.radical_bars[0].excluded


def test_audit_fraction_line():
    spans = [
        Span(text="a", x0=12, y0=38, x1=18, y1=48, size=10, origin_y=46),
        Span(text="b", x0=12, y0=54, x1=18, y1=64, size=10, origin_y=62),
    ]
    audit = audit_formula_vectors([_bar(10, 22, 50)], spans, Rect(10, 35, 25, 66),
                                 font_height=10.0)
    assert "fraction" in audit.kind
    assert audit.fraction_lines[0].numerator_glyphs == ["a"]
    assert audit.fraction_lines[0].denominator_glyphs == ["b"]


def test_recover_candidates_includes_structured_and_literal():
    spans = [
        Span(text="3", x0=12, y0=52, x1=18, y1=62, size=10, origin_y=60),
    ]
    audit = audit_formula_vectors([_bar(10, 21, 50)], spans, Rect(10, 45, 40, 65),
                                 font_height=10.0)
    cands = recover_candidates("3", audit, spans)
    kinds = {c.kind for c in cands}
    assert "literal" in kinds
    assert any("sqrt" in c.latex for c in cands)


def test_validate_syntax_detects_unbalanced():
    res = validate_formula(r"\frac{a}{b", "a b", None, [])
    assert res.syntax_ok is False
    assert any("花括号" in i for i in res.issues)


def test_validate_constants_conflict():
    """规划 §13.3 C2 的真实错例：2.7 与 2.17 并存。"""
    rules = [{"symbol": r"\pi", "value_float": 2.7207, "note": "MSE 上界"}]
    ok, issues, checked = validate_formula(
        r"2.17 \cdot \pi", "2.17 π", None, rules).constants_ok, \
        validate_formula(r"2.17 \cdot \pi", "2.17 π", None, rules).issues, \
        validate_formula(r"2.17 \cdot \pi", "2.17 π", None, rules).constants_checked
    assert checked
    assert not ok
    assert any("常量冲突" in i for i in issues)


def test_extract_constants_from_text_roundtrip():
    found = extract_constants_from_text(r"where $\sqrt{3}\pi/2 \approx 2.7207$ holds")
    assert any(abs(c["value"] - 2.7207) < 1e-6 for c in found)


def test_grade_formula_plain_is_a():
    from backend.parsing.models import VectorAudit

    audit = VectorAudit(kind="plain")
    res = validate_formula("x = y", "x = y", audit, [])
    grade, conf, reasons = grade_formula(audit=audit, validation=res,
                                         candidate_kind="literal")
    assert grade == "A" and conf > 0.9 and reasons


def test_grade_formula_bitmap_is_c():
    from backend.parsing.models import VectorAudit

    grade, conf, _ = grade_formula(audit=VectorAudit(), validation=validate_formula(
        "x", "x", None, []), candidate_kind="literal", is_bitmap=True)
    assert grade == "C" and conf < 0.3


def test_extract_constants_skips_subscripts():
    assert 3.0 in extract_constants(r"\frac{\sqrt{3}\pi}{2}")


# --------------------------------------------------------------------------
# Markdown 往返
# --------------------------------------------------------------------------


def _mini_result() -> ParseResult:
    blocks = [
        Block(id="p01-b01", paper_id="t", page_no=1, order_in_page=1,
              order_global=1, type="heading",
              rect=Rect(0, 0, 100, 20), text="1 Introduction",
              text_clean="1 Introduction", font_size=12.0),
        Block(id="p01-b02", paper_id="t", page_no=1, order_in_page=2,
              order_global=2, type="para",
              rect=Rect(0, 30, 300, 60),
              text="Random rotation converts the worst case.",
              text_clean="Random rotation converts the worst case."),
    ]
    for b in blocks:
        b.finalize()
    return ParseResult(paper_id="t", title="Demo", blocks=blocks, page_count=1)


def test_markdown_roundtrip_translation():
    from backend.parsing.md.generator import render_markdown

    md = render_markdown(_mini_result(), paper_id="t")
    assert "<!-- blk:p01-b01" in md and "## 1 Introduction" in md

    md2 = upsert_translation(md, "p01-b02", "随机旋转把最坏情况转化为已知分布。", 1, "abc")
    parsed = parse_markdown(md2)
    assert parsed["p01-b02"]["translation"] == "随机旋转把最坏情况转化为已知分布。"
    assert "Random rotation" in parsed["p01-b02"]["text"]

    # 幂等：再次写入同一块应替换而不是追加
    md3 = upsert_translation(md2, "p01-b02", "第二次译文。", 2, "def")
    parsed3 = parse_markdown(md3)
    assert parsed3["p01-b02"]["translation"] == "第二次译文。"
    assert parsed3["p01-b02"]["revision"] == 2
    assert md3.count("<!-- tr:p01-b02") == 1
    assert extract_translations(md3)["p01-b02"]["text"] == "第二次译文。"


# --------------------------------------------------------------------------
# block_id 稳定重绑定
# --------------------------------------------------------------------------


def test_assign_ids_in_order_avoids_collision():
    blocks = [
        Block(id="", paper_id="t", page_no=1, order_in_page=0, order_global=1,
              rect=Rect(0, 0, 10, 10), text="a", text_clean="a"),
        Block(id="", paper_id="t", page_no=1, order_in_page=0, order_global=2,
              rect=Rect(0, 20, 10, 30), text="b", text_clean="b"),
    ]
    assign_ids_in_order(blocks, reserve={"demo-p01-b01"}, prefix="demo")
    assert {b.id for b in blocks} == {"demo-p01-b02", "demo-p01-b03"}


def test_block_id_prefix_differs_per_paper():
    """块 ID 必须带论文前缀，否则多篇论文会在共享业务库里主键冲突。"""
    from backend.parsing.ir.rebind import id_prefix, make_block_id

    assert id_prefix("turboquant-iclr26") == "turboquantic"
    assert id_prefix("e2e-check") == "e2echeck"
    assert make_block_id("e2echeck", 3, 7) == "e2echeck-p03-b07"


def test_block_id_prefix_extends_when_truncated_prefix_collides():
    """截断到 12 字符会撞车（实测 `12382-beyond-speedup-utilizing` 与 `…-2`）→ 自动加长。

    `block.id` 是共享业务库的全局主键，撞车会让后导入的那篇解析直接失败
    （`Duplicate entry '12382beyonds-p01-b01'`，IR 只写一半）。
    """
    from backend.parsing.ir.rebind import id_prefix

    taken = {"12382beyonds"}
    got = id_prefix("12382-beyond-speedup-utilizing-2", taken=taken)
    assert got != "12382beyonds" and got not in taken
    assert got.startswith("12382beyondspeed")           # 加长到 16 字符就够
    # 不冲突时**保持原样**：存量论文的前缀、块 ID、批注/高亮都不被打乱
    assert id_prefix("turboquant-online-vector-iclr-26", taken=taken) == "turboquanton"
    assert id_prefix("turboquant-online-vector-iclr-26") == "turboquanton"


def test_rebind_keeps_ids_for_unchanged_blocks():
    old = [
        BlockFingerprint(id="p01-b01", page_no=1, x0=10, y0=10, x1=200, y1=30,
                         type="para", text_fp="randomrotationconvertstheworst",
                         content_hash=sha256_text("Random rotation converts the worst.")),
    ]
    new = [
        Block(id="", paper_id="t", page_no=1, order_in_page=1, order_global=1,
              type="para", rect=Rect(10.5, 10.2, 200.4, 30.1),
              text="Random rotation converts the worst.",
              text_clean="Random rotation converts the worst."),
    ]
    new[0].finalize()
    report = rebind_block_ids(new, old)
    assert report.matched == 1
    assert new[0].id == "p01-b01"


def test_rebind_does_not_reuse_ids_from_another_prefix():
    """前缀撞车加长后，旧 IR 里那些**老前缀**的 ID 不能再沿用。

    实测事故（2026-09-28）：`12382-beyond-speedup-utilizing` 与 `…-3` 的 12 字符前缀
    都是 `12382beyonds`，`id_prefix` 给 `-3` 加长到 16 字符，但重解析时 rebind 又从旧 IR
    沿用了 12 字符的 ID → MySQL 里撞上另一篇的主键：`Duplicate entry '12382beyonds-p01-b01'`。
    """
    from backend.parsing.ir.rebind import id_prefix

    old = [
        BlockFingerprint(id="12382beyonds-p01-b01", page_no=1, x0=10, y0=10, x1=200, y1=30,
                         type="para", text_fp="randomrotationconvertstheworst",
                         content_hash=sha256_text("Random rotation converts the worst.")),
    ]
    new = [
        Block(id="", paper_id="12382-beyond-speedup-utilizing-3", page_no=1,
              order_in_page=1, order_global=1, type="para",
              rect=Rect(10.5, 10.2, 200.4, 30.1),
              text="Random rotation converts the worst.",
              text_clean="Random rotation converts the worst."),
    ]
    new[0].finalize()
    prefix = id_prefix("12382-beyond-speedup-utilizing-3",
                       taken={id_prefix("12382-beyond-speedup-utilizing")})
    assert prefix == "12382beyondspeed"                       # 加长避让
    report = rebind_block_ids(new, old, prefix=prefix)
    assert report.matched == 0                                # 老前缀的旧 ID 一个都不沿用
    assert new[0].id.startswith("12382beyondspeed-p")         # 新块用**本次**前缀编号
    assert not new[0].id.startswith("12382beyonds-p")


def test_orphan_block_id_uses_the_paper_prefix():
    """孤儿块（``-pXX-xNN``）也必须用本篇**实际**前缀，不能现算 12 字符的那个。

    同一个撞车场景的第三处：`_next_block_id` 原来 `id_prefix(paper_id)` 现算，
    于是"加长避让"在这一路上被丢掉，产出别人占用的 ID。
    """
    from backend.parsing.pipeline import _next_block_id

    result = ParseResult(paper_id="12382-beyond-speedup-utilizing-3")
    result.id_prefix = "12382beyondspeed"
    assert _next_block_id(result, 3) == "12382beyondspeed-p03-x01"
    result.id_prefix = ""                                     # 没写前缀时退回老行为
    assert _next_block_id(result, 3) == "12382beyonds-p03-x01"


def test_taken_prefixes_use_the_prefix_actually_in_use():
    """挑前缀时，别的论文要按**库里实际在用的前缀**算，不能现算 12 字符那个。

    同一个撞车场景的**第四处**（2026-09-28 重解析实测）：
    `12382-beyond-speedup-utilizing`（实际前缀 12 字符 `12382beyonds`）与
    `…-3`（当年避让后实际用 16 字符 `12382beyondspeed`）——
    `_other_paper_prefixes` 原来对两篇都现算 `12382beyonds`，于是前一篇重新解析时
    以为 `12382beyondspeed` 没人用、自己也算出它 → `Duplicate entry
    '12382beyondspeed-p01-b01'`，任务失败在第 27% 处。
    """
    from backend.parsing.ir.rebind import id_prefix
    from backend.parsing.pipeline import _taken_prefixes

    pair = ["12382-beyond-speedup-utilizing", "12382-beyond-speedup-utilizing-3"]
    # ① 两篇都解析过：各按自己实际在用的前缀占位
    taken_a = _taken_prefixes(pair, {"12382-beyond-speedup-utilizing-3": "12382beyondspeed"},
                              "12382-beyond-speedup-utilizing")
    assert taken_a == {"12382beyondspeed"}
    assert "12382beyonds" not in taken_a                      # 现算的那个是**错的**
    # 前一篇于是守住自己的 12 字符前缀 → 存量块 ID / 高亮 / 批注都不用改号
    assert id_prefix("12382-beyond-speedup-utilizing", taken=taken_a) == "12382beyonds"

    taken_b = _taken_prefixes(pair, {"12382-beyond-speedup-utilizing": "12382beyonds"},
                              "12382-beyond-speedup-utilizing-3")
    assert taken_b == {"12382beyonds"}
    # 后一篇的加长结果仍然是它自己在用的那个前缀（同样不改号）
    assert id_prefix("12382-beyond-speedup-utilizing-3", taken=taken_b) == "12382beyondspeed"

    # ② 从没解析过（没有块）的论文按现算前缀预留：先解析的那篇占住名字
    assert _taken_prefixes(["new-paper"], {}, "other") == {id_prefix("new-paper")}
    # ③ 自己不算在 taken 里（自己的旧前缀可以继续用）
    assert _taken_prefixes(["me"], {"me": "meprefix"}, "me") == set()
