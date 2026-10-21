"""重新翻译**不影响**已有解读：引用过期只标记、不清空。

用户口径：要留出"只重新翻译"的接口（不带后面的解读），而且**重新翻译不能把已有的解读打回原形**。

重新翻译 + 重建索引后，chunk id 会变（`{paper}:{A|B}:{type}:{首块}-{末块}` 里的块号变了），
于是解读里存的 `refs_resolved` 会指向不存在的 chunk。**正确做法是保留**：
解读内容（概念解释、阅读顺序、方法链条）与 chunk id 无关，仍然有效；
过期的引用还留着**页码**，前端照样能跳到那一页，只是标一句"引用已过期"。
"""

from __future__ import annotations

from backend.services.understanding import understanding_service as us


def _by_code() -> dict:
    return {"B001": {"chunk_type": "unit", "page": 3},
            "B002": {"chunk_type": "formula", "page": 7}}


def test_alive_refs_stay_in_refs_resolved() -> None:
    live, stale = us.split_live_refs(
        {"B001": "paper:b:unit:p003-01:zh", "B002": "paper:b:formula:f007:any"},
        _by_code(), exists=lambda ids: set(ids))
    assert [r["code"] for r in live] == ["B001", "B002"]
    assert stale == []
    assert live[0] == {"code": "B001", "chunk_id": "paper:b:unit:p003-01:zh",
                       "chunk_type": "unit", "page": 3}


def test_stale_refs_are_kept_with_page_not_dropped() -> None:
    """重新翻译后 chunk id 变了 → 进 stale，但**页码还在**（照样能跳），内容不丢。"""
    live, stale = us.split_live_refs(
        {"B001": "旧:chunk:id", "B002": "paper:b:formula:f007:any"},
        _by_code(), exists=lambda ids: {"paper:b:formula:f007:any"} & set(ids))
    assert [r["code"] for r in live] == ["B002"]
    assert [r["code"] for r in stale] == ["B001"]
    assert stale[0]["page"] == 3, "过期引用必须保留页码，前端才能跳到那一页并标注"
    assert stale[0]["chunk_type"] == "unit"


def test_live_refs_carry_block_and_row_id_for_jumping() -> None:
    """有效引用同时带**块 id**与**正文行 id**：前端点标号要落到原文那一段。

    row_id 是从 chunk 元数据里取的（page_row / formula_block 两种锚定都写它）：
    公式 chunk 的 block_id 是 row-<行 id> 甚至 …-x01 这种伪块 id，阅读器翻不成行，
    只靠 block_id 点标号就只剩整页闪（2026-12-03 用户报的那两条）。
    """
    live, _ = us.split_live_refs(
        {"B001": "paper:b:unit:p003-01:zh"}, _by_code(),
        exists=lambda ids: set(ids),
        anchors={"paper:b:unit:p003-01:zh": {"block_id": "paper-p03-b004",
                                            "row_id": "p003-04"}})
    assert live[0]["block_id"] == "paper-p03-b004"
    assert live[0]["row_id"] == "p003-04"
    assert live[0]["page"] == 3


def test_live_ref_survives_without_anchor_keys() -> None:
    """锚点表里只给了一个键（老数据没有 block_id / 没有 row_id）也不能丢引用。"""
    live, _ = us.split_live_refs(
        {"B001": "paper:b:unit:p003-01:zh"}, _by_code(),
        exists=lambda ids: set(ids),
        anchors={"paper:b:unit:p003-01:zh": {"block_id": None, "row_id": "p003-04"}})
    assert live[0]["row_id"] == "p003-04" and "block_id" not in live[0]


def test_stale_ref_has_no_anchor() -> None:
    live, stale = us.split_live_refs(
        {"B001": "旧:chunk:id"}, _by_code(), exists=lambda ids: set(),
        anchors={})
    assert live == [] and "block_id" not in stale[0] and "row_id" not in stale[0]


def test_no_refs_is_not_an_error() -> None:
    assert us.split_live_refs({}, {}, exists=lambda ids: set()) == ([], [])


def test_all_stale_keeps_every_ref() -> None:
    resolved = {"B001": "x", "B002": "y"}
    live, stale = us.split_live_refs(resolved, _by_code(), exists=lambda ids: set())
    assert live == [] and len(stale) == 2, "全部过期也要全部保留（解读内容还在）"

# ---------------------------------------------------------------------------
# 标号在正文里出现、但收尾 JSON 的 refs 没登记它（用户 2026-12-04 的标号 16）
# ---------------------------------------------------------------------------


def _row(markdown: str, resolved: dict, inventory: list) -> "us.Understanding":
    return us.Understanding(
        id="p:a", paper_id="p", path="a", markdown=markdown,
        refs_json=us.json_dumps({"codes": list(resolved), "resolved": resolved,
                                 "invalid": [], "cross_road": [], "ok": True}),
        inventory=us.json_dumps(inventory), refs_ok=True, model="m",
        prompt_tokens=0, completion_tokens=0, updated_at=None,
    )


def test_code_cited_only_in_markdown_still_resolves(monkeypatch) -> None:
    """正文写了 [A002]、refs_json 里却没有 A002 —— 读的时候按清单补齐，标号照样能跳。"""
    inventory = [{"code": "A001", "chunk_id": "cid1", "chunk_type": "unit", "page": 3},
                 {"code": "A002", "chunk_id": "cid2", "chunk_type": "unit", "page": 15}]
    monkeypatch.setattr(us, "_chunk_anchors",
                        lambda ids: {"cid1": {"block_id": "b1", "row_id": "r1"},
                                     "cid2": {"block_id": "b2", "row_id": "r2"}})
    got = us.to_dict(_row("见 [A001] 与 [A002]。", {"A001": "cid1"}, inventory))
    assert [r["code"] for r in got["refs_resolved"]] == ["A001", "A002"]
    assert got["refs_resolved"][1]["chunk_id"] == "cid2"
    assert got["refs_resolved"][1]["row_id"] == "r2"


def test_markdown_code_missing_from_inventory_stays_unresolved(monkeypatch) -> None:
    """清单里没有的短码（模型抄错/跨路）不能凭空补一条，免得前端跳到一个不存在的位置。"""
    monkeypatch.setattr(us, "_chunk_anchors", lambda ids: {"cid1": {"row_id": "r1"}})
    got = us.to_dict(_row("见 [A001] 与 [A999]。", {"A001": "cid1"}, [
        {"code": "A001", "chunk_id": "cid1", "chunk_type": "unit", "page": 3}]))
    assert [r["code"] for r in got["refs_resolved"]] == ["A001"]


def test_merge_ref_codes_keeps_order_and_dedupes() -> None:
    assert us.merge_ref_codes(["A002", "A001"], "正文 [A001]，[A002]，再来一次 [A001]") \
        == ["A002", "A001"]
    assert us.merge_ref_codes(["A001"], "正文 [A001] 与 [A004]") == ["A001", "A004"]

