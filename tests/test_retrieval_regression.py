"""固定检索回归集（**需要 MySQL + 已建索引**，不可用就跳过）。

这是"chunk 分布大变之后，RAG 到底有没有更准"的自动化底线：跑固定 10 问，要求
- 期望小节/正文特征的命中率 ≥ 阈值（默认 70%）；
- **附录题必须能命中附录内容**（改造前 turboquant 附录整段不在库里，这几题必然 0）；
- 公式题（`want_formula`）必须至少有一条命中里出现 formula atomic chunk。

详细的逐题报告（rank / section / formula / appendix）由
`python -m tests.retrieval_regression.run --out <path>` 出，不进断言。
"""

from __future__ import annotations

import pytest

from backend.repositories.mysql.session import healthcheck
from tests.retrieval_regression import run as regression

pytestmark = pytest.mark.e2e

HIT_BAR = 0.7


@pytest.fixture(scope="module")
def report():
    if not healthcheck().get("ok"):
        pytest.skip("MySQL 不可用（设置 PR_DB_BACKEND=sqlite 或启动 MySQL）")
    spec = regression.load_spec()
    from backend.repositories.mysql.models import Chunk
    from backend.repositories.mysql.session import session_scope

    with session_scope() as sess:
        indexed = (sess.query(Chunk)
                   .filter(Chunk.paper_id == spec["paper_id"]).first() is not None)
    if not indexed:
        pytest.skip(f"论文还没建索引：{spec['paper_id']}")
    from backend.repositories.vector.lancedb_store import vector_store

    if not vector_store.available:
        pytest.skip("向量库不可用")
    return regression.evaluate(spec)


def test_expected_sections_are_hit(report) -> None:
    assert report["hit_at_k"] >= HIT_BAR, (
        f"hit@{report['top_k']} 只有 {report['hit_at_k']:.0%}，低于 {HIT_BAR:.0%}\n"
        + regression.to_markdown(report))


def test_appendix_questions_hit_appendix(report) -> None:
    """附录题必须检得到附录 —— 这是本轮 parser 修复的直接验收项。"""
    rows = [r for r in report["rows"] if r["want_appendix"]]
    assert rows, "题库里没有附录题？"
    missed = [r["q"] for r in rows if not r["appendix"]]
    assert not missed, f"这些附录题没命中 appendix chunk：{missed}\n" + regression.to_markdown(report)


def test_formula_questions_hit_formula_atomic(report) -> None:
    """公式题在**公式专项检索**（只看 formula chunk）下必须命中期望公式。

    不在混合检索里要求它一定进 top-k：公式原子块正文只有 LaTeX，跟整段正文拼相似度天然吃亏；
    它存在的理由是"专门找公式"这条路径，所以按那条路径量（`formula_hit_rate`）。
    """
    rows = [r for r in report["rows"] if r["want_formula"]]
    assert rows, "题库里没有公式题？"
    missed = [r["q"] for r in rows if not r["formula_first_match_rank"]]
    assert not missed, f"这些公式题在公式专项检索里没命中：{missed}\n" + regression.to_markdown(report)


def test_semantic_chunks_keep_formula_context(report) -> None:
    """公式应当**同时**存在于语义 chunk 里（不只有孤立的公式原子块）。"""
    assert report["formula_context_coverage"] >= 0.5, (
        "语义 chunk 带内联公式的比例过低\n" + regression.to_markdown(report))
