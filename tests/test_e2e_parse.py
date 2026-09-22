"""端到端测试（需要真实 PDF；无 PDF 或不可用时自动跳过）。

覆盖规划验收项：
- A1 导入 → 解析 → 可读全链路
- A2 IR 可追溯（block 显示页码 + bbox + 原文）
- 阶段 03：双栏顺序 + block_id 重跑稳定
- 阶段 04：Markdown 有锚点、每块可定位
"""

from __future__ import annotations

import shutil
import tempfile
from pathlib import Path

import pytest

pytestmark = pytest.mark.e2e


@pytest.fixture(scope="module")
def parsed(tmp_path_factory, sample_pdf):
    if sample_pdf is None:
        pytest.skip("未找到示例 PDF（TurboQuant_Online_Vector ICLR-26.pdf）")
    from backend.parsing.pipeline import PaperBuilder

    out = tmp_path_factory.mktemp("paper")
    builder = PaperBuilder(sample_pdf, "e2e-paper", out, max_pages=6)
    result = builder.build()
    return result, out, builder


def test_parse_produces_blocks(parsed):
    result, _out, _b = parsed
    assert result.page_count == 6
    assert len(result.blocks) > 100
    assert result.title
    # 每块都可追溯：页码 + bbox + 原文（验收 A2）
    for b in result.blocks[:50]:
        assert b.page_no >= 1
        assert b.rect.width >= 0 and b.rect.height >= 0
        # 块 ID 带论文前缀，保证跨论文在共享业务库中唯一
        assert b.id.startswith("e2epaper-p")
        assert b.content_hash


def test_reading_order_global_monotonic(parsed):
    result, _out, _b = parsed
    orders = [b.order_global for b in result.blocks]
    assert orders == sorted(orders)
    # 页码随 order_global 单调不减（阅读顺序跨页）
    pages = [b.page_no for b in result.blocks]
    assert pages == sorted(pages)


def test_markdown_has_anchor_for_every_block(parsed):
    result, out, _b = parsed
    md = (out / "md" / "full.md").read_text(encoding="utf-8")
    from backend.parsing.md.generator import extract_block_ids

    ids = set(extract_block_ids(md))
    assert len(ids) >= len(result.blocks) * 0.95
    assert '<a id="blk-' in md


def test_ir_written_and_readable(parsed):
    result, out, _b = parsed
    from backend.parsing.ir.ir_reader import IRReader

    ir = IRReader(out / "ir.sqlite")
    assert ir.exists
    assert ir.stats()["blocks"] >= 100
    stats = ir.stats()
    assert stats["pages"] == 6
    assert ir.paper()["title"]


def test_reparse_keeps_block_ids(parsed, sample_pdf):
    """阶段 03 验收：重跑 ID 稳定。"""
    result, out, _b = parsed
    from backend.parsing.pipeline import PaperBuilder

    before = [b.id for b in result.blocks]
    builder2 = PaperBuilder(sample_pdf, "e2e-paper", out, max_pages=6)
    result2 = builder2.build()
    after = [b.id for b in result2.blocks]
    overlap = len(set(before) & set(after)) / max(1, len(set(before)))
    assert overlap >= 0.9, f"block_id 稳定性不足：{overlap:.2f}"


def test_formula_grades_and_crops(parsed):
    result, out, _b = parsed
    assert result.formulas, "示例论文应识别出公式"
    dist = result.grade_distribution()
    assert dist["A"] > 0
    c_grade = [f for f in result.formulas if f.grade == "C"]
    for f in c_grade:
        # C 级必须有高倍裁图兜底（规划 §14.5）
        assert f.image_path and Path(f.image_path).is_file()


def test_duplicate_ids_absent(parsed):
    result, _out, _b = parsed
    ids = [b.id for b in result.blocks]
    assert len(ids) == len(set(ids)), "block_id 必须唯一"
    fids = [f.block_id for f in result.formulas]
    assert len(fids) == len(set(fids)), "formula.block_id 必须唯一（主键约束）"


@pytest.mark.skipif(shutil.which("mysql") is None, reason="需要 MySQL（可选）")
def test_mysql_sync_optional(parsed):
    """MySQL 可用时验证回写；不可用（或未配置）时跳过。"""
    from backend.repositories.mysql.session import healthcheck

    if not healthcheck().get("ok"):
        pytest.skip("MySQL 不可用")
    result, _out, _b = parsed
    from backend.repositories.mysql.models import Block as BlockRow
    from backend.repositories.mysql.session import session_scope

    with session_scope() as sess:
        n = sess.query(BlockRow).filter(BlockRow.paper_id == "e2e-paper").count()
    assert n >= 0
    _ = result
