r"""`paper_search` 的**文本指纹兜底去重**：跨 block 但逐字相同的两条 = 同一处（纯离线）。

用户实测（TurboQuant）：ref 3 与 ref 4 的 `block_get` 文本**逐字相同**（都是
"# E IMPLEMENTATION / The TURBOQUANT algorithm…"），只是一个报 zh 一个报 en，却算成了两处 ——
于是"4 个不同位置"实际只有 3 处。成因是中英两路落到了**两个不同 block**（`block_id` 不同），
现有的按落点去重兜不住。

规则：命中片段文本去掉 markdown 记号（`#` `*` `_` 等）、空白、大小写差异后取指纹，指纹相同
就并入同一处（先到的那条当主条，另一面进 `also_sides`，`deduped` +1）。两条硬约束：

1. **只在同一页**合并：隔了几页的相同文本是论文里重复出现的模板句，不是同一处证据；
2. **少于 12 字**的片段不凭指纹合并：`见图 3` 这种短句满篇都是，会误并成一条。
"""

from __future__ import annotations

import asyncio

from backend.agent import tools as agent_tools

#: 线上那两条的块文本（markdown 里带 `#` 标题记号）
BLOCK = ("# E IMPLEMENTATION\n\nThe TURBOQUANT algorithm rotates the input vector by a "
         "random orthogonal matrix and then quantizes each coordinate independently.")


def _hit(chunk_id: str, *, lang: str, block_id: str, content: str,
         page: int = 5) -> dict:
    return {"chunk_id": chunk_id, "paper_id": "p1", "block_id": block_id,
            "row_ids": [], "page_no": page, "section": "3", "chunk_type": "unit",
            "lang": lang, "content": content, "translation": None}


class FakeHost:
    def __init__(self, items: list[dict]) -> None:
        self.items = list(items)

    async def search(self, paper_id, query, *, top_k, chunk_types=None, mode=None):
        return {"page_total": 22, "items": [dict(one) for one in self.items]}


class FakeDeps:
    def __init__(self, host) -> None:
        from backend.agent.citations import RefRegistry

        self.host = host
        self.paper_id = "p1"
        self.registry = RefRegistry("p1")
        self.query_cache: dict = {}
        self.row_spans: dict = {}
        self.page_total = 0

    def count_search(self) -> None:
        return None

    def note_tool(self, *args, **kwargs) -> None:
        return None


class _Ctx:
    def __init__(self, deps) -> None:
        self.deps = deps


def _call(host, **kwargs):
    return asyncio.run(agent_tools.paper_search(_Ctx(FakeDeps(host)), "quantize", **kwargs))


def test_identical_text_on_two_blocks_counts_once() -> None:
    """两个 block（`p005-03` / `p005-09`）、一 en 一 zh，文本逐字相同 → 一处。"""
    host = FakeHost([_hit("a1", lang="en", block_id="p005-03", content=BLOCK),
                     _hit("b1", lang="zh", block_id="p005-09", content=BLOCK)])
    out = _call(host, top_k=4, mode="rag")
    assert out["returned"] == 1, out
    assert out["deduped"] == 1, "合并了就要说出来，不能静默丢"
    one = out["items"][0]
    assert one["side"] == "en" and one["also_sides"] == ["zh"]
    assert "also_sides" in (out.get("hint") or "")


def test_markdown_markers_and_case_do_not_keep_them_apart() -> None:
    """只差 `#` / `*` 与大小写：仍然是同一处（指纹前先归一化）。"""
    host = FakeHost([_hit("a1", lang="en", block_id="p005-03", content=BLOCK),
                     _hit("b1", lang="zh", block_id="p005-09",
                          content=BLOCK.replace("# E IMPLEMENTATION", "* e implementation *"))])
    assert _call(host, top_k=4)["returned"] == 1


def test_short_snippets_are_never_merged_by_fingerprint() -> None:
    """少于 12 字的片段（"见图 3"）不凭指纹合并 —— 短句满篇都是，并了就少一个位置。"""
    host = FakeHost([_hit("a1", lang="en", block_id="p005-03", content="见图 3"),
                     _hit("b1", lang="zh", block_id="p005-09", content="见图 3")])
    out = _call(host, top_k=4)
    assert out["returned"] == 2 and "deduped" not in out
    assert len({one["ref"] for one in out["items"]}) == 2


def test_same_text_pages_apart_is_two_positions() -> None:
    """隔了几页的相同文本是论文里的模板句，不是同一处证据 → 两处。"""
    host = FakeHost([_hit("a1", lang="en", block_id="p004-03", content=BLOCK, page=4),
                     _hit("b1", lang="zh", block_id="p012-09", content=BLOCK, page=12)])
    out = _call(host, top_k=4)
    assert out["returned"] == 2 and "deduped" not in out
