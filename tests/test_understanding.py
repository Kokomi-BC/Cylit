"""第二阶段「论文理解」单测：两路做不同的事、结果自由 Markdown、引用必须可校验。

用户口径逐条钉住：

- ⑤ 理解提示词**不是**翻译提示词（必须在翻译之后作为第二阶段注入）；
- ⑥ 两路任务**不同**（A 论文精读 + 创新点 / B 阅读地图 + 相关概念）；
- ⑦ 结果是**自由 Markdown**（模型增删合并小节合法，不按 schema 校验）；
- ⑧ **相关概念由中文路（B）产出**并引用自己的短码；
- ④ chunk 是引用基准单位（`来源 Chunk: B023` → 落库解析回真正的 chunk id）。
"""

from __future__ import annotations

import pytest

from backend.services.understanding import prompts, understanding_service as us


# --------------------------------------------------------------------------
# 1. 提示词：两路不同 + 与翻译提示词分开
# --------------------------------------------------------------------------


def test_two_paths_do_different_things() -> None:
    a, b = prompts.UNDERSTAND_A_SYSTEM, prompts.UNDERSTAND_B_SYSTEM
    assert a != b
    assert "论文精读" in a
    assert "阅读地图" in b
    # 相关概念是**中文路专属**（用户 ⑧）
    assert "相关概念" in b
    assert "相关概念" not in a
    # A 路 = 精读框架，且**论文创新点放在这一路**（它要覆盖的方面比 B 少，放得下；
    # 用户看过真实结果后把原"技术解剖"判定为与阅读地图重复，见 prompts.py 顶部）
    for word in ("基本信息", "背景与问题", "动机与 gap", "贡献与创新点", "局限与启发"):
        assert word in a
    for word in ("推荐阅读顺序", "难点", "读之前需要知道的"):
        assert word in b


def test_prompts_are_not_the_translation_prompts() -> None:
    """理解是**第二阶段**：提示词必须与页级翻译/转录完全不同（用户 ⑤）。"""
    from backend.services.translation.page_translate import (
        SYSTEM_PROMPT, TRANSCRIBE_SYSTEM, TRANSLATE_SYSTEM,
    )

    for understanding in (prompts.UNDERSTAND_A_SYSTEM, prompts.UNDERSTAND_B_SYSTEM):
        assert understanding not in (SYSTEM_PROMPT, TRANSCRIBE_SYSTEM, TRANSLATE_SYSTEM)
        # 翻译提示词的核心产物是 original_md/translation_md / breaks，理解不该要这些
        for leaked in ("original_md", "translation_md", '"breaks"'):
            assert leaked not in understanding


def test_both_paths_share_the_reference_contract() -> None:
    for text in (prompts.UNDERSTAND_A_SYSTEM, prompts.UNDERSTAND_B_SYSTEM):
        assert "来源 Chunk" in text
        assert "只许引用清单里出现过的短码" in text
        # 自由 Markdown（用户 ⑦）
        assert "不要套固定模板" in text
        assert "不要按固定 schema 校验" in text or "系统不会按固定 schema 校验" in text


def test_understanding_allows_general_knowledge() -> None:
    """用户选的口径：理解阶段允许用通用知识、**不要求区分来源**。

    与知识库问答（严格只依据来源）是两套规则，所以这里明确"不必区分"。
    """
    for text in (prompts.UNDERSTAND_A_SYSTEM, prompts.UNDERSTAND_B_SYSTEM):
        assert "不必区分" in text


def test_system_for_validates_path() -> None:
    assert prompts.system_for("a") is prompts.UNDERSTAND_A_SYSTEM
    assert prompts.system_for("b") is prompts.UNDERSTAND_B_SYSTEM
    with pytest.raises(ValueError):
        prompts.system_for("c")


# --------------------------------------------------------------------------
# 2. 材料清单与短码
# --------------------------------------------------------------------------


def _inventory() -> list[dict]:
    return [{"code": "B001", "chunk_id": "paper:b:unit:p001-00-p001-02:zh",
             "chunk_type": "unit", "page": 1, "section": "# 摘要",
             "preview": "向量量化这一植根于 Shannon 信源编码的问题"},
            {"code": "B002", "chunk_id": "paper:b:unit:p002-00-p002-01:zh",
             "chunk_type": "unit", "page": 2, "section": "# 1 方法",
             "preview": "两阶段量化器"}]


def test_render_prompt_lists_codes_and_previews() -> None:
    text = prompts.render_understanding_prompt(path="b", title="示例论文",
                                               inventory=_inventory(), brief="领域：量化")
    assert "[B001] unit p1 · # 摘要" in text
    assert "向量量化这一植根于" in text
    assert "领域：量化" in text
    assert text.endswith("```json 代码块给出 refs。")
    # A 路的清单标题与 B 路不同（免得两路 prompt 前缀缓存互相干扰）
    a_text = prompts.render_understanding_prompt(path="a", title="示例论文",
                                                 inventory=_inventory())
    assert "英文材料清单" in a_text and "中文材料清单" in text


def test_preview_is_truncated() -> None:
    long_item = {"code": "A001", "chunk_id": "x", "chunk_type": "unit", "page": 1,
                 "section": "", "preview": "字" * 900}
    text = prompts.render_understanding_prompt(path="a", title="t", inventory=[long_item])
    assert "字" * prompts.PREVIEW_CHARS in text
    assert "字" * (prompts.PREVIEW_CHARS + 1) not in text


def test_code_map_and_path_of_code() -> None:
    m = us.code_map(_inventory())
    assert m["B002"] == "paper:b:unit:p002-00-p002-01:zh"
    assert us.path_of_code("B023") == "b" and us.path_of_code("A001") == "a"


def test_normalize_path() -> None:
    assert us.normalize_path("A") == "a"
    assert us.normalize_path(" b ") == "b"
    assert us.normalize_path(None) == "a"
    with pytest.raises(ValueError):
        us.normalize_path("c")


# --------------------------------------------------------------------------
# 3. 解析：自由 Markdown 必须原样留下
# --------------------------------------------------------------------------


def test_parse_keeps_free_markdown_and_strips_the_json_tail() -> None:
    raw = ("## 我自己起的小节\n正文（来源 Chunk: B023）。\n\n"
           "### 另一个小节\n\n```json\n{\"refs\": [\"B023\"]}\n```")
    got = us.parse_result(raw)
    assert got["markdown"].startswith("## 我自己起的小节")
    assert "```" not in got["markdown"], "收尾的 JSON 块不许留在 Markdown 里"
    assert "另一个小节" in got["markdown"]
    assert got["refs"] == ["B023"]


def test_parse_prefers_explicit_markdown_field() -> None:
    raw = '```json\n{"markdown": "## 直接给的\\n内容", "refs": ["B001"]}\n```'
    got = us.parse_result(raw)
    assert got["markdown"] == "## 直接给的\n内容"
    assert got["refs"] == ["B001"]


def test_parse_falls_back_to_scanning_refs() -> None:
    """模型没给 JSON 收尾时不能丢内容：整段当 Markdown，引用从正文里兜底扫出来。"""
    raw = "结论（来源 Chunk: B007）。另见（来源 Chunk: B007, B009）。"
    got = us.parse_result(raw)
    assert got["markdown"] == raw
    assert got["refs"] == ["B007", "B009"], "要按出现顺序去重"


# --------------------------------------------------------------------------
# 4. 引用校验（用户 ④ 与 ⑧）
# --------------------------------------------------------------------------


def test_validate_refs_resolves_and_flags() -> None:
    valid = us.code_map(_inventory())
    got = us.validate_refs(["B001", "B999", "A001"], valid, path="b")
    assert got["resolved"] == {"B001": "paper:b:unit:p001-00-p001-02:zh"}
    assert got["invalid"] == ["B999"], "清单里没有的短码是幻觉，要标出来"
    assert got["cross_road"] == ["A001"], "B 路引用 A 路短码要单独标（不许跨路）"
    assert got["ok"] is False


def test_validate_refs_ok_when_all_valid() -> None:
    got = us.validate_refs(["B002"], us.code_map(_inventory()), path="b")
    assert got["ok"] is True and got["invalid"] == [] and got["cross_road"] == []


def test_validate_refs_never_raises() -> None:
    """校验失败**不能丢结果**：理解内容仍然有用，只在结果上打标记。"""
    got = us.validate_refs(["ZZZ"], {}, path="a")
    assert got["ok"] is False and got["resolved"] == {}
    assert got["invalid"] == ["ZZZ"]


def test_concept_section_must_cite_chunks() -> None:
    assert us.concept_sections_missing_refs("## 相关概念\n只讲概念不引用") is True
    assert us.concept_sections_missing_refs(
        "## 相关概念\n**MSE**（来源 Chunk: B003）") is False
    # 没有这一节也算缺（B 路提示词点名必须有）
    assert us.concept_sections_missing_refs("## 结论\n没有那一节") is True


def test_a_path_skips_the_concept_check() -> None:
    """相关概念是 B 路的硬要求；A 路没有这一节是正常的。"""
    from backend.services.understanding.understanding_service import (
        concept_sections_missing_refs,
    )

    assert concept_sections_missing_refs("# 技术解剖\n没有相关概念一节") is True
    # 调用侧按路跳过（见 `build_understanding`：path == "b" 才查）
    assert us.LANGS_OF_PATH["a"] == ("en", "any")
    assert us.LANGS_OF_PATH["b"] == ("zh", "any")
