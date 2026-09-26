"""理解结果的**读时投影**：从自由 Markdown 里解析概念 + 按页码找相关概念。

为什么要这一层：理解结果是**自由 Markdown**（用户口径⑦，模型可增删合并小节），所以"概念"是
**投影**而不是事实源。读时投影的好处是**不用改表、不用重新生成** —— 已经存好的解读立刻就能
出概念列表与"跟随阅读位置"，前端不必等一轮重跑。
"""

from __future__ import annotations

from backend.services.understanding import understanding_service as us

MD = """# 论文阅读地图

## 一句话概括
两阶段量化器。

## 相关概念（先读这一节）

### 1. 向量量化 与位宽（bit-width）
它是什么（来源 Chunk: B008）。为什么需要（来源 Chunk: B009）。

### 2. QJL：1 比特内积量化
无偏内积估计（来源 Chunk: B096）。另见 B098。

### 3. 不存在的引用（来源 Chunk: B999）
这条的短码不在清单里，应当被丢掉而不是报错。

## 推荐阅读顺序
先说结论。
"""

BY_CODE = {
    "B008": {"code": "B008", "chunk_id": "p:b:unit:p002-01:zh", "chunk_type": "unit", "page": 2},
    "B009": {"code": "B009", "chunk_id": "p:b:unit:p002-02:zh", "chunk_type": "unit", "page": 2},
    "B096": {"code": "B096", "chunk_id": "p:b:unit:p017-01:zh", "chunk_type": "unit", "page": 17},
    "B098": {"code": "B098", "chunk_id": "p:b:formula:f017:any", "chunk_type": "formula", "page": 17},
}


def test_parses_only_the_concepts_section() -> None:
    concepts = us.parse_concepts(MD, BY_CODE)
    assert [c["name"].startswith(("1.", "2.", "3.")) for c in concepts] == [True] * 3
    # 「一句话概括」「推荐阅读顺序」不属于概念区
    names = " ".join(c["name"] for c in concepts)
    assert "一句话概括" not in names and "推荐阅读顺序" not in names


def test_splits_chinese_and_english_names() -> None:
    concepts = us.parse_concepts(MD, BY_CODE)
    assert concepts[0]["name_en"] == "bit-width"
    assert "向量量化" in concepts[0]["name"]


def test_resolves_refs_and_drops_unknown_codes() -> None:
    concepts = us.parse_concepts(MD, BY_CODE)
    qjl = next(c for c in concepts if "QJL" in c["name"])
    assert [r["code"] for r in qjl["refs"]] == ["B096", "B098"]
    assert qjl["refs"][1]["page"] == 17 and qjl["refs"][1]["chunk_type"] == "formula"
    missing = next(c for c in concepts if "不存在" in c["name"])
    assert missing["refs"] == [], "清单里没有的短码要丢掉，不能编出一条假引用"


def test_no_concepts_section_returns_empty() -> None:
    assert us.parse_concepts("# 阅读地图\n\n## 概括\n没有概念那一节。", BY_CODE) == []
    assert us.parse_concepts("", BY_CODE) == []


def test_related_concepts_matches_within_tolerance(monkeypatch) -> None:
    """引用常跨页，所以按 ±tolerance 命中（完全等值会漏掉"就在这一页"的概念）。"""
    monkeypatch.setattr(us, "get_understanding",
                        lambda paper_id, path=None: {"concepts": us.parse_concepts(MD, BY_CODE)})
    assert [c["name"][:1] for c in us.related_concepts("p", 2)["concepts"]] == ["1"]
    assert [c["name"][:1] for c in us.related_concepts("p", 17)["concepts"]] == ["2"]
    assert us.related_concepts("p", 17)["total_concepts"] == 3
    # 容差可调：第 3 页对 B009（page 2）在 tol=1 时命中（|3-2|=1），tol=0 时不命中
    assert us.related_concepts("p", 3, tolerance=0)["concepts"] == []
    assert [c["name"][:1] for c in us.related_concepts("p", 3, tolerance=1)["concepts"]] == ["1"]


def test_related_concepts_on_missing_understanding_is_empty(monkeypatch) -> None:
    """没有解读结果时返回空列表（前端静默退回全篇列表，不报错）。"""
    monkeypatch.setattr(us, "get_understanding", lambda paper_id, path=None: {})
    got = us.related_concepts("p", 3)
    assert got["concepts"] == [] and got["total_concepts"] == 0
