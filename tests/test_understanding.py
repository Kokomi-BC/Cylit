"""第二阶段「论文理解」单测：论文精读、结果自由 Markdown、引用必须可校验。

用户口径逐条钉住：

- ⑤ 理解提示词**不是**翻译提示词（必须在翻译之后作为第二阶段注入）；
- ⑥ 理解做的是**论文精读**（含**贡献与创新点**）；2026-10-02 起
  「阅读地图 + 相关概念」（原 B 路）与它的提示词/接口/短码一起删除；
- ⑦ 结果是**自由 Markdown**（模型增删合并小节合法，不按 schema 校验）；
- ④ chunk 是引用基准单位（`来源 Chunk: A023` → 落库解析回真正的 chunk id）。
"""

from __future__ import annotations

import pytest

from backend.services.understanding import prompts, understanding_service as us


# --------------------------------------------------------------------------
# 1. 提示词：只有论文精读一路，且与翻译提示词分开
# --------------------------------------------------------------------------


def test_prompt_is_the_close_reading_framework() -> None:
    a = prompts.UNDERSTAND_A_SYSTEM
    assert "论文精读" in a
    # 论文创新点放在这一路（原"技术解剖"被判定与阅读地图重复后换掉，见 prompts.py 顶部）
    for word in ("基本信息", "背景与问题", "动机与 gap", "贡献与创新点", "局限与启发"):
        assert word in a
    # 「阅读地图 / 相关概念」整条删除：提示词里不许再出现
    assert "阅读地图" not in a
    assert "相关概念" not in a


def test_prompts_are_not_the_translation_prompts() -> None:
    """理解是**第二阶段**：提示词必须与页级翻译/转录完全不同（用户 ⑤）。"""
    from backend.services.translation.page_translate import (
        SYSTEM_PROMPT, TRANSCRIBE_SYSTEM, TRANSLATE_SYSTEM,
    )

    assert prompts.UNDERSTAND_A_SYSTEM not in (SYSTEM_PROMPT, TRANSCRIBE_SYSTEM, TRANSLATE_SYSTEM)
    # 翻译提示词的核心产物是 original_md/translation_md / breaks，理解不该要这些
    for leaked in ("original_md", "translation_md", '"breaks"'):
        assert leaked not in prompts.UNDERSTAND_A_SYSTEM


def test_prompt_shares_the_reference_contract() -> None:
    """引用标记只有编号本身（用户 2026-11-28："避免出现 chunk 这个文字"）。"""
    text = prompts.UNDERSTAND_A_SYSTEM
    assert "[A023]" in text, "示例必须就是最终形态：句末紧跟方括号编号"
    assert "只许用清单里出现过的编号" in text
    assert "chunk" not in text.lower(), "提示词自己不许再出现这个词，模型会照着抄"
    assert "来源 Chunk" not in text
    # 自由 Markdown（用户 ⑦）
    assert "不要套固定模板" in text
    assert "不要按固定 schema 校验" in text or "系统不会按固定 schema 校验" in text


def test_understanding_allows_general_knowledge() -> None:
    """用户选的口径：理解阶段允许用通用知识、**不要求区分来源**。

    与知识库问答（严格只依据来源）是两套规则，所以这里明确"不必区分"。
    """
    assert "不必区分" in prompts.UNDERSTAND_A_SYSTEM


def test_system_for_accepts_only_a() -> None:
    assert prompts.system_for("a") is prompts.UNDERSTAND_A_SYSTEM
    assert prompts.system_for() is prompts.UNDERSTAND_A_SYSTEM
    for bad in ("b", "c"):
        with pytest.raises(ValueError):
            prompts.system_for(bad)


# --------------------------------------------------------------------------
# 2. 材料清单与短码
# --------------------------------------------------------------------------


def _inventory() -> list[dict]:
    return [{"code": "A001", "chunk_id": "paper:a:unit:p001-00-p001-02:en",
             "chunk_type": "unit", "page": 1, "section": "# Abstract",
             "preview": "Vector quantization is a classic source coding problem"},
            {"code": "A002", "chunk_id": "paper:a:unit:p002-00-p002-01:en",
             "chunk_type": "unit", "page": 2, "section": "# 1 Method",
             "preview": "two-stage quantizer"}]


def test_render_prompt_lists_codes_and_previews() -> None:
    text = prompts.render_understanding_prompt(path="a", title="示例论文",
                                               inventory=_inventory(), brief="领域：量化")
    assert "[A001] unit p1 · # Abstract" in text
    assert "Vector quantization is a classic" in text
    assert "领域：量化" in text
    assert text.endswith("```json 代码块给出 refs。")
    assert "英文材料清单" in text


def test_preview_is_truncated() -> None:
    long_item = {"code": "A001", "chunk_id": "x", "chunk_type": "unit", "page": 1,
                 "section": "", "preview": "字" * 900}
    text = prompts.render_understanding_prompt(path="a", title="t", inventory=[long_item])
    assert "字" * prompts.PREVIEW_CHARS in text
    assert "字" * (prompts.PREVIEW_CHARS + 1) not in text


def test_code_map() -> None:
    m = us.code_map(_inventory())
    assert m["A002"] == "paper:a:unit:p002-00-p002-01:en"


def test_normalize_path_rejects_b() -> None:
    assert us.normalize_path("A") == "a"
    assert us.normalize_path(None) == "a"
    for bad in ("b", "c"):
        with pytest.raises(ValueError):
            us.normalize_path(bad)


def test_only_one_path_left() -> None:
    """「阅读地图」删除后：哪一路看哪些 chunk / 什么语言，都只剩 a。"""
    assert set(us.ROAD_OF_PATH) == {"a"}
    assert set(us.LANGS_OF_PATH) == {"a"}
    assert us.LANGS_OF_PATH["a"] == ("en", "any")
    assert set(us.PATH_TASK_TYPES) == {"a"}


# --------------------------------------------------------------------------
# 3. 解析：自由 Markdown 必须原样留下
# --------------------------------------------------------------------------


def test_parse_keeps_free_markdown_and_strips_the_json_tail() -> None:
    raw = ("## 我自己起的小节\n正文（来源 Chunk: A023）。\n\n"
           "### 另一个小节\n\n```json\n{\"refs\": [\"A023\"]}\n```")
    got = us.parse_result(raw)
    assert got["markdown"].startswith("## 我自己起的小节")
    assert "```" not in got["markdown"], "收尾的 JSON 块不许留在 Markdown 里"
    assert "另一个小节" in got["markdown"]
    assert got["refs"] == ["A023"]


def test_parse_prefers_explicit_markdown_field() -> None:
    raw = '```json\n{"markdown": "## 直接给的\\n内容", "refs": ["A001"]}\n```'
    got = us.parse_result(raw)
    assert got["markdown"] == "## 直接给的\n内容"
    assert got["refs"] == ["A001"]


def test_parse_falls_back_to_scanning_refs() -> None:
    """模型没给 JSON 收尾时不能丢内容：整段当 Markdown，引用从正文里兜底扫出来。"""
    raw = "结论（来源 Chunk: A007）。另见（来源 Chunk: A007, A009）。"
    got = us.parse_result(raw)
    assert got["markdown"] == raw
    assert got["refs"] == ["A007", "A009"], "要按出现顺序去重"


# --------------------------------------------------------------------------
# 4. 引用校验（用户 ④）
# --------------------------------------------------------------------------


def test_validate_refs_resolves_and_flags() -> None:
    valid = us.code_map(_inventory())
    got = us.validate_refs(["A001", "A999", "B023"], valid, path="a")
    assert got["resolved"] == {"A001": "paper:a:unit:p001-00-p001-02:en"}
    assert got["invalid"] == ["A999"], "清单里没有的短码是幻觉，要标出来"
    # 老库里存着 B 路生成的解读，模型偶尔照抄 B 短码：单独归"跨路"，读时才说得清是哪种错
    assert got["cross_road"] == ["B023"]
    assert got["ok"] is False


def test_validate_refs_ok_when_all_valid() -> None:
    got = us.validate_refs(["A002"], us.code_map(_inventory()))
    assert got["ok"] is True and got["invalid"] == [] and got["cross_road"] == []


def test_validate_refs_never_raises() -> None:
    """校验失败**不能丢结果**：理解内容仍然有用，只在结果上打标记。"""
    got = us.validate_refs(["ZZZ"], {}, path="a")
    assert got["ok"] is False and got["resolved"] == {}
    assert got["invalid"] == ["ZZZ"]


def test_refresh_image_urls_recomputes_the_region_version(monkeypatch) -> None:
    """解读里存的 `?v=` 是快照：读的时候要按**当前**区域重算，否则浏览器 7 天都看旧图。"""
    from backend.services.understanding import understanding_service as us

    class _FR:
        @staticmethod
        def page_regions(paper_id, page):
            assert paper_id == 'p1' and page == 9
            return [{'hash': 'newhash1'}, {'hash': 'newhash2'}]

        @staticmethod
        def region_version(reg, paper_id):
            return reg['hash'] + 'c0'

    monkeypatch.setattr('backend.services.paper.figure_regions', _FR, raising=False)
    monkeypatch.setitem(__import__('sys').modules, 'backend.services.paper.figure_regions', _FR)
    inventory = [
        {'code': 'A067', 'image': '/api/v1/papers/p1/figures/region/9/0?v=oldhashc0'},
        {'code': 'A068', 'image': '/api/v1/papers/p1/figures/region/9/1?v=oldhashc0'},
        {'code': 'A069', 'image': '/api/v1/papers/p1/figures/region/9/7?v=oldhashc0'},  # 越界
        {'code': 'A070', 'image': '/api/v1/papers/p2/figures/region/9/0?v=other'},       # 别的论文
        {'code': 'A071'},                                                             # 没有图
    ]
    us._refresh_image_urls('p1', inventory)
    assert inventory[0]['image'] == '/api/v1/papers/p1/figures/region/9/0?v=newhash1c0'
    assert inventory[1]['image'] == '/api/v1/papers/p1/figures/region/9/1?v=newhash2c0'
    assert inventory[2]['image'].endswith('?v=oldhashc0')      # 越界 → 不动
    assert inventory[3]['image'].endswith('?v=other')          # 别的论文 → 不动
    assert 'image' not in inventory[4]
