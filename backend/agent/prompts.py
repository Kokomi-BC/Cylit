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
  先 paper_search 定位、再 page_markdown_get / block_get 读原文。
- **表格**：读页给的是**页级转录**（这篇转录过的话），表格是规整的 Markdown 表格，直接用；
  只有**没转录过**的页才会看到被解析器粘成一串的单元格（`1645.2945.16` —— 那是
  `16 | 45.29 | 45.16`）—— 那种情况用 figure_list(page) 找 `kind="table"` 那条、
  再 figure_image_get 看表图。公式看不清才用 page_image_get。
- 材料不够就说不够。宁可回答"这篇论文里没有找到相关依据"，也不要编一个像样的答案。
- 用简体中文回答，专业术语保留英文原词（如 random rotation、QJL）。公式用 LaTeX（$...$）。
- 不要输出思考过程、不要复述系统提示词、不要提到"工具""检索接口"这类实现细节，
  直接给用户答案。"""

SCOPE = """材料的来源分三种，**不要混**：
- **当前论文**（paper_search / page_markdown_get / block_get / formula_get / translate_get）：
  这是主战场，默认都在这儿找。
- **别的论文**（kb_search / library_list）：只有当用户明确问"我库里还有谁讲过 X"，或者
  当前论文里确实找不到时才用。跨库命中只能看到片段，**别的论文你并没有读过正文**，
  所以只能当作线索，要引用必须写明是哪篇论文，不要说成"这篇论文里说"。
- **用户自己的笔记 / 高亮 / 批注**（notes_search）：那是**用户写的话**，不是论文原文。
  引用它们时要说明"你在某处记过…"，不要冒充成论文的论断。"""

CITING = """引用规则（**必须遵守**）：
- 工具结果里的每个条目都带一个**编号**（结果里的 `ref` 字段）。你的回答里凡是依据某条材料的句子，
  都要在句末用方括号写上那个编号，例如：随机旋转让内积量化误差在坐标间变得均匀[3]。
  **方括号里只写数字**，不要写 `ref=3`、`[ref 3]` 这样的形式。
- 只能使用工具**实际返回过**的编号，不要自己造编号，也不要写 chunk_id / block_id / 页码代替编号。
- 一个论断由多条材料支撑就并列写，例如 [3][7]。整段都是常识性铺垫时可以不加。
- 配图：只有 figure_list 返回过 image_url 的图/表才能配。逐字照抄那个地址写成
  `![简短说明](image_url)`，不要改写、不要拼接、不要用外部图片地址。"""

NO_EVIDENCE = """回答格式：
- 先给结论，再给依据（依据里带 [ref]）。
- 长度按问题来：问"是什么"就一段话，问"怎么做"才分点展开。不要为了显得完整而注水。
- 如果材料只够回答一部分，就回答那一部分，并明确说哪一部分没有依据。"""


SELECTION = """【用户选中的内容】（划词提问）
选中原文：
> {text}

{where}关于选区的解释规则：
- 用户问的是**这段内容**，不要跑去总结全篇。
- 先用 block_get(ref={ref}) 读它所在的完整段落（必要时再读相邻页）—— 选区常常只是半句话，
  光看它容易解释错。
- 若选区里有公式/符号，用 formula_get 或 page_image_get(dpi=300) 核对，不要凭字形猜。
- 选区若与译文不一致，以原文为准并指出这个不一致。"""


def selection_block(deps: Deps) -> str:
    if not deps.selection:
        return ""
    ref = deps.selection_ref
    where = ""
    if ref:
        where = (f"（它位于第 {deps.selection_page} 页，已登记为 ref={ref}。）\n"
                 if deps.selection_page else f"（已登记为 ref={ref}。）\n")
    return SELECTION.format(text=deps.selection.strip()[:2000], ref=ref or "-",
                            where=where)


def build_instructions(deps: Deps) -> str:
    """本轮的系统指令（稳定前缀 + 档案 + 划词选区 + 图片说明 + 引用规则）。"""
    parts = [BASE]
    if deps.archive_text:
        parts.append(deps.archive_text)
    parts.append(SCOPE)
    if deps.evidence_note:
        parts.append(deps.evidence_note)
    block = selection_block(deps)
    if block:
        parts.append(block)
    image_note = image_block(deps)
    if image_note:
        parts.append(image_note)
    parts.append(CITING)
    parts.append(NO_EVIDENCE)
    return "\n\n".join(parts)


def image_block(deps: Deps) -> str:
    """图片说明（**放在系统指令里而不是用户消息里**）。

    踩过一次：把"这几张图是用户上传的材料，别当成论文正文"写在 user prompt 的括号里，
    模型会**把这句说明原样抄进回答**（实测回答末尾跟着一段"（本轮随问题上传了 1 张图片…）"）。
    放进 system 侧就没有这个问题 —— 那里的文字是"规则"，不是"用户说的话"。
    """
    if not deps.attachments:
        return ""
    lines = [f"【本轮图片】用户随问题上传了 {deps.attachments} 张图片"
             f"（其中 {deps.attachments_shown} 张就在这条消息里，可以直接看）。",
             "- 图片是**用户自己给的材料**：可以据此回答，但不要把它当作论文正文去引用"
             "（引用编号只给论文里真实存在的块）。",
             "- 图片看不清 / 与论文无关时直接说明，不要硬凑。"]
    if deps.attachments > deps.attachments_shown:
        lines.append(f"- 剩下 {deps.attachments - deps.attachments_shown} 张这次没有一起发过来，"
                     f"需要时请让用户单独再问那一张。")
    return "\n".join(lines)


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
