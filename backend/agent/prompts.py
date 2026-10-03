"""Agent 的提示词。

两条组织原则：

1. **system 侧只放"这篇论文 / 这次部署不变"的内容**：基础指令 → 论文档案 → 通用规则。
   前缀缓存是**从第 0 个 token 起比前缀**的，system 里混进"这一轮才有的值"
   （选区原文、图片张数、检索分），`system + 全部历史` 就整段按未命中计费
   （DeepSeek 命中部分约 0.1×）。那些每轮会变的值统一走 `turn_notes()`，
   由 `runtime._user_prompt()` 拼在**最后一条用户消息**里。
   ⚠️ 只把动态内容"排在 system 的后半段"是没用的 —— 前缀对不上就是整段不算命中。
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
- 编号是**每一轮各自从 1 开始**的：历史回答里的 `[n]` 属于那一轮（它后面附的材料索引能对上），
  本轮要引用某条材料，必须**自己调工具**拿本轮的编号；不要沿用历史里的编号，也不要拿历史编号
  当本轮的依据。
- 一个论断由多条材料支撑就并列写，例如 [3][7]。整段都是常识性铺垫时可以不加。
- 配图：只有 figure_list 返回过 image_url 的图/表才能配。逐字照抄那个地址写成
  `![简短说明](image_url)`，不要改写、不要拼接、不要用外部图片地址。"""

NO_EVIDENCE = """回答格式：
- 先给结论，再给依据（依据里带 [ref]）。
- 长度按问题来：问"是什么"就一段话，问"怎么做"才分点展开。不要为了显得完整而注水。
- 如果材料只够回答一部分，就回答那一部分，并明确说哪一部分没有依据。"""


# --------------------------------------------------------------------------
# 常驻规则（**同一篇论文逐字节不变**）与"本轮备注"（每轮都会变，拼在用户消息里）
#
# 为什么分成两堆：system 在请求最前面，provider 的前缀缓存是**从第 0 个 token 起比前缀**的 ——
# 这里只要变一个字节，`system + 全部历史` 就都按未命中计费（DeepSeek 命中部分约 0.1×）。
# 2026-10-03 之前 selection_block / image_block / evidence_note 都拼在 system 里，
# 于是"划词问一次的下一轮"、"检索分低的那一轮"都会把整段前缀打掉，下一轮块消失又打一次。
# 现在：规则常驻（下面这三块），每轮的值走 `turn_notes()` → `runtime._user_prompt()` 拼到最后。
# --------------------------------------------------------------------------

SELECTION_RULES = """【用户划的选区】
用户消息以 `> 〔第 N 页〕原文` 这样的引用块开头时，那就是用户划出来的选区
（本轮备注里会给它的编号与页号）：
- 用户问的是**这段内容**，不要跑去总结全篇。
- 先 block_get(ref=备注里那个编号) 读它所在的完整段落（必要时再读相邻页）—— 选区常常只是
  半句话，光看它容易解释错；备注里没有编号时，用 page_markdown_get 读它所在的那一页。
- 若选区里有公式/符号，用 formula_get 或 page_image_get(dpi=300) 核对，不要凭字形猜。
- 选区若与译文不一致，以原文为准并指出这个不一致。"""

IMAGE_RULES = """【用户上传的图片】
这条消息里带了图片时，那是**用户自己给的材料**（本轮备注里写了张数）：
- 可以据此回答，但不要把它当作论文正文去引用（引用编号只给论文里真实存在的块）。
- 图片看不清 / 与论文无关时直接说明，不要硬凑。
- 备注里写了"还有 N 张这次没一起发过来"时，需要那张就请用户单独再问一次。"""

NOTES_RULE = """【本轮备注】
用户消息开头可能有一段 `【本轮备注】…`：那是系统给**这一轮**补的定位信息（选区的编号与页号、
图片张数、检索相关度这类每轮都可能不一样的东西）。按它办事，但**不要在回答里复述、引用或
提到这段备注本身** —— 它是系统的定位信息，不是用户说的话。"""

EDIT_RULES = """【改产物：只有用户要求时才做】
你能改**页级产物**里的字句（英文转录 = OCR 结果、中文译文），用的是 `page_markdown_edit`。
它是唯一会改用户数据的动作，因此：
- **只有用户明确要求修改时才用**（"这处识别错了，改成…"、"这句翻译不对"、"把 X 统一成 Y"）。
  你自己觉得译得别扭、或与你的理解不同，都**不算理由**：那要在回答里说出来，而不是动手改。
- **先读后写**：先弄清哪一侧、错在哪（原文与译文用 `page_markdown_get` 一起看；OCR 的错
  要对着 `page_image_get` 的原版面核对），再用 `op="view"` 取到那一段逐字文本，才动 `op="replace"`。
- 一次只改一处（`find` 必须在整页里唯一）。改完在回答里逐条报告：第几页 · 哪一侧 ·
  原来是什么 · 改成了什么 —— 用户的正文、导出与检索索引都会跟着变，别让用户自己去找。
  改错了就 `op="undo"` 撤回。"""


SELECTION = """【本轮的选区】选中原文{where_inline}：
> {text}

{where}{read_hint}"""


def selection_block(deps: Deps) -> str:
    """**这一轮**的选区（选区原文 + 它的位置/编号 + 先读哪里的提示）。

    规则本身常驻在 system 的 `SELECTION_RULES` 里；这里只放每轮会变的值 ——
    放进 system 会让"划词那轮的下一轮"整段前缀缓存失效（见 `turn_notes`）。

    位置有三种情形（2026-10-03 补全）：有 ref 且知道页号 → 两个都给；只有 ref → 只给 ref；
    **只有页号**（原 PDF 划词没有块锚点）→ 只说页码并交代"只能依据选区文字"，不要让模型
    以为它读过那一页。
    """
    if not deps.selection:
        return ""
    ref = deps.selection_ref
    page = deps.selection_page
    where = ""
    if ref and page:
        where = f"（它位于第 {page} 页，已登记为 ref={ref}。）\n"
    elif ref:
        where = f"（已登记为 ref={ref}。）\n"
    elif page:
        where = (f"（它位于第 {page} 页；这一处没有段落锚点 —— 只依据选区文字回答，"
                 f"需要那一页的上下文就用 page_markdown_get 去读。）\n")
    read_hint = (f"先用 block_get(ref={ref}) 读它所在的完整段落（必要时再读相邻页）—— "
                 f"选区常常只是半句话，光看它容易解释错。"
                 if ref else
                 "选区常常只是半句话：先 page_markdown_get 读它所在的那一页（必要时读相邻页），"
                 "看清上下文再解释。")
    return SELECTION.format(text=deps.selection.strip()[:2000],
                            where=where,
                            where_inline=(f"（第 {page} 页）" if page else ""),
                            read_hint=read_hint)


def build_instructions(deps: Deps) -> str:
    """本轮的系统指令。

    ⚠️ **对同一篇论文必须逐字节稳定**：system 在请求最前面，前缀缓存从第 0 个 token 起比 ——
    这里变一个字节，`system + 全部历史` 就都按未命中算。所以这一串只放"这篇论文 / 这次部署
    不变"的内容（基础指令、档案、通用规则）；选区的编号与原文、图片张数、检索相关度这些
    **每轮会变**的值一律走 `turn_notes()`（拼在最后的用户消息里）。
    """
    parts = [BASE]
    if deps.archive_text:
        parts.append(deps.archive_text)
    parts.append(SCOPE)
    parts.append(SELECTION_RULES)
    parts.append(IMAGE_RULES)
    parts.append(CITING)
    parts.append(NO_EVIDENCE)
    parts.append(EDIT_RULES)
    parts.append(NOTES_RULE)
    return "\n\n".join(parts)


def turn_notes(deps: Deps) -> str:
    """**这一轮**的备注（拼在用户消息开头，不进 system）。

    里面每一样都是"每轮可能不同"的值：选区（原文 / ref / 页号）、图片张数、检索相关度提示。
    放在最后一条用户消息里，前面那一大段（system + 历史）才缓存得住；
    `runtime._user_prompt()` 负责拼，`NOTES_RULE` 负责让模型别把这段复述出来。
    """
    items: list[str] = []
    if deps.selection:
        items.append(selection_block(deps))
    if deps.attachments:
        items.append(image_block(deps))
    if deps.evidence_note:
        items.append(deps.evidence_note)
    if not items:
        return ""
    return "【本轮备注】\n" + "\n\n".join(items)


def image_block(deps: Deps) -> str:
    """图片说明（**每轮的值**，所以走 `turn_notes()` 拼在用户消息里，不进 system）。

    踩过一次：把"这几张图是用户上传的材料，别当成论文正文"写在 user prompt 的括号里，
    模型会**把这句说明原样抄进回答**（实测回答末尾跟着一段"（本轮随问题上传了 1 张图片…）"）。
    所以现在两边分工：**规则**（图片是用户材料、不要当论文正文引用）常驻在 system 的
    `IMAGE_RULES` 里；这里只放"这一轮到底几张、几张没随消息发过来"这种**值**，
    并在 system 的 `NOTES_RULE` 里明确"别复述这一段"。
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
