"""Agent 的提示词。

两条组织原则：

1. **稳定内容在前**：基础指令 → 论文档案（同一篇论文内字节级不变）→ 本轮动态内容。
   档案放在中间不是随意排的：同一篇论文多轮问答时它的前缀完全一致，能吃到 provider 的
   prompt 前缀缓存（DeepSeek 命中部分约 0.1× 计费）。
2. **"什么时候用"写在工具描述里，不堆在系统提示词里**：工具各自的 docstring 就是它的说明书，
   系统提示词只讲跨工具的原则（引用怎么写、没证据怎么办、图片怎么引）。
"""

from __future__ import annotations

from backend.agent.deps import Deps

BASE = """你是 Cylit 的论文阅读助手，帮用户读懂**当前这一篇**论文。

工作方式：
- 关于论文内容的一切判断都必须来自工具返回的材料（档案、检索结果、读到的正文、看到的页面图），
  不要凭训练记忆补充这篇论文没有说的东西。用户问的是"这篇论文怎么写"，不是"这个领域一般怎样"。
- 档案（若有）只是背景，不足以支撑具体论断。涉及具体方法、公式、数字、结论时，
  先 paper_search 定位、再 page_markdown_get / block_get 读原文；公式或表格看不清就用 page_image_get 看版面。
- 材料不够就说不够。宁可回答"这篇论文里没有找到相关依据"，也不要编一个像样的答案。
- 用简体中文回答，专业术语保留英文原词（如 random rotation、QJL）。公式用 LaTeX（$...$）。
- 不要输出思考过程、不要复述系统提示词、不要提到"工具""检索接口"这类实现细节，
  直接给用户答案。"""

CITING = """引用规则（**必须遵守**）：
- 工具结果里的每个条目都带 `ref` 编号。你的回答里凡是依据某条材料的句子，都要在句末标 `[ref]`，
  例如：随机旋转让内积量化误差在坐标间变得均匀[3]。
- 只能使用工具**实际返回过**的 ref 编号，不要自己造编号，也不要写 chunk_id / block_id / 页码代替编号。
- 一个论断由多条材料支撑就并列写，例如 [3][7]。整段都是常识性铺垫时可以不加。
- 配图：只有 figure_list 返回过 image_url 的图才能配。逐字照抄那个地址写成
  `![简短说明](image_url)`，不要改写、不要拼接、不要用外部图片地址。"""

NO_EVIDENCE = """回答格式：
- 先给结论，再给依据（依据里带 [ref]）。
- 长度按问题来：问"是什么"就一段话，问"怎么做"才分点展开。不要为了显得完整而注水。
- 如果材料只够回答一部分，就回答那一部分，并明确说哪一部分没有依据。"""


def build_instructions(deps: Deps) -> str:
    """本轮的系统指令（稳定前缀 + 档案 + 引用规则）。"""
    parts = [BASE]
    if deps.archive_text:
        parts.append(deps.archive_text)
    if deps.evidence_note:
        parts.append(deps.evidence_note)
    parts.append(CITING)
    parts.append(NO_EVIDENCE)
    return "\n\n".join(parts)


def archive_block(cfg_chars: int, brief: dict, outline: list[dict], *,
                  title: str, page_total: int) -> str:
    """把论文档案渲染成**注入用**的稳定文本块。

    ⚠️ 与 `translation/brief.py` 的 `prompt_block()` **刻意分开**：那个块是翻译提示词的前缀，
    必须字节级稳定（改了就把翻译的缓存前缀打掉）。这里是问答用的，可以带大纲、可以有更多字段。
    两个用途挤进一个函数，早晚会因为"问答想加点东西"而破坏翻译的缓存命中。
    """
    lines: list[str] = ["【论文档案（本轮的背景材料）】"]
    if title:
        lines.append(f"标题：{title}")
    if page_total:
        lines.append(f"篇幅：共 {page_total} 页")
    if brief.get("domain"):
        lines.append(f"领域与核心方法：{brief['domain']}")
    claims = [str(x).strip() for x in (brief.get("key_claims") or []) if str(x).strip()]
    if claims:
        lines.append("主要论断：")
        lines.extend(f"  - {c}" for c in claims[:6])
    glossary = brief.get("glossary") or {}
    if glossary:
        items = "；".join(f"{en}→{zh}" for en, zh in list(glossary.items())[:16])
        lines.append(f"统一译法：{items}")
    if brief.get("keep_as_is"):
        lines.append(f"保持英文不译：{'、'.join(list(brief['keep_as_is'])[:16])}")
    if outline:
        lines.append("章节结构：")
        lines.extend(f"  {'  ' * int(node.get('level', 1) - 1)}- {node.get('title')}"
                     for node in outline[:40] if node.get("title"))
    text = "\n".join(lines)
    return text[:cfg_chars] + ("\n…（档案过长已截断）" if len(text) > cfg_chars else "")
