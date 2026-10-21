"""同一条证据只能有一个**引用号**。

A 路英文 unit 与 B 路中文 unit 是同一次检索命中的两份投影：页码、block_id、给用户看的
片段（中文译文）三者完全一样，只有 chunk id 不同。按 chunk id 去重就会发出两个号 ——
正文里出现两枚标号、两张一模一样的速览卡（用户 2026-12-04 报的 12 / 13）。
所以去重键按「落点 + 片段」，不按 chunk id。
"""

from __future__ import annotations

from backend.agent.citations import RefRegistry
from backend.agent.tools import _chunk_key

SNIP = "**显式常数与渐近最优性。** Gao et al. (2024) 的近期工作提出了一种基于网格的量化方法"


def test_same_row_two_roads_share_one_key() -> None:
    a = _chunk_key("paper", "p015-06", "paper:a:unit:p015-06-p015-06:en", SNIP)
    b = _chunk_key("paper", "p015-06", "paper:b:unit:p015-06-p015-06:zh", SNIP)
    assert a == b, "同一行两路的命中必须是同一个引用号"


def test_different_rows_snippets_or_papers_stay_apart() -> None:
    base = _chunk_key("paper", "p015-06", "c1", SNIP)
    assert base != _chunk_key("paper", "p015-07", "c1", SNIP), "不同行不能合并"
    assert base != _chunk_key("paper", "p015-06", "c1", SNIP + "（另一条公式）"), \
        "同一行里两条不同证据（片段不同）必须分开"
    assert base != _chunk_key("other-paper", "p015-06", "c1", SNIP), "跨论文的同一 block_id 不能撞车"


def test_reuse_registers_every_internal_id() -> None:
    reg = RefRegistry("paper")
    first = reg.assign(key=_chunk_key("paper", "p015-06", "chunk-en", SNIP), kind="chunk",
                       page=15, block_id="p015-06", ids=("chunk-en",))
    again = reg.assign(key=_chunk_key("paper", "p015-06", "chunk-zh", SNIP), kind="chunk",
                       page=15, block_id="p015-06", ids=("chunk-zh",))
    assert again.ref == first.ref == 1
    assert len(reg) == 1, "同一处证据只占一个号"
    assert reg.ref_of_id("chunk-en") == 1 and reg.ref_of_id("chunk-zh") == 1, \
        "两个 chunk id 都要能被清洗阶段认出来"
