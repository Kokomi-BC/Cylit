"""页级翻译（**唯一粒度**）——页图 + 页内文本 → 原文 Markdown + 译文 Markdown → 逐块对齐。

设计（与上一版的区别）

上一版是「按页喂文本 → 模型只回译文 → 再用 :mod:`page_aligner` 的锚点 + 保序 DP
把整页译文切回 PDF 抽出的块」，有三个固有毛病：抽取块的边界本来就不可靠、模型
合并/拆分段落时切不准、切不准就要回退块级补译（调用数翻倍）。

本版把「原文」也交给模型产出：同一页、同一次调用里，模型先**忠实转录**这一页
（OCR / 版面还原），再给出**块划分与之逐一对应**的中文译文。于是：

- **对齐是天然的**：两份 Markdown 的块按序号一一对应，不需要 DP、不需要回退补译；
- **不信任抽取结果也没关系**：抽取文本只作为参考塞进 prompt，页图才是事实来源；
- **目录中英双语**：标题块同时带原文与译文，目录直接由标题行生成；
- **只有一种粒度**：一页一次调用。图注不再单独调用，公式不再特殊处理，
  参考文献由模型原样保留（不翻译，不额外调用）。

产物

- ``md/pages.json``：逐页产物（两份 Markdown + 对齐后的行 + 用量），**翻译的唯一事实源**；
- 每页写盘即原子落盘，中断后按页续跑（``pages.json`` 里已有的页不再调用模型）。

完全信任模型：返回的行**不做校验、不做回退**。模型没给出原文/译文时只记一次
``align`` 质量标记（``exact`` / ``structure``），供前端提示，不触发任何补救调用。
"""

from __future__ import annotations

import json
import re
import threading
from dataclasses import dataclass
from difflib import SequenceMatcher
from pathlib import Path

from backend.core.errors import ProviderError
from backend.core.logging import get_logger
from backend.core.utils import (
    json_loads, now_iso, read_json, sha256_text, write_json,
)
from backend.parsing.md.generator import (
    heading_level_or_none, normalize_heading_text, section_depth,
)

log = get_logger(__name__)

# 产物与版本
PAGES_FILENAME = "pages.json"
PAGES_VERSION = 3

# --------------------------------------------------------------------------
# Prompt
# --------------------------------------------------------------------------

SYSTEM_PROMPT = """你是学术论文的「页面转录 + 翻译」专家。每次给你一页论文的**页图**与**解析器抽出的页内文本**，
你要在同一次回复里产出两份互相独立、结构完全对应的 Markdown：先忠实转录原文，再给出中文译文。

【最高原则：一切以页图上**显示出来的内容**为准】
- 页图是唯一事实来源。解析器文本只作参考：它可能有断行、连字、错字、缺字、乱码、公式损坏、顺序错乱、表格串行。
- **两者冲突时，一律以页图上看到的内容为准**，包括文字、数字、单位、符号、编号、大小写与上下标。
- 解析器文本里没有、但页图上有的内容要补上；页图上没有、只在解析器文本里出现的，删掉。
- 看不清的字符写 [?]，禁止猜测、禁止用常识补全。

【original_md：原文转录】
1. 标题层级**必须与【全文标题骨架】一致**（骨架里写 `# 1 TURBOQUANT…`，这一页再遇到它就必须用 `#`）。
   骨架里没有的标题按编号深度推断：`1` / `A` / `ABSTRACT` / `REFERENCES` 这类章节级用 `#`，
   `1.1` / `B.2` 用 `##`，`1.1.1` 用 `###`。**同一层级在全文必须一致**，
   不要因为这一页看起来像子节就多加一个 `#`；编号（1、1.1、A.2）原样保留。
2. 正文按页图分段，段间空行分隔；行内公式写 $...$，独立公式写 $$...$$ 并单独成段。
3. **插图**：图注单独成段，写成 **Figure 1: ...** / **Table 2: ...**，编号必须与页图上实际编号一致
   （页图上是 Figure 2 就写 Figure 2）。图内的子图标题（(a)/(b)…）、坐标轴名、刻度、图例、
   旁边标注的文字，若页图看得清就一并写进图注所在段的下一行，看不清就省略，不要编造。
4. **表格**：用 Markdown 表格输出（`| 列 | 列 |` + `|---|---|`），**逐格照页图抄写数字与文字**：
   行名、列名、单位、每格数值都要与页图一致；页图上的粗体/分组行用 `**...**` 表示。
   页图上表格过大、无法在一屏内看清的，抄下能看清的行列，其余不要臆造。
5. 参考文献条目逐条保留（保留 [12] 这类编号），不要翻译、不要合并。
6. 丢弃页眉、页脚、页码、脚注横线、出版社水印。

【translation_md：中文译文】
1. **块划分必须与 original_md 一一对应**：第 i 块译第 i 块，块的数量、顺序、类型完全一致
   （标题对标题、段落对段落、公式对公式、图注对图注、表格对表格）。
2. 标题的层级与编号必须与 original_md 的**对应块完全一致**（例：## 3.2.1 子中心弧面损失）。
3. 公式的 LaTeX 逐字符原样保留，不改任何符号。
4. 图注/表注保留编号（例：**图 1：…**）；**表格里的数字、单位、方法名、数据集名原样保留**，
   只翻译行名/列名这类自然语言表头。
5. 参考文献条目**不翻译**：与 original_md 的对应块保持相同文本。
6. 术语按给定术语表统一；学术书面语；不增删任何内容。

【前文衔接】prompt 里可能给出**已经处理过的前文结尾**：只用它保持术语、句式、段落衔接一致
（尤其是段落跨页的情况），**不要把它重复输出到本次结果里**。

【跨页衔接（三 条硬规则，页尾/页首最容易出错）】
1. **译文里禁止保留断词连字符**：页尾原文被版面折断成 `communication bot-` 这种，是**折行**不是连字符词；
   译文写完整词（→「通信瓶颈」），不许写成「通信瓶-」或半截词。
2. **本页首块若是上一页句子的延续，直接续译成完整通顺的中文**：
   不要用「……」/「…」/「（承接上页）」之类的占位或标记开头，也不要重译上一页已经翻过的内容。
3. 搭接块（【下一页开头】）**只用于理解句子怎么接下去**：
   - 原文转录（original_md）仍**只转录本页页图能看到的内容**，不要把搭接块抄进来；
   - 但译文可以据它把页尾的半截词补成完整词。
4. **标记不许跨页悬空**：若上一页把 `*`（斜体/加粗）或 `$$` 打开却没闭合，本页首块要把标记补齐成对；
   本页末尾同理 —— 宁可把标记收在本页，也不要留给下一页一个孤立的 `*` 或 `$$`。

【输出格式】只输出一个 JSON 对象，不要解释、不要代码块：
{"original_md": "……", "translation_md": "……"}"""

PAGE_USER_TEMPLATE = """{prefix}{previous}

【本页】第 {page} 页 / 共 {page_total} 页{section_line}
【解析器抽出的本页文本（**仅供参考**；与页图冲突时以页图为准）】
<页面文本>
{body}
</页面文本>{lookahead}

【本页输出】按系统提示输出 JSON：original_md 为忠实转录（一切以页图显示的内容为准，
表格逐格照抄、图注编号与页图一致），translation_md 为逐块对应的中文译文。"""

PAGE_SCHEMA = {
    "type": "object",
    "properties": {
        "original_md": {"type": "string"},
        "translation_md": {"type": "string"},
    },
    "required": ["original_md", "translation_md"],
}

# 思考模式（DeepSeek 默认开启）会把 max_tokens 吃光导致正文为空，按阶梯升档。
MAX_TOKENS_LADDER = (8192, 16384, 32768, 65536)
SAFE_MAX_TOKENS = MAX_TOKENS_LADDER[0]

# 单页原文文本送入 prompt 的上限（页图才是事实来源，文本只是辅助）
MAX_PAGE_TEXT_CHARS = 16000

# 前文（完整历史）的长度上限。
#
# 为什么默认给得这么大（≈ 200 万字）：目标模型窗口 **1M token**（约 200~300 万字，
# 实测演示论文的前 21 页原文+译文合计 13.4 万字），所以**正常论文根本不该截断** ——
# 这个数就只是"别真把十几 MB 塞进去"的护栏。
#
# 为什么不做"只带最近几页"的小预算：前缀缓存比的是**最长公共前缀**，历史只要完整且逐页追加，
# 第 N 页与第 N+1 页的 prompt 从稳定前缀到"第 N-1 页结束"逐字节一致 → 前文全部命中缓存
# （命中部分约 0.1× 计费）；而滑窗式的小尾巴每页都在挪，反而把公共前缀截断在头部，
# 既丢记忆又丢缓存。
#
# 真要控成本/蹭窗口上限时用任务参数 `context_chars` 调小，`context=false` 完全关闭。
CONTEXT_MAX_CHARS = 2_000_000


def effective_max_tokens(configured: int | None) -> int:
    if configured and configured > 0:
        return max(int(configured), SAFE_MAX_TOKENS)
    return SAFE_MAX_TOKENS


def next_max_tokens(current: int) -> int | None:
    for candidate in MAX_TOKENS_LADDER:
        if candidate > current:
            return candidate
    return None


def build_stable_prefix(brief, glossary_text: str = "", skeleton: str = "") -> str:
    """整篇字节级不变的前缀（论文简报 + 术语表 + **全文标题骨架**），放最前以命中前缀缓存。

    `skeleton` 也必须是整篇不变的（调用方在任务开始时算一次），否则前缀缓存全失效。
    """
    parts: list[str] = []
    if brief is not None and getattr(brief, "enabled", False):
        block = brief.prompt_block()
        if block:
            parts.append(block)
    if glossary_text:
        parts.append(f"【术语表（必须遵守）】\n{glossary_text}")
    if skeleton:
        parts.append(skeleton)
    return "\n\n".join(parts)


def build_outline_skeleton(paper_id: str, store: "PageStore | None" = None,
                           *, limit_lines: int = 80) -> str:
    """【全文标题骨架】：整篇字节级不变，因此可以放进**稳定前缀**（前缀缓存仍然命中）。

    为什么必须有它 —— 页级翻译是"一页一次"的独立调用，模型**没有任何跳页记忆**，
    每页各自猜 `#` 深度，于是同一层级在不同页拿到不同深度。实测 TurboQuant：
    `1 TURBOQUANT…`（一级）回来是 `#`，`2 EXPERIMENTS`（同为一级）却是 `##`，
    `1.2 INNER-PRODUCT…`（二级）回来是 `###`。把"编号 → 层级"的权威映射一次给全，
    深度就不再逐页飘（并彮由 `reader_service` 的编号优先建树 / 阅读区层级）。

    来源优先用解析索引（原生页有字号证据）；扫描件没有文本层时，退回**任务开始时**
    已有页产物里的标题行（取到什么算什么，一次定下来）。
    """
    from backend.services.paper import reader_service

    lines: list[str] = []
    index = reader_service.load_block_index(paper_id)
    for node in index.get("blocks") or []:
        if node.get("type") != "heading":
            continue
        raw = str(node.get("text") or "")
        # 解析会把伪代码行（`3: Generate …`）与表格数值行也标成 `type=heading`
        # （heading_level=0）；它们一旦进骨架，模型就会拿它当标题的依据 → 用同一套判据剔掉。
        if heading_level_or_none(raw) is None:
            continue
        text = normalize_heading_text(raw)
        if not text:
            continue
        lines.append(f"{'#' * section_depth(_number_of(text))} {text}")
        if len(lines) >= limit_lines:
            break

    if not lines and store is not None:          # 扫描件：用已有产物里的标题行兜底
        for _page, payload in sorted(store.all_pages().items()):
            for row in payload.get("rows") or []:
                if row.get("type") != "heading":
                    continue
                text = normalize_heading_text(str(row.get("original") or ""))
                if text:
                    lines.append(f"{'#' * section_depth(str(row.get('number') or _number_of(text)))} {text}")
            if len(lines) >= limit_lines:
                break

    if not lines:
        return ""
    return ("【全文标题骨架（转录原文时**必须**用这里的层级；全文同一层级保持一致）】\n"
            + "\n".join(lines[:limit_lines]))


def history_context(store: "PageStore", paper_id: str, page_no: int, *,
                    max_chars: int = CONTEXT_MAX_CHARS) -> str:
    """**完整前文**（第 1..N-1 页的原文 + 译文），逐页追加，供模型做连贯记忆。

    ## 为什么必须是"完整 + 追加式"，而不是"最近几页的尾巴"

    前缀缓存（DeepSeek 命中部分约 0.1× 计费）比的是**最长公共前缀**：第 N 页与第 N+1 页的
    prompt 只有在"从头开始逐字节相同"的那一段才命中。于是：

    - **完整**：把第 1..N-1 页**整页**放进去。第 N 页的 prompt 是
      `[稳定前缀] + [第 1..N-1 页] + [第 N 页]`，第 N+1 页是 `[稳定前缀] + [第 1..N 页] + …`，
      两者从头到"第 N-1 页结束"完全一致 → **前文全部命中缓存**。
    - **追加式**：每页的渲染模板逐字相同、按页号升序拼装，且**表头不带页码/页数/时间**
      （任何随页变化的东西都会把公共前缀截断在它那里，后面整段历史全部作废）。
    - 若改成"最近两页的尾巴"（滑窗），窗口每页都在挪 → 头部一变，后面全部重新计费，
      既丢记忆又丢缓存。这就是这个函数存在的理由。

    ## 并发与缺页

    页级翻译是**并发**跑的（窗口 1~8），第 N-1 页此刻可能还没落地。这里只取
    **紧邻 N 且连续已完成**的那一串页（`N-1, N-2, …` 直到遇到缺口为止）：正常顺序推进时
    它等于"完整前文"，偶尔乱序时也不会出现"第 4 页缺席但第 5 页在场"这种会错位的拼接。

    ## 溢出保护

    `max_chars` 默认 200 万字，而目标模型窗口是 **1M token**（≈ 200~300 万字）、
    演示论文整篇历史才 13.4 万字 —— 也就是说**正常论文永远不会截断**，这个参数只是"别真塞
    十几 MB"的护栏。真触顶时会**整页地**从最早的页丢（保住最近内容、不切碎块边界），
    代价是公共前缀从被丢的那页起失效（记忆与缓存同时打折），届时会记一条 warning。
    """
    if page_no <= 1 or max_chars <= 0:
        return ""

    products = store.all_pages()
    blocks: list[str] = []       # 按页号升序（追加式的关键：顺序固定、模板逐字相同）
    used = 0
    truncated = False
    earliest = page_no - 1
    for page in range(page_no - 1, 0, -1):
        payload = products.get(page) or {}
        original = str(payload.get("original_md") or "").strip()
        translation = str(payload.get("translation_md") or "").strip()
        if not original and not translation:
            break                # 缺口：不再往前找（避免错位拼接）
        block = (f"<第 {page} 页｜原文>\n{original}\n</第 {page} 页｜原文>\n"
                 f"<第 {page} 页｜译文>\n{translation}\n</第 {page} 页｜译文>")
        if blocks and used + len(block) > max_chars:
            truncated = True     # 整页丢弃（不把一页切成半页）
            break
        blocks.append(block)
        earliest = page
        used += len(block) + 2
    if not blocks:
        return ""
    if truncated:
        log.warning("前文超出 %d 字上限：第 %d 页之前的内容不再作为上下文（记忆与缓存同时打折）",
                    max_chars, earliest)

    blocks.reverse()
    body = "\n\n".join(blocks)
    # ⚠️ 表头必须**逐字固定**：不要写页码/页数/时间，否则每页一变就把前缀缓存截断在这里。
    return ("【前文（已处理过的完整内容，逐页附上；仅供衔接与术语/句式统一，"
            "**不要**重复输出）】\n" + body)


def prefix_hash(prefix: str) -> str:
    return sha256_text(prefix or "")[:16]


# --------------------------------------------------------------------------
# 跨页衔接（页尾断词 / 断句 / 下页续句）
# --------------------------------------------------------------------------


def page_lookahead(paper_id: str, page: int, *, lines: int = 6,
                   max_chars: int = 1200) -> str:
    """**下一页开头**的原文片段，作为"仅上下文"塞进本页 prompt。

    为什么需要：页尾被版面折断时（`communication bot-` | `tlenecks …`），只看本页的
    模型无法知道 `bot-` 是什么词，只能照抄半截；把下一页开头给它，它就能把**译文**里的
    词补全（原文转录仍严格只写本页页图内容）。
    """
    try:
        body, _section = page_source_text(paper_id, page + 1)
    except Exception:  # noqa: BLE001  最后一页 / 索引缺失都不值得中断翻译
        return ""
    picked: list[str] = []
    size = 0
    for line in (body or "").split("\n"):
        text = line.strip()
        if not text:
            continue
        picked.append(text)
        size += len(text)
        if len(picked) >= lines or size >= max_chars:
            break
    if not picked:
        return ""
    return ("【下一页开头（**仅上下文**：只用来理解本页末尾的句子/断词怎么接下去，"
            "**不要把这里的内容输出**）】\n<下一页文本>\n" + "\n".join(picked) + "\n</下一页文本>")


#: 句末标点（含数理论文里的证毕号 □）：页尾见到这些就不算"半句"
_TERMINAL_PUNCT = tuple(".。!！?？:：;；\"')）】”`…□■▪")
_BREAK_TYPES = ("formula", "table", "code")     # 页尾/页首是这些块 → 不按"断句"处理


def _page_text_blocks(paper_id: str, page: int) -> list[dict]:
    """该页的块（按阅读顺序），用于判断页尾/页首是正文还是公式表格。"""
    from backend.services.paper import reader_service

    index = reader_service.load_block_index(paper_id)      # 内部有 mtime 缓存
    rows = [b for b in (index.get("blocks") or [])
            if int(b.get("page") or 0) == int(page) and (b.get("text") or "").strip()]
    rows.sort(key=lambda b: b.get("order") or 0)
    return rows


def page_continuity(paper_id: str, page: int) -> dict:
    """判断本页与**上/下页**之间是否"跨页续句 / 断词"，返回 ``{head, tail}``。

    判据只用**解析器的原文**（确定性、与并发顺序无关），并且要两侧同时成立才认：
    - ``tail``：本页末块以连字符结尾，或"末块不以句末标点收尾 **且** 下页首块小写起头"；
    - ``head``：本页首块小写起头，或上页末块以连字符结尾（断词正好落在页边界）。
    页尾/页首是公式、表格、伪代码时一律不判 —— 那时不存在"句子被切断"的问题
    （实测 p16→p17 的"…lower bound. □ | ## B.2 …"就是被启发式误报的那类）。
    """
    head_blocks = _page_text_blocks(paper_id, page)
    prev_blocks = _page_text_blocks(paper_id, page - 1)
    next_blocks = _page_text_blocks(paper_id, page + 1)

    def is_text(block: dict | None) -> bool:
        return bool(block) and (block.get("type") or "para") not in _BREAK_TYPES

    head = head_blocks[0] if head_blocks else None
    prev_tail = prev_blocks[-1] if prev_blocks else None
    next_head = next_blocks[0] if next_blocks else None

    head_text = (head or {}).get("text", "").strip()
    prev_text = (prev_tail or {}).get("text", "").strip()
    next_text = (next_head or {}).get("text", "").strip()
    ends_hyphen = prev_text.endswith(("-", "‐", "–"))       # 断词正好落在页边界
    starts_lower = bool(head_text) and head_text[:1].islower()

    head_continues = bool(
        is_text(head)
        and ((starts_lower and is_text(prev_tail)) or (ends_hyphen and starts_lower))
    )

    this_tail = head_blocks[-1] if head_blocks else None
    tail_text = (this_tail or {}).get("text", "").strip()
    tail_continues = bool(
        is_text(this_tail) and is_text(next_head) and tail_text and next_text
        and (tail_text.endswith(("-", "‐", "–"))
             or (next_text[:1].islower() and not tail_text.endswith(_TERMINAL_PUNCT)))
    )
    return {"head": head_continues, "tail": tail_continues}


#: 续句开头常见的占位噪声（模型被"页尾半截"误导时会写这些）
_LEAD_JUNK = re.compile(r"^\s*(?:（?承?接上页）?|\(?承?接上页\)?|【接上页】|…+|\.{2,}|—{2,}|–\s*)+")


def mark_rows_continuity(rows: list[dict], *, head: bool, tail: bool) -> None:
    """把跨页衔接标到行上，并清掉续句开头/断词结尾的噪声（**只动译文**）。

    - 首行 ``continued_from_prev``：译文去掉「……」/「（承接上页）」这类占位——
      它们正是"模型知道被切断、却被契约禁止补全"留下的痕迹（实测 p002-00 就是「……在加速器上…」）。
    - 末行 ``continues``：译文去掉结尾悬空的连字符（实测「通信瓶-」）。
      屏幕上的合并显示由前端负责（上页末行的译文让位给下页的完整句）。
    """
    if not rows:
        return
    if head:
        rows[0]["continued_from_prev"] = True
        rows[0]["translation"] = _LEAD_JUNK.sub("", str(rows[0].get("translation") or "")).lstrip()
    if tail:
        rows[-1]["continues"] = True
        rows[-1]["translation"] = re.sub(
            r"[-\u2010\u2013\u2014]\s*$", "", str(rows[-1].get("translation") or "")
        ).rstrip()


def render_page_prompt(*, page: int, page_total: int, body: str,
                       prefix: str = "", section: str = "", previous: str = "",
                       lookahead: str = "") -> str:
    section_line = f"；章节：{section}" if section else ""
    return PAGE_USER_TEMPLATE.format(
        prefix=prefix, previous=f"\n\n{previous}" if previous else "",
        page=page, page_total=page_total,
        section_line=section_line, body=body or "（解析器没有抽到文本，请完全依据页图）",
        lookahead=f"\n\n{lookahead}" if lookahead else "",
    )


# --------------------------------------------------------------------------
# 调用
# --------------------------------------------------------------------------


@dataclass
class PageCallResult:
    original_md: str
    translation_md: str
    model: str = ""
    prompt_tokens: int = 0
    completion_tokens: int = 0
    reasoning_tokens: int = 0
    latency_ms: float = 0.0
    max_tokens: int = 0


def call_page(provider, prompt: str, image: bytes | None, *,
              max_tokens: int) -> PageCallResult:
    """一次页级调用（带页图）。图片不可用时退化为纯文本调用。"""
    try:
        if image and getattr(provider, "supports_vision", False):
            res = provider.generate_vision(prompt, [image], system=SYSTEM_PROMPT,
                                           max_tokens=max_tokens)
        else:
            res = provider.generate(prompt, system=SYSTEM_PROMPT,
                                    temperature=0.2, max_tokens=max_tokens)
    except ProviderError:
        raise
    except Exception as exc:  # noqa: BLE001
        raise ProviderError(f"页级翻译调用失败：{str(exc)[:200]}") from exc

    original, translation = parse_pair(res.text or "")
    if not original.strip() or not translation.strip():
        raise ProviderError(
            "模型未返回完整的 original_md / translation_md（页级调用）",
            retryable=True,
        )
    return PageCallResult(
        original_md=original.strip(), translation_md=translation.strip(),
        model=getattr(res, "model", "") or provider.model_name,
        prompt_tokens=int(getattr(res, "prompt_tokens", 0) or 0),
        completion_tokens=int(getattr(res, "completion_tokens", 0) or 0),
        reasoning_tokens=int(getattr(res, "reasoning_tokens", 0) or 0),
        latency_ms=float(getattr(res, "latency_ms", 0.0) or 0.0),
        max_tokens=max_tokens,
    )


def parse_pair(text: str) -> tuple[str, str]:
    """从模型回复里取 ``original_md`` / ``translation_md``（容错解析）。

    真实调用里可能出现的形状：纯 JSON、带 ```json 围栏、JSON 前后带一句说明。
    都按「先找 JSON 对象，再退回标记切分」处理；仍然失败时返回空串由调用方报错。
    """
    raw = (text or "").strip()
    if not raw:
        return "", ""
    data = _loads_lenient(raw)
    if isinstance(data, dict):
        return str(data.get("original_md") or ""), str(data.get("translation_md") or "")
    # 退路：模型没用 JSON，而是用标记分了两段
    orig = _between(raw, "【原文】", "【译文】") or _between(raw, "<original_md>", "</original_md>")
    trans = _between(raw, "【译文】", None) or _between(raw, "<translation_md>", "</translation_md>")
    return orig or "", trans or ""


def _loads_lenient(raw: str) -> dict | None:
    candidates = [raw]
    fence = re.search(r"```(?:json)?\s*(.*?)```", raw, re.DOTALL)
    if fence:
        candidates.append(fence.group(1).strip())
    start, end = raw.find("{"), raw.rfind("}")
    if 0 <= start < end:
        candidates.append(raw[start:end + 1])
    for cand in candidates:
        try:
            data = json.loads(cand)
        except (ValueError, TypeError):
            continue
        if isinstance(data, dict) and ("original_md" in data or "translation_md" in data):
            return data
    return json_loads(raw, None) if raw.startswith("{") else None


def _between(text: str, start: str, end: str | None) -> str | None:
    i = text.find(start)
    if i < 0:
        return None
    i += len(start)
    if end is None:
        return text[i:].strip()
    j = text.find(end, i)
    return (text[i:j] if j >= 0 else text[i:]).strip()


# --------------------------------------------------------------------------
# Markdown → 块
# --------------------------------------------------------------------------

_HEADING = re.compile(r"^(#{1,6})\s+(.*)$")
_NUMBER = re.compile(r"^(\d{1,2}(?:\.\d{1,2}){0,3}|[A-Z](?:\.\d{1,2}){1,3})\b")
_LIST_ITEM = re.compile(r"^\s*(?:[-*+]|\d{1,2}[.)])\s+\S")
_CAPTION = re.compile(r"^\*{0,2}\s*(Figure|Fig\.|Table|Algorithm|Listing|图|表|算法)\s*\.?\s*\d+",
                      re.IGNORECASE)
_REF_ITEM = re.compile(r"^\s*\[?\d{1,3}\]?[.)]?\s+\S")


def split_blocks(md: str) -> list[dict]:
    """把一页 Markdown 切成块（标题 / 段落 / 公式 / 图注 / 列表 / 表格 / 代码）。

    切法是**纯结构**的：空行分段、`$$` 围栏成块、连续列表项并成一块。
    左右两侧用同一套切法，于是"块数相同"就意味着块序号一一对应。
    """
    lines = (md or "").replace("\r\n", "\n").split("\n")
    blocks: list[dict] = []
    buf: list[str] = []
    mode = "para"          # para | formula | code | list | table
    level = 0

    def flush() -> None:
        nonlocal buf, mode, level
        text = "\n".join(buf).strip()
        buf = []
        if not text:
            mode, level = "para", 0
            return
        btype = mode
        if mode == "para":
            if _CAPTION.match(text):
                btype = "caption"
            elif _REF_ITEM.match(text) and text.count("\n") == 0:
                btype = "ref"
        blocks.append({"type": btype, "level": level if btype == "heading" else 0,
                       "number": _number_of(text) if btype == "heading" else "",
                       "text": text})
        mode, level = "para", 0

    for raw in lines:
        line = raw.rstrip()
        stripped = line.strip()

        if mode == "code":
            buf.append(line)
            if stripped.startswith("```"):
                flush()
            continue
        if mode == "formula":
            buf.append(line)
            if stripped.endswith("$$") and len(stripped) >= 2:
                flush()
            continue

        if not stripped:
            flush()
            continue
        if stripped.startswith("```"):
            flush()
            buf, mode = [line], "code"
            continue
        if stripped.startswith("$$"):
            flush()
            buf, mode = [line], "formula"
            if stripped.endswith("$$") and len(stripped) > 2:
                flush()
            continue

        heading = _HEADING.match(stripped)
        if heading:
            flush()
            buf = [stripped]
            mode, level = "heading", len(heading.group(1))
            flush()
            continue

        if stripped.startswith("|") and stripped.endswith("|"):
            if mode != "table":
                flush()
                mode = "table"
            buf.append(line)
            continue
        if _LIST_ITEM.match(stripped):
            if mode != "list":
                flush()
                mode = "list"
            buf.append(line)
            continue

        if mode in ("table", "list"):
            # 表格/列表块被普通段落打断
            flush()
        buf.append(line)

    flush()
    return blocks


def _number_of(text: str) -> str:
    first = (text or "").split("\n", 1)[0]
    first = re.sub(r"^[\s#]+", "", first.strip())
    m = _NUMBER.match(first)
    return m.group(1) if m else ""


# --------------------------------------------------------------------------
# 两份 Markdown → 对齐后的行
# --------------------------------------------------------------------------


def pair_rows(original_md: str, translation_md: str, page: int) -> tuple[list[dict], str]:
    """把两份 Markdown 按块对齐成行。

    块数相同 → 直接按序号配对（``exact``，绝大多数情况）。
    否则用 ``difflib`` 按「类型 + 编号/文本签名」做一次序列对齐（``structure``），
    多出来的块保留为单边行（原文有译文空，或反之），**不丢弃任何内容**。
    """
    left = split_blocks(original_md)
    right = split_blocks(translation_md)
    if not left and not right:
        return [], "empty"
    pairs: list[tuple[dict | None, dict | None]] = []
    if len(left) == len(right):
        pairs = list(zip(left, right))
        quality = "exact"
    else:
        pairs = _sequence_pairs(left, right)
        quality = "structure"
    rows: list[dict] = []
    for i, (a, b) in enumerate(pairs):
        head = a or b or {}
        rows.append({
            "id": f"p{page:03d}-{i:02d}",
            "index": i,
            "type": head.get("type") or "para",
            "level": int(head.get("level") or 0),
            "number": head.get("number") or "",
            "original": (a or {}).get("text") or "",
            "translation": (b or {}).get("text") or "",
        })
    return rows, quality


def _sequence_pairs(left: list[dict], right: list[dict]
                    ) -> list[tuple[dict | None, dict | None]]:
    keys_l = [_row_key(b) for b in left]
    keys_r = [_row_key(b) for b in right]
    matcher = SequenceMatcher(a=keys_l, b=keys_r, autojunk=False)
    out: list[tuple[dict | None, dict | None]] = []
    for tag, i1, i2, j1, j2 in matcher.get_opcodes():
        if tag == "equal":
            out.extend(zip(left[i1:i2], right[j1:j2]))
        elif tag == "replace":
            span = max(i2 - i1, j2 - j1)
            for k in range(span):
                out.append((left[i1 + k] if i1 + k < i2 else None,
                            right[j1 + k] if j1 + k < j2 else None))
        elif tag == "delete":
            out.extend((b, None) for b in left[i1:i2])
        elif tag == "insert":
            out.extend((None, b) for b in right[j1:j2])
    return out


def _row_key(block: dict) -> tuple:
    btype = block.get("type") or "para"
    text = (block.get("text") or "").strip()
    if btype == "heading":
        return ("h", int(block.get("level") or 0), block.get("number") or _norm(text)[:24])
    if btype == "formula":
        return ("f", _norm(text)[:60])
    return (btype, _norm(text)[:40])


def _norm(text: str) -> str:
    return re.sub(r"\s+", " ", (text or "").lower()).strip()


# --------------------------------------------------------------------------
# 页产物
# --------------------------------------------------------------------------


def build_page_payload(*, page: int, page_total: int, result: PageCallResult,
                       source_chars: int = 0, cached: bool = False,
                       align_override: str | None = None) -> dict:
    rows, quality = pair_rows(result.original_md, result.translation_md, page)
    return {
        "page": page,
        "page_total": page_total,
        "original_md": result.original_md,
        "translation_md": result.translation_md,
        "rows": rows,
        "align": align_override or quality,
        "model": result.model,
        "prompt_tokens": result.prompt_tokens,
        "completion_tokens": result.completion_tokens,
        "reasoning_tokens": result.reasoning_tokens,
        "latency_ms": round(result.latency_ms, 1),
        "max_tokens": result.max_tokens,
        "source_chars": source_chars,
        "cached": cached,
        "updated_at": now_iso(),
    }


class PageStore:
    """``md/pages.json`` 的读写（逐页原子落盘 → 中断可续跑）。"""

    def __init__(self, paper_id: str, path: Path | None = None) -> None:
        self.paper_id = paper_id
        self.path = path
        self._lock = threading.RLock()
        self._data: dict | None = None

    # ---------------- 读写 ----------------
    def _load(self) -> dict:
        with self._lock:
            if self._data is not None:
                return self._data
            if self.path is not None and self.path.is_file():
                data = read_json(self.path, {}) or {}
                pages = data.get("pages") if isinstance(data.get("pages"), dict) else {}
                self._data = {
                    "paper_id": data.get("paper_id") or self.paper_id,
                    "version": int(data.get("version") or PAGES_VERSION),
                    "page_total": int(data.get("page_total") or 0),
                    "model": data.get("model") or "",
                    "updated_at": data.get("updated_at"),
                    "pages": {int(k): v for k, v in pages.items()
                              if isinstance(v, dict)},
                }
            else:
                self._data = {"paper_id": self.paper_id, "version": PAGES_VERSION,
                              "page_total": 0, "model": "", "updated_at": None,
                              "pages": {}}
            return self._data

    def flush(self) -> None:
        with self._lock:
            data = self._load()
            data["version"] = PAGES_VERSION
            data["updated_at"] = now_iso()
            if self.path is not None:
                write_json(self.path, {
                    **{k: v for k, v in data.items() if k != "pages"},
                    "pages": {str(k): v for k, v in sorted(data["pages"].items())},
                })

    def put(self, page: int, payload: dict) -> None:
        with self._lock:
            data = self._load()
            data["pages"][int(page)] = payload
            if payload.get("page_total"):
                data["page_total"] = int(payload["page_total"])
            if payload.get("model"):
                data["model"] = str(payload["model"])
            self.flush()

    def clear(self) -> int:
        with self._lock:
            data = self._load()
            n = len(data["pages"])
            data["pages"] = {}
            self.flush()
            return n

    # ---------------- 查询 ----------------
    def page(self, page: int) -> dict | None:
        return self._load()["pages"].get(int(page))

    def all_pages(self) -> dict[int, dict]:
        return dict(self._load()["pages"])

    def done_pages(self) -> list[int]:
        return sorted(p for p, v in self._load()["pages"].items()
                      if (v.get("rows") or v.get("translation_md")))

    def set_page_total(self, total: int) -> None:
        with self._lock:
            data = self._load()
            if int(data.get("page_total") or 0) != int(total):
                data["page_total"] = int(total)
                self.flush()

    def stats(self) -> dict:
        data = self._load()
        pages = data["pages"]
        rows = sum(len(v.get("rows") or []) for v in pages.values())
        return {
            "page_total": int(data.get("page_total") or 0),
            "pages_done": len([p for p, v in pages.items()
                               if (v.get("rows") or v.get("translation_md"))]),
            "pages_failed": len([p for p, v in pages.items() if v.get("error")]),
            "rows": rows,
            "model": data.get("model") or "",
            "updated_at": data.get("updated_at"),
            "prompt_tokens": sum(int(v.get("prompt_tokens") or 0) for v in pages.values()),
            "completion_tokens": sum(int(v.get("completion_tokens") or 0) for v in pages.values()),
            "reasoning_tokens": sum(int(v.get("reasoning_tokens") or 0) for v in pages.values()),
            "align": _align_counts(pages),
        }


def _align_counts(pages: dict) -> dict:
    out: dict[str, int] = {}
    for v in pages.values():
        key = str(v.get("align") or "?")
        out[key] = out.get(key, 0) + 1
    return out


# --------------------------------------------------------------------------
# 页清单 / 页内文本
# --------------------------------------------------------------------------

_PAGE_PNG = re.compile(r"page-(\d+)\.png$")


def list_pages(paper_id: str) -> list[int]:
    """该论文的页号清单（以渲染出来的页图为准；没有页图时退回 IR 的页数）。"""
    from backend.services.paper import paper_service

    paths = paper_service.paper_paths(paper_id)
    pages_dir: Path = paths["pages"]
    nums: list[int] = []
    if pages_dir.is_dir():
        for p in pages_dir.iterdir():
            m = _PAGE_PNG.search(p.name)
            if m:
                nums.append(int(m.group(1)))
    if nums:
        return sorted(set(nums))
    try:
        from backend.parsing.ir.ir_reader import IRReader

        ir = IRReader(paths["ir"])
        if ir.exists:
            return [int(p["page_no"]) for p in ir.pages()]
    except Exception as exc:  # noqa: BLE001
        log.warning("读取页清单失败：%s", exc)
    return []


def page_source_text(paper_id: str, page: int) -> tuple[str, str]:
    """该页的抽取文本 + 章节名（供 prompt 参考）与字符数。"""
    from backend.services.paper import reader_service

    index = reader_service.load_block_index(paper_id)
    rows = [b for b in (index.get("blocks") or []) if int(b.get("page") or 0) == int(page)]
    rows.sort(key=lambda b: b.get("order") or 0)
    section = next((b.get("section") for b in rows if b.get("section")), "") or ""
    parts: list[str] = []
    size = 0
    truncated = False
    for b in rows:
        text = (b.get("text") or "").strip()
        if not text:
            continue
        btype = b.get("type") or "para"
        if btype == "heading" and not text.lstrip().startswith("#"):
            # 层级用**编号语义**（1 → `#`、1.1 → `##`），与【全文标题骨架】/ 目录同一套。
            # ⚠️ 不要用 b["heading_level"]：它是 `2 + 点号数`，会让模型看到 `## 1 TURBOQUANT`
            # 这种偏一层的参照，模型再自己判断一次，输出的层级就更乱了。
            text = f"{'#' * section_depth(_number_of(text))} {text}"
        if size + len(text) > MAX_PAGE_TEXT_CHARS:
            truncated = True
            break
        parts.append(text)
        size += len(text)
    if truncated:
        parts.append("……（本页文本过长，已截断；请完全依据页图）")
    return "\n\n".join(parts), section


def load_page_image(paper_id: str, page: int) -> bytes | None:
    from backend.services.paper import paper_service

    path = paper_service.paper_paths(paper_id)["pages"] / f"page-{page:04d}.png"
    if not path.is_file():
        return None
    try:
        return path.read_bytes()
    except OSError as exc:
        log.warning("读取页图失败 p%d：%s", page, exc)
        return None


def page_rows(paper_id: str, store: PageStore | None = None) -> list[dict]:
    """阅读器用：按页序拼出全部行（含页信息）。"""
    store = store or PageStore(paper_id, _default_store_path(paper_id))
    out: list[dict] = []
    for page, payload in sorted(store.all_pages().items()):
        for row in payload.get("rows") or []:
            out.append({**row, "page": page})
    return out


def _default_store_path(paper_id: str) -> Path:
    from backend.services.paper import paper_service

    return paper_service.paper_paths(paper_id)["md"] / PAGES_FILENAME
