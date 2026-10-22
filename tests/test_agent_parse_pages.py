r"""工具层的页码解析：模型怎么写都该认，认不出要报**对**的原因。

线上实测 2026-12-07：模型要在第 4、7 页读正文，写法是 ``"4,7"`` —— 老解析器只认单页或
单区间，直接回「页码格式不对」，thinking 里能看到它原地重发一次；这一点白跑。
另一种更误导：``"99"`` 而全文只有 22 页，也回「页码格式不对」—— 模型以为写法错了，
换个写法再试，错还在。所以「写法不认识」和「超出页码范围」必须是两句话。
"""
from __future__ import annotations

import pytest

from backend.agent.tools import pages_error, parse_pages


@pytest.mark.parametrize("spec", ["5", "p5", "P5", "page 5", "pages 5", "pp. 5", "第 5 页"])
def test_single_page_in_every_shape_the_model_writes(spec: str) -> None:
    assert parse_pages(spec, total=22, limit=20) == ([5], False)


@pytest.mark.parametrize("spec", ["4,7", "4, 7", "4、7", "4 7", "4 and 7", "4和7", "7,4"])
def test_page_lists(spec: str) -> None:
    assert sorted(parse_pages(spec, total=22, limit=20)[0]) == [4, 7]


@pytest.mark.parametrize("spec", ["3-8", "3 – 8", "3~8", "3 到 8", "3 to 8",
                                  "pages 3-8", "第 3-8 页", "pp. 3 - 8"])
def test_ranges(spec: str) -> None:
    assert parse_pages(spec, total=22, limit=20) == (list(range(3, 9)), False)


def test_mixed_list_and_range() -> None:
    assert parse_pages("1, 3-5, 9", total=22, limit=20) == ([1, 3, 4, 5, 9], False)


@pytest.mark.parametrize("spec", ["all", "all pages", "全部", "全文", "*"])
def test_all(spec: str) -> None:
    pages, clipped = parse_pages(spec, total=22, limit=50)
    assert pages == list(range(1, 23)) and not clipped


def test_limit_reports_truncation() -> None:
    pages, clipped = parse_pages("1-30", total=40, limit=20)
    assert pages == list(range(1, 21)) and clipped is True


def test_duplicates_collapse_but_order_is_kept() -> None:
    assert parse_pages("5,5,3", total=22, limit=20) == ([5, 3], False)


def test_out_of_range_says_so_instead_of_blaming_the_format() -> None:
    assert parse_pages("99", total=22, limit=20) == ([], False)
    msg = pages_error("99", total=22)
    assert "第 99 页" in msg and "22" in msg
    assert "没读懂" not in msg


def test_unparsable_spec_says_the_format_was_not_understood() -> None:
    assert parse_pages("随便", total=22, limit=20) == ([], False)
    msg = pages_error("随便", total=22)
    assert "没读懂" in msg and "4,7" in msg


def test_unknown_total_never_claims_the_page_is_missing() -> None:
    """页数还没拿到（total=0）时不能断言「没有第 N 页」。"""
    assert "不存在" not in pages_error("all", total=0)
    assert "没有" not in pages_error("all", total=0)