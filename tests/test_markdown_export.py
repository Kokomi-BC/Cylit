"""下载 / 打包导出的三个 Markdown 版本（纯函数，不依赖数据库与 PDF）。

用户口径（2026-09-28）：
1. 「markdown 视图下载 markdown」要去掉**页标识**（``<!-- page N -->``）与 ``## 第 N 页``；
2. 「下载打包」要给出**所有版本**的 Markdown（原文 / 译文 / 中英对照）+ 原 PDF + 被引用的插图。

这里锁住第 1 条的文本规则与三个版本的拼装形状；zip 的那一份在
``reader_service.export_bundle`` 里复用同一个 ``assemble_versions``，所以规则只有一处。
"""

from __future__ import annotations

from backend.services.paper import reader_service
from backend.services.paper.reader_service import (
    MD_EXPORT_MODES, assemble_versions,
)

PAGES = [
    {"page": 1, "original_md": "# 1 INTRO\n\nHello.\n\n![Figure 1](figures/a.png)",
     "translation_md": "# 1 引言\n\n你好。\n\n![图 1](figures/a.png)"},
    {"page": 2, "original_md": "## 2 METHOD\n\nWorld.",
     "translation_md": "## 2 方法\n\n世界。"},
    # 空页不该产出多余的分隔线
    {"page": 3, "original_md": "", "translation_md": ""},
]


def test_versions_have_no_page_marks():
    """三个版本里都不能出现页标识（`<!-- page N -->`）与 `第 N 页` 标题。"""
    v = assemble_versions(PAGES)
    assert set(v) == set(MD_EXPORT_MODES)
    for mode, text in v.items():
        assert "<!-- page" not in text, mode
        assert "第 1 页" not in text and "第 2 页" not in text, mode
        assert "## 第" not in text, mode


def test_original_and_translation_only_carry_their_own_side():
    v = assemble_versions(PAGES)
    assert v["original"].startswith("# 1 INTRO")
    assert "你好。" not in v["original"]
    assert v["translation"].startswith("# 1 引言")
    assert "Hello." not in v["translation"]
    # 图片引用原样保留（谁拼的引用就用谁的前缀：`/api/v1` 或 `figures`）
    assert "![Figure 1](figures/a.png)" in v["original"]


def test_bilingual_pairs_both_sides_per_page():
    """中英对照 = 逐页「原文在上、译文在下」，页与页之间用 `---` 分隔。"""
    v = assemble_versions(PAGES)["bilingual"]
    assert "### 原文" in v and "### 译文" in v
    assert v.index("Hello.") < v.index("你好。")            # 同一页内：原文在前
    assert v.count("### 原文") == 2                         # 空页不产生一段
    assert v.count("\n---\n") == 1                          # 两页之间一条分隔线
    assert v.endswith("\n")


def test_empty_input_gives_empty_versions():
    """没有页级产物时给空串（不是一堆分隔线），前端据此显示"还没有翻译产物"。"""
    v = assemble_versions([])
    assert v == {"original": "", "translation": "", "bilingual": ""}


def test_image_prefix_comes_from_the_caller():
    """同一个页产物，图片前缀不同 → 下载用绝对地址、打包用相对目录。"""
    pages = [{"page": 1, "original_md": "![Figure 1](/api/v1/papers/x/figures/region/1/0)",
              "translation_md": "![Figure 1](figures/region-p1.png)"}]
    v = assemble_versions(pages)
    assert "/api/v1/" in v["original"]            # 调用方注入的前缀原样保留
    assert "figures/region-p1.png" in v["translation"]


def test_export_markdown_absolutizes_image_refs(monkeypatch):
    """下载的 .md 在本地打开时，根相对图片路径会被解析成 `file:///api/v1/...` → 必须补成绝对地址。

    ⚠️ 补只能用**根相对**前缀（`/api/v1`）+ `absolute_base` 两段来做：`markdown_refs` 按
    "前缀是不是以 / 开头"决定拼接口 URL 还是落盘相对路径，把 `http://host/api/v1` 整串传进去
    会掉进相对路径那一支，拼出 `http://host/api/v1/region-p0003-….png` 这种不存在的地址（实测踩过）。
    """
    monkeypatch.setattr(reader_service, "markdown_versions",
                        lambda pid, *, img_prefix: {          # noqa: ARG005
                            "original": "![Figure 1](/api/v1/papers/x/figures/region/1/0)\n",
                            "translation": "", "bilingual": ""})
    out = reader_service.export_markdown("p", "original", img_prefix="/api/v1",
                                         absolute_base="http://127.0.0.1:8710")
    assert "](http://127.0.0.1:8710/api/v1/papers/x/figures/region/1/0)" in out
    # 不给 absolute_base 时保持根相对（应用内 Markdown 视图用得到）
    assert reader_service.export_markdown("p", "original").startswith("![Figure 1](/api/v1/")
