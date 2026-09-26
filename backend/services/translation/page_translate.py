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
from dataclasses import dataclass, field
from difflib import SequenceMatcher
from pathlib import Path

from backend.core.errors import ProviderError, ValidationError
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
#: 旧格式（**同一次调用**产出原文 + 译文 + 行）的产物版本。拆开两路后它只读不写 ——
#: 存量论文靠它零迁移继续可读（`reader_service.page_products` 会从它合成 A/B 视图）。
PAGES_VERSION = 3
#: 两路各自的产物：A 路只有原文转录，B 路只有中文译文。**必须分文件**：
#: `PageStore.flush()` 是整文件原子重写，两个并发写者共用一个文件会互相覆盖。
PATH_A_FILENAME = "path_a.json"
PATH_B_FILENAME = "path_b.json"
#: 单路产物的版本（与 `PAGES_VERSION` 分开：结构不同，不该共用一个版本号）
PATH_VERSION = 1
#: 两条路的名字（顺序即展示顺序：先英文、后中文）
PATHS = ("a", "b")

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
2. 正文按页图分段，段间空行分隔；行内公式写 $...$，独立公式写 $$...$$ 并单独成段，编号用 `\\tag{n}`。
   **页图上的任何数学符号都要包进定界符**（连方法名下标也算：`$\\text{TurboQuant}_{\\text{mse}}$`），
   不许裸写 `\\text{…}` / `^{…}` / `_{…}`（前端会原样显示成 `Q_mse^{-1}` 这种文字，用户报过）。
3. **插图**：**只写图注本身**，单独成段：**Figure 1: ...** / **Table 2: ...**，编号必须与页图上实际编号一致
   （页图上是 Figure 2 就写 Figure 2）。
   **图里面的一切文字都不要抄、也不要另起一行转述**：子图标签 (a)/(b)、坐标轴名、刻度、图例、
   图内注释、示例句子、示例表格内容、伪代码清单，全部属于图，不属于正文
   （界面会按插图区域单独裁图呈现，读者看图就能看到）。
   ⚠️ 最容易被误写的两种：
   ① 图内**成段文字**（实测 Attention Is All You Need 附录的注意力可视化图，图内示例句被抄成了一行带
   `<EOS>`/`<pad>` 的英文）；
   ② **图例/子图说明行**（实测 TurboQuant 图 3 的图例被抄成
   `SnapKV (Score: 0.858); PyramidKV (Score: 0.895); …`、图 5 的 `(a) …; (b) … vertical axis: …` 一行）。
   两者一律不要出现在 original_md / translation_md 里。
4. **每一行的原文都必须逐字来自给你的文本**（【本页】/【上一页衔接】/【下一页前瞻】/图注本身）；
   给你的文本里没有的句子、标签、数字组合，哪怕在页图上看得一清二楚，也不要新增成行。
5. **表格**：用 Markdown 表格输出（`| 列 | 列 |` + `|---|---|`），**逐格照页图抄写数字与文字**：
   行名、列名、单位、每格数值都要与页图一致；页图上的粗体/分组行用 `**...**` 表示。
   页图上表格过大、无法在一屏内看清的，抄下能看清的行列，其余不要臆造。
6. 参考文献条目逐条保留（保留 [12] 这类编号），不要翻译、不要合并。
7. 丢弃页眉、页脚、页码、脚注横线、出版社水印。

【translation_md：中文译文】
1. **块划分必须与 original_md 一一对应**：第 i 块译第 i 块，块的数量、顺序、类型完全一致
   （标题对标题、段落对段落、公式对公式、图注对图注、表格对表格）。
2. 标题的层级与编号必须与 original_md 的**对应块完全一致**（例：## 3.2.1 子中心弧面损失）。
3. 公式的 LaTeX 逐字符原样保留，不改任何符号。
4. 图注/表注保留编号（例：**图 1：…**）；**表格里的数字、单位、方法名、数据集名原样保留**，
   只翻译行名/列名这类自然语言表头。
   **图/表内部画出来的文字不是正文**：图里的示例句、坐标轴刻度、图例、子图标签、
   `<EOS>` / `<pad>` 这类训练标记，全都属于**图像内容**，一个字都不要写进译文。
   文字只来自**图注本身**。（实测图 3/图 4 的注意力可视化：图内那句示例英文被读成正文，
   连 `<EOS> <pad> <pad> …` 都带了出来；A 路提示词本来就有"只写图注"，所以 A 侧是干净的。）
5. 参考文献条目**不翻译**：与 original_md 的对应块保持相同文本。
6. 术语按给定术语表统一；学术书面语；不增删任何内容。
7. **所有数学都必须写进公式定界符**：行内 `$…$`、独立公式 `$$…$$`（单独成段，编号用 `\\tag{n}`）。
   禁止裸写 LaTeX —— `Q_mse^{-1}`、`S^{d-1}`、`\\text{…}`、`\\mathbb{E}`、`R^{d×d}` 这类**必须包在 `$…$` 里**；
   方法名带下标写成 `$\\text{TurboQuant}_{\\text{mse}}$`。公式块里不要塞中文（中文说明放在公式外的段落）。
8. **不要输出任何页边界痕迹**：页码、「承上页」「续下页」、分隔线、页注释一律不要。

【前文衔接】prompt 里可能给出**已经处理过的前文结尾**：只用它保持术语、句式、段落衔接一致
（尤其是段落跨页的情况），**不要把它重复输出到本次结果里**。

【跨页衔接：页面只是**排版单位**，不是语义单位】
1. **以自然流畅为先**：译文要读起来像一篇完整的中文论文。一句话该在哪里断句就在哪里断，
   **不要为了迁就页面边界去拆句、补句或改写句子结构**。
2. **页尾那句尽量在本页收成完整句**：原文在页边界被切断时，若【下一页开头】里能看到这句话的剩余部分，
   就用它**把这一整句译完**（一直译到真正的句末：句号/问号/分号等），不要留半句、不要写「……」占位。
   这一句的译文**只在本页出现一次**（下一页不会再译它）。例：
   - 本页末尾原文：`…This latency is primarily attributed to communication bot-`
   - 【下一页开头】：`tlenecks between HBM and SRAM on accelerators, or across distributed clusters.`
   - `carry_md`：`tlenecks between HBM and SRAM on accelerators, or across distributed clusters.`
   - 本页末句译文：`…这种延迟主要归因于通信瓶颈，它出现在加速器上的 HBM 与 SRAM 之间，或跨分布式集群。`
   （被版面折断的 `bot-` 要补成完整词「瓶颈」，不许写成「通信瓶-」或半截词。）
   ⚠ `carry_md` **只放原文**（照抄【下一页开头】里用到的那段，一个字符都不要改、不要翻译、不要加标点），
   它是给**下一页**用的——下一页会把这段原文从自己的正文里删掉，所以**少抄会漏译、多抄会造成重复**。
3. **页首那半句已由上一页译完，本页跳过它**：原文被页边界切断时，本页开头往往是上一页那句的后半截
   （形如 `tlenecks between HBM and SRAM on accelerators, or across distributed clusters.`）。
   **跳过这半句**，从**下一个句子**开始译（例：直接译 `By compressing or quantizing model weights …`）。
   同一个词/同一句话**绝不能两页各写一遍**（用户报过「瓶颈」出现两次）。判断依据就是原文本身，
   看不到【前文衔接】时也照此处理；能看到时，别重复上一页已经写出来的词。
4. **页内需要时可以用「——」把补充成分接上**；不要用「也就是说」这类重新起句的引导词。
5. **译文里不要输出任何页边界痕迹**：页码、「承上页」「续下页」、分隔线、页注释一律不要。
6. **标记不许跨页悬空**：本页打开的 `*`（斜体/加粗）或 `$$` 要在本页闭合；
   【下一页开头】只用来补全页尾那句，**不要把搭接块里其他句子的内容输出到本页**
   （原文转录一如既往：只转录本页页图能看到的内容）。

【breaks：语义边界锚点（给**检索分块**用，跟渲染无关）】
- 用途：标出「一个完整语义单元说完了、下一个语义单元开始」的位置。知识库按这些边界切分，
  所以**你标在哪里，就决定了检索怎么切** —— 不要按字数机械切，只按语义判断。
- 写法：每条锚点是一个对象：`start` = **新语义单元开头那一块的起始原文片段**，从该段/该标题的
  **第一个字符**开始**逐字照抄** 12~40 个字符（公式符号原样抄，不要译成中文、不要补省略号），
  要求它能在这页的 original_md 里**唯一定位**；`type` = 这条边界属于哪一类
  （`background`/`definition`/`method`/`algorithm`/`formula`/`experiment`/`result`/`analysis`/`section`/`other`，
  拿不准就写 `other` —— 类型只是留给「论文理解」的标签，**不参与切分判定**）。
- **该标**（典型边界）：定义 → 新定义；背景 → 方法；方法 → 实验；问题 → 解决方案；
  一段推导 → 下一段推导；算法描述 → 实验结果；一个独立论点 → 下一个独立论点；
  新小节的**标题行本身**（表示这个小节从这里开始）。
- **不要标**（标了会被判非法，整页退回机械切分）：
  · 本页**第一块**（页首没有"上一个语义单元"；跨页那句归上一页管）；
  · 一句话中间、一个连续论证的中间；
  · 公式与紧随其后的解释之间（公式要和它的解释在一起）；
  · 表格标题与表体之间、图注与图的说明之间；
  · 公式内部、表格内部、代码块内部、图内文字内部；
  · 标题行的**下一行**（标题必须和它下面第一段在一起）。
- **语义完整性优先于长度**：不要为了凑长度硬标。一页一般 2~6 条；确实没有自然边界就写 `[]`。
- 锚点落在**块边界**上（块 = 空行分隔的段落 / 标题 / 公式 / 表格）。只给**原文**锚点即可 ——
  translation_md 的块与 original_md 一一对应，不需要另给中文锚点。

【output_md：只输出一个 JSON 对象】不要解释、不要代码块：
{"original_md": "……", "translation_md": "……", "carry_md": "……", "breaks": [{"start": "……", "type": "……"}]}

【carry_md：你到底从【下一页开头】借了哪段原文】
- 本页末尾那句靠【下一页开头】补完时，把**你实际用到的那段原文**逐字符照抄进来（原文，不是译文）。
- 没借用（本页句子本来就完整、或下一页开头接不上）就写空字符串 `""`。
- 这段会被**下一页**用来从它的正文里删掉重复部分 —— 照抄准确两边就刚好衔接，抄错就是漏译或重复。"""

PAGE_USER_TEMPLATE = """{prefix}{previous}

【本页】第 {page} 页 / 共 {page_total} 页{section_line}
【解析器抽出的本页文本（**仅供参考**；与页图冲突时以页图为准）】
<页面文本>
{body}
</页面文本>{lookahead}{edges}

【本页输出】按系统提示输出 JSON：original_md 为忠实转录（一切以页图显示的内容为准，
表格逐格照抄、图注编号与页图一致），translation_md 为逐块对应的中文译文，
carry_md 为本页末尾那句从【下一页开头】借用的原文片段（没借就写空串），
breaks 为本页的语义边界锚点（新语义单元开头那块的起始原文片段，没有就写空数组）。"""

PAGE_SCHEMA = {
    "type": "object",
    "properties": {
        "original_md": {"type": "string"},
        "translation_md": {"type": "string"},
        "carry_md": {"type": "string"},
        # 语义边界锚点：检索分块用（见 SYSTEM_PROMPT 的【breaks】一节）。
        # v2：每条是对象 `{"start", "type"}`（v1 的纯字符串数组仍然读得懂，见 `normalize_breaks`）。
        "breaks": {"type": "array", "items": {
            "type": "object",
            "properties": {"start": {"type": "string"}, "type": {"type": "string"}},
            "required": ["start"],
        }},
    },
    "required": ["original_md", "translation_md"],
}


# ==========================================================================
# 两路提示词（A 只转录英文原文 / B 只出中文译文）
# ==========================================================================
#
# 为什么要拆：融合模式下"两侧块结构一致"是同一次推理的副产物；两路并发之后不再是了，
# 于是改用一条**共同的分段规则**来保证（`_SEGMENTATION_RULES`），残差交给 `pair_rows`
# 的结构对齐。好处是每一路都只对自己那一侧负责：A 不产出译文、B 不产出原文。
#
# 两路的**事实来源完全一样**（页图 + 解析器文本 + 全文标题骨架），所以：
# · 谁都不需要等另一路 —— 这才可能真正并发；
# · 提示前缀各自稳定（两套 system prompt → 各自的前缀缓存）。

#: `breaks` 里 `type` 的合法取值（模型自报、代码归一）。
#: **未知类型一律归一成 `other`，不因为"分类不认识"丢掉锚点** —— 锚点本身的价值远大于分类。
BREAK_TYPES = (
    "background", "definition", "method", "algorithm", "formula",
    "experiment", "result", "analysis", "section", "other",
)

_SHARED_PAGE_IMAGE_RULES = """【最高原则：一切以页图上**显示出来的内容**为准】
- 页图是唯一事实来源。解析器文本只作参考：它可能有断行、连字、错字、缺字、乱码、公式损坏、顺序错乱、表格串行。
- **两者冲突时，一律以页图上看到的内容为准**，包括文字、数字、单位、符号、编号、大小写与上下标。
- 解析器文本里没有、但页图上有的内容要补上；页图上没有、只在解析器文本里出现的，删掉。
- 看不清的字符写 [?]，禁止猜测、禁止用常识补全。"""

_SHARED_SEGMENTATION_RULES = """【分段规则（两路**完全一致**：这是"两侧块结构对得上"的唯一保证）】
1. 页图上**一个自然段 = 一块**，块与块之间空一行。
2. **标题**单独成块（一行一块）。
3. **独立公式**单独成块（`$$…$$`）；**行内公式**写 `$…$`，不要单独成块。
4. **表格**整张表成一块；**图注 / 表注**各自单独成块（形如 **Figure 1: …** / **Table 2: …**）。
5. **参考文献条目**逐条成块（保留 `[12]` 这类编号）。
6. **算法伪代码**整块成一块（行尾保留两个空格做 Markdown 硬换行）。
7. 不要把一段拆成两块、也不要把相邻两段合成一块；**不要按字数机械切分**。
8. **分段的依据只有页图**：解析器文本里的空行**不算分段证据** —— 它可能有假空行，也可能把一整段
   拆成好几行。页图上连续的一整段就**必须是一块**；两句之间即使换了行，只要页图上是同一段就不会拆开。
   判断"这是不是新的一段"看**段首缩进 / 段间空隙**这类排版证据。
   ⚠️ 代价很大：两侧各拆各的，第 i 行就会配上**邻行**的内容（整页错位一行，读者一眼就看出对不上）。"""

_SHARED_HEADING_RULES = """【标题层级：必须与【全文标题骨架】一致】
- 骨架里写 `# 1 TURBOQUANT…`，这一页再遇到它就必须用 `#`。
- 骨架里没有的标题按编号深度推断：`1` / `A` / `ABSTRACT` / `REFERENCES` 这类章节级用 `#`，
  `1.1` / `B.2` 用 `##`，`1.1.1` 用 `###`。**同一层级在全文必须一致**，
  不要因为这一页看起来像子节就多加一个 `#`；编号（1、1.1、A.2）原样保留。"""

#: `breaks` 一节的公共模板（`__MD__` / `__TYPES__` / `__SAMPLE__` 由 `_breaks_rules` 替换）。
#:
#: 为什么只有 `start` 没有 `end`：单元在哪里结束是**代码算得出来的**（下一个边界的前一行），
#: 让模型自己再报一个结束位置只会多一个可能错、还要额外校验的字段。用户口径里的"起止位置"
#: 由 `start` + 下一个边界共同确定，比模型自报更稳。
_BREAKS_RULES_TEMPLATE = """【breaks：语义边界锚点（给**检索分块**用，跟渲染无关）】
- 用途：标出「一个完整语义单元说完了、下一个语义单元开始」的位置。知识库按这些边界切分，
  所以**你标在哪里，就决定了检索怎么切** —— 不要按字数机械切，只按语义判断。
- 写法：每条锚点是一个对象，两个字段：
  · `start` = **新语义单元开头那一块的起始片段**，从该段/该标题的**第一个字符**开始
    **逐字照抄** 12~40 个字符（公式符号原样抄，不要改写、不要补省略号），
    要求它能在 __MD__ 里**唯一定位**（同一个片段在本页出现两次就会被判无效）；
  · `type` = 这条边界属于哪一类，取值之一：__TYPES__（拿不准就写 `other`）。
    `type` 只是留给后续「论文理解」的标签，**不参与切分判定** —— 不确定也要给出 `start`。
- **该标**（典型边界）：定义 → 新定义；背景 → 方法；方法 → 实验；问题 → 解决方案；
  一段推导 → 下一段推导；算法描述 → 实验结果；一个独立论点 → 下一个独立论点；
  新小节的**标题行本身**（表示这个小节从这里开始）。
- **不要标**（标了那条锚点会被丢掉，其余锚点照用）：
  · 本页**第一块**（页首没有"上一个语义单元"；跨页那句归上一页管）；
  · 一句话中间、一个连续论证的中间；
  · 公式与紧随其后的解释之间（公式要和它的解释在一起）；
  · 表格标题与表体之间、图注与图的说明之间；
  · 公式内部、表格内部、代码块内部、图内文字内部；
  · 标题行的**下一行**（标题必须和它下面第一段在一起）。
- **语义完整性优先于长度**：不要为了凑长度硬标。一页一般 2~6 条；确实没有自然边界就写 `[]`。
- 例子：__SAMPLE__"""


def _breaks_rules(side_md: str, sample: str) -> str:
    return (_BREAKS_RULES_TEMPLATE
            .replace("__MD__", side_md)
            .replace("__TYPES__", " / ".join(f"`{t}`" for t in BREAK_TYPES))
            .replace("__SAMPLE__", sample))


TRANSCRIBE_SYSTEM = """你是学术论文的「**页面转录**」专家。每次给你一页论文的**页图**与**解析器抽出的页内文本**，
你要输出这一页的原文 Markdown（`original_md`）、给检索分块用的语义边界锚点（`breaks`），
以及**本页坐标**（`src`：与输出块一一对应，写每个输出块覆盖了哪几个解析器块号，
见页尾【本页解析器块清单】与【本页坐标】）。
你**不翻译** —— 中文译文由另一条独立流水线负责，你只对原文负责。

""" + _SHARED_PAGE_IMAGE_RULES + """

""" + _SHARED_SEGMENTATION_RULES + """

""" + _SHARED_HEADING_RULES + """

【original_md：原文转录】
1. 页图是什么语言就原样转录什么语言：**不要翻译、不要添词、不要改写成更通顺的英文**。
2. **每一行都必须逐字来自给你的文本**（【本页】/【页边界参考】/页图上的图注本身）；
   给你的文本里没有的句子、标签、数字组合，哪怕在页图上看得一清二楚，也不要新增成行。
3. 行内公式写 $...$，独立公式写 $$...$$ 并单独成段，编号用 `\\tag{n}`。
   **页图上的任何数学符号都要包进定界符**（连方法名下标也算：`$\\text{TurboQuant}_{\\text{mse}}$`），
   不许裸写 `\\text{…}` / `^{…}` / `_{…}`（前端会原样显示成 `Q_mse^{-1}` 这种文字，用户报过）。
4. **插图**：**只写图注本身**，单独成段：**Figure 1: ...** / **Table 2: ...**，编号必须与页图上实际编号一致
   （页图上是 Figure 2 就写 Figure 2）。
   **图里面的一切文字都不要抄、也不要另起一行转述**：子图标签 (a)/(b)、坐标轴名、刻度、图例、
   图内注释、示例句子、示例表格内容、伪代码清单，全部属于图，不属于正文
   （界面会按插图区域单独裁图呈现，读者看图就能看到）。
   ⚠️ 最容易被误写的两种：
   ① 图内**成段文字**（实测 Attention Is All You Need 附录的注意力可视化图，图内示例句被抄成了一行带
   `<EOS>`/`<pad>` 的英文）；
   ② **图例/子图说明行**（实测 TurboQuant 图 3 的图例被抄成
   `SnapKV (Score: 0.858); PyramidKV (Score: 0.895); …`、图 5 的 `(a) …; (b) … vertical axis: …` 一行）。
   两者一律不要出现在 original_md 里。
5. **表格**：用 Markdown 表格输出（`| 列 | 列 |` + `|---|---|`），**逐格照页图抄写数字与文字**：
   行名、列名、单位、每格数值都要与页图一致；页图上的粗体/分组行用 `**...**` 表示。
   页图上表格过大、无法在一屏内看清的，抄下能看清的行列，其余不要臆造。
6. 参考文献条目逐条保留（保留 [12] 这类编号），不要翻译、不要合并。
7. 丢弃页眉、页脚、页码、脚注横线、出版社水印。
8. **只转录本页页图里能看到的内容**：页尾被版面切断的那半句**照原样停在断处**，
   不要用下一页的内容把它补完（补句是译文那一侧的事）。

""" + _breaks_rules("original_md", '`{"start": "# 3 METHOD", "type": "section"}`') + """

【output：只输出一个 JSON 对象】不要解释、不要代码块：
{"original_md": "……", "breaks": [{"start": "……", "type": "……"}]}"""


TRANSLATE_SYSTEM = """你是学术论文的「**中文翻译**」专家。每次给你一页论文的**页图**与**解析器抽出的页内文本**，
你要输出这一页的中文译文 Markdown（`translation_md`）、跨页承接片段（`carry_md`）、
给检索分块用的语义边界锚点（`breaks`），以及**本页坐标**（`src`：与输出块一一对应，
写每个输出块覆盖了哪几个解析器块号，见页尾【本页解析器块清单】与【本页坐标】）。
你**不转录原文** —— 英文原文由另一条独立流水线负责，你只对译文负责。

""" + _SHARED_PAGE_IMAGE_RULES + """

""" + _SHARED_SEGMENTATION_RULES + """

""" + _SHARED_HEADING_RULES + """

【translation_md：中文译文】
1. 标题的层级与编号必须与原文一致（例：`## 3.2.1 子中心弧面损失`）。
2. 公式的 LaTeX **逐字符原样保留**，不改任何符号；**所有数学都必须写进公式定界符**：
   行内 `$…$`、独立公式 `$$…$$`（单独成段，编号用 `\\tag{n}`）。
   禁止裸写 LaTeX —— `Q_mse^{-1}`、`S^{d-1}`、`\\text{…}`、`\\mathbb{E}`、`R^{d×d}` 这类**必须包在 `$…$` 里**；
   方法名带下标写成 `$\\text{TurboQuant}_{\\text{mse}}$`。公式块里不要塞中文（中文说明放在公式外的段落）。
3. 图注/表注保留编号（例：**图 1：…**）；**表格里的数字、单位、方法名、数据集名原样保留**，
   只翻译行名/列名这类自然语言表头。
4. 参考文献条目**不翻译**：逐条原样保留（与原文一致）。
5. 术语按给定术语表统一；学术书面语；**不增删任何内容**（不加解释、不写"注："、不省略句子）。
6. 丢弃页眉、页脚、页码、脚注横线、出版社水印。
7. **标记不许跨页悬空**：本页打开的 `*`（斜体/加粗）或 `$$` 要在本页闭合。

【前文衔接】prompt 里可能给出**已经处理过的前文结尾**：只用它保持术语、句式、段落衔接一致
（尤其是段落跨页的情况），**不要把它重复输出到本次结果里**。

【跨页衔接：页面只是**排版单位**，不是语义单位】
1. **以自然流畅为先**：译文要读起来像一篇完整的中文论文。一句话该在哪里断句就在哪里断，
   **不要为了迁就页面边界去拆句、补句或改写句子结构**。
2. **页尾那句尽量在本页收成完整句**：原文在页边界被切断时，若【下一页开头】里能看到这句话的剩余部分，
   就用它**把这一整句译完**（一直译到真正的句末：句号/问号/分号等），不要留半句、不要写「……」占位。
   这一句的译文**只在本页出现一次**（下一页不会再译它）。例：
   - 本页末尾原文：`…This latency is primarily attributed to communication bot-`
   - 【下一页开头】：`tlenecks between HBM and SRAM on accelerators, or across distributed clusters.`
   - `carry_md`：`tlenecks between HBM and SRAM on accelerators, or across distributed clusters.`
   - 本页末句译文：`…这种延迟主要归因于通信瓶颈，它出现在加速器上的 HBM 与 SRAM 之间，或跨分布式集群。`
   （被版面折断的 `bot-` 要补成完整词「瓶颈」，不许写成「通信瓶-」或半截词。）
   ⚠ `carry_md` **只放原文**（照抄【下一页开头】里用到的那段，一个字符都不要改、不要翻译、不要加标点），
   它是给**下一页**用的 —— 下一页会把这段原文从自己的正文里删掉，所以**少抄会漏译、多抄会造成重复**。
3. **页首那半句已由上一页译完，本页跳过它**：原文被页边界切断时，本页开头往往是上一页那句的后半截
   （形如 `tlenecks between HBM and SRAM on accelerators, or across distributed clusters.`）。
   **跳过这半句**，从**下一个句子**开始译。同一个词/同一句话**绝不能两页各写一遍**（用户报过「瓶颈」出现两次）。
   判断依据就是原文本身；看不到【前文衔接】时也照此处理。
4. **页内需要时可以用「——」把补充成分接上**；不要用「也就是说」这类重新起句的引导词。
5. **译文里不要输出任何页边界痕迹**：页码、「承上页」「续下页」、分隔线、页注释一律不要。

""" + _breaks_rules("translation_md", '`{"start": "我们定义这个估计量", "type": "definition"}`') + """

【output：只输出一个 JSON 对象】不要解释、不要代码块：
{"translation_md": "……", "carry_md": "……", "breaks": [{"start": "……", "type": "……"}]}

【carry_md：你到底从【下一页开头】借了哪段原文】
- 本页末尾那句靠【下一页开头】补完时，把**你实际用到的那段原文**逐字符照抄进来（原文，不是译文）。
- 没借用（本页句子本来就完整、或下一页开头接不上）就写空字符串 `""`。
- 这段会被**下一页**用来从它的正文里删掉重复部分 —— 照抄准确两边就刚好衔接，抄错就是漏译或重复。"""


#: 按路的输出契约（声明用：`call_page` 只把 system prompt 发给 provider，schema 供测试与文档）
PAGE_SCHEMA_A = {
    "type": "object",
    "properties": {
        "original_md": {"type": "string"},
        "breaks": {"type": "array", "items": {
            "type": "object",
            "properties": {"start": {"type": "string"}, "type": {"type": "string"}},
            "required": ["start"],
        }},
    },
    "required": ["original_md"],
}

PAGE_SCHEMA_B = {
    "type": "object",
    "properties": {
        "translation_md": {"type": "string"},
        "carry_md": {"type": "string"},
        "breaks": {"type": "array", "items": {
            "type": "object",
            "properties": {"start": {"type": "string"}, "type": {"type": "string"}},
            "required": ["start"],
        }},
    },
    "required": ["translation_md"],
}


def path_system_prompt(path: str) -> str:
    """按路取 system prompt：``"a"`` = 英文转录、``"b"`` = 中文翻译。"""
    if path == "a":
        return TRANSCRIBE_SYSTEM
    if path == "b":
        return TRANSLATE_SYSTEM
    raise ValueError(f"未知的路径：{path!r}（只能是 'a' / 'b'）")


#: 两路的 user prompt 模板。与融合模式的 `PAGE_USER_TEMPLATE` 只差两处：
#: A 路**不给【下一页开头】**（转录只写本页看到的内容；给了反而容易把那半句补完），
#: 结尾的"本页输出"按各自的 JSON 字段写。
PAGE_USER_TEMPLATE_A = """{prefix}{previous}

【本页】第 {page} 页 / 共 {page_total} 页{section_line}
【解析器抽出的本页文本（**仅供参考**；与页图冲突时以页图为准）】
<页面文本>
{body}
</页面文本>{edges}

【本页输出】按系统提示输出 JSON：original_md 为本页原文的忠实转录（一切以页图显示的内容为准，
表格逐格照抄、图注编号与页图一致），breaks 为本页的语义边界锚点（没有就写空数组）。{src_tail}"""

PAGE_USER_TEMPLATE_B = """{prefix}{previous}

【本页】第 {page} 页 / 共 {page_total} 页{section_line}
【解析器抽出的本页文本（**仅供参考**；与页图冲突时以页图为准）】
<页面文本>
{body}
</页面文本>{lookahead}{edges}

【本页输出】按系统提示输出 JSON：translation_md 为本页中文译文，carry_md 为本页末尾那句
从【下一页开头】借用的原文片段（没借就写空串），breaks 为本页的语义边界锚点（没有就写空数组）。{src_tail}"""

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

    ## 缺口

    页级翻译按升序**串行**跑（并发设置已移除），正常情况下前文是完整的；
    但单独重跑某几页、或上次中断后跳页续跑时，done 里会留缺口。这里只取
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


def page_lookahead(paper_id: str, page: int, *, lines: int = 14,
                   max_chars: int = 2200) -> str:
    """**下一页开头**的原文片段，作为"仅上下文"塞进本页 prompt。

    为什么需要：页尾被版面折断时（`communication bot-` | `tlenecks …`），只看本页的
    模型无法知道 `bot-` 是什么词，只能照抄半截；把下一页开头给它，它就能把**译文**里的
    词补全（原文转录仍严格只写本页页图内容）。

    ⚠ 必须跳掉"图区噪声"（子图标签 / 坐标轴刻度 / 图片占位）：实测 p20 页尾是
    `…random rotation matrices applied to the`，而 p21 开头是图 5 的整块图区 ——
    旧实现把前 6 个非空行原样塞进去（`(a) TURBOQUANT`、`$$ \times 10 $$`、`![](...png)`），
    真正的续句 `input embedding vectors during…` 根本没进来，模型只能写
    「…矩阵施加于……」这种占位省略号（用户截图报的就是这一行）。
    """
    picked = _leading_lines(paper_id, page + 1, lines=lines, max_chars=max_chars)
    if not picked:
        return ""
    return ("【下一页开头（**仅上下文**：只用来理解本页末尾的句子/断词怎么接下去，"
            "**不要把这里的内容输出**）】\n<下一页文本>\n" + "\n".join(picked) + "\n</下一页文本>")


#: 句末标点（含数理论文里的证毕号 □）：页尾见到这些就不算"半句"
_TERMINAL_PUNCT = tuple(".。!！?？:：;；\"')）】”`…□■▪")
_BREAK_TYPES = ("formula", "table", "code")     # 页尾/页首是这些块 → 不按"断句"处理
#: 页首"图区噪声"：图片占位、坐标轴刻度、子图标签 —— 都不是正文，要找续句就跳过它继续往后看。
_NOISE_LINE = re.compile(
    r"^(?:"
    r"!\["                                                        # 图片 markdown
    r"|\*{0,2}\(?[a-dA-D]\)\*{0,2}[\s$\\{}_a-zA-Z]{0,44}$"       # 子图标签：(a) TURBOQUANT$_{\text{prod}}$
    r"|\$+\s*[\s$\\times×xX\d.,+\-−–()⋅·]*\$*$"                    # 纯数字公式 / 坐标轴刻度（\times 10）
    r"|\*{0,2}[\-−+]?\d+(?:[.\s\-−+]\d+)*\s*(?:\(?[a-dA-D]\)?)?\s*[\\$_{}a-zA-Z]*\*{0,2}$"   # 裸刻度串（含 (b) 混排）
    r")"
)

#: 题注/标题/参考文献这类"结构性"行：可以当上下文给模型，但**不能**当作"续句"的证据。
_STRUCT_LINE = re.compile(
    r"^(?:#{1,6}\s|\$\$|\|"
    r"|\*{0,2}(?:Figure|Fig\.|Table|Algorithm|Listing|图|表|算法)\s*[\dA-Z]"
    r"|[A-Z][a-z]+,\s+[A-Z])"
)


def _is_prose(line: str) -> bool:
    """这一行像不像"正文句子"（续句证据只认它）。"""
    text = line.strip()
    if not text or _NOISE_LINE.match(text) or _STRUCT_LINE.match(text):
        return False
    return sum(1 for ch in text if ch.isalpha()) >= 8


def _leading_lines(paper_id: str, page: int, *, lines: int = 6,
                   max_chars: int = 1600) -> list[str]:
    """该页开头的有效行（跳过图区噪声），按阅读顺序，最多 ``lines`` 行。"""
    try:
        body, _section = page_source_text(paper_id, page)
    except Exception:  # noqa: BLE001  最后一页 / 索引缺失都不值得中断翻译
        return []
    picked: list[str] = []
    size = 0
    for line in (body or "").split("\n"):
        text = line.strip()
        if not text or _NOISE_LINE.match(text):
            continue
        picked.append(text)
        size += len(text)
        if len(picked) >= lines or size >= max_chars:
            break
    return picked


def _starts_lower(line: str) -> bool:
    """这一行是不是"小写字母起头"（= 很可能是上一页那半句的后半截）。"""
    text = line.lstrip(" \t*_$\\([{—–-")
    return bool(text) and text[:1].islower()


#: 句末标点（用于判断"这一行是不是把句子说完了"）
_SENT_END = ".。!！?？:：;；"


def _ends_sentence(line: str) -> bool:
    """这一行是否以句末标点收尾（Markdown 尾巴 `*` / `$` / 引号先剥掉）。"""
    text = line.strip().rstrip("*_`）)】”\"'")
    return bool(text) and text[-1] in _SENT_END


def _leading_lines(paper_id: str, page: int, *, lines: int = 14,
                   max_chars: int = 2200) -> list[str]:
    """该页开头的有效行（跳过图区噪声），按阅读顺序。

    额外规则：如果最后一行还没收句，就继续多要几行 —— 跨页那句往往在图/算法块后面才收尾，
    拿不到句尾，上一页就只能补半句、下一页再译一遍（实测 p5→p6）。
    """
    try:
        body, _section = page_source_text(paper_id, page)
    except Exception:  # noqa: BLE001  最后一页 / 索引缺失都不值得中断翻译
        return []
    kept = [line.strip() for line in (body or "").split("\n")
            if line.strip() and not _NOISE_LINE.match(line.strip())]
    picked: list[str] = []
    size = 0
    for text in kept:
        picked.append(text)
        size += len(text)
        if len(picked) >= lines or size >= max_chars:
            break
    # 没收句：再多看几行，直到看到句末标点 / 硬上限
    if picked and not _ends_sentence(picked[-1]):
        extra = 0
        for text in kept[len(picked):]:
            picked.append(text)
            size += len(text)
            extra += 1
            if _ends_sentence(text) or extra >= 8 or size >= max_chars + 900:
                break
    return picked


def _continuation_line(paper_id: str, page: int, *, lines: int = 14,
                       max_chars: int = 2200) -> str:
    """该页开头**第一行"小写起头的正文"** —— 有它才敢说"下页开头在接着本页那半句"。

    只看 ``page_lookahead`` 用的**同一个窗口**：判定与提示词必须一致，
    否则会出现"提示词让它用【下一页开头】补完，可窗口里根本没那半句"。
    跳过：图区噪声（子图标签/坐标轴刻度/图片）、题注与标题（结构行）、大写起头的新句。
    """
    for line in _leading_lines(paper_id, page, lines=lines, max_chars=max_chars):
        if _is_prose(line) and _starts_lower(line):
            return line
    return ""


def drop_declared_fragment(source: str, fragment: str, *, scan_chars: int = 3000) -> str:
    """把下一页的正文里"上一页已译完的那半句"删掉。

    片段是**上一页自己在 ``carry_md`` 里申报的**（它从【下一页开头】借用了哪段原文），
    不是启发式猜的 —— 启发式会把 `**Algorithm 1 …: optimized for MSE**` 这种题注也能误剪
    （实测），而申报的片段只在真发生跨页续句时才非空。

    空白不敏感匹配（模型偶尔把换行抄成空格）；找不到 / 位置太后就原样返回（宁重勿丢）。
    """
    text = source or ""
    frag = (fragment or "").strip()
    if not text or not frag:
        return text
    pattern = re.compile(r"\s+".join(re.escape(tok) for tok in frag.split()))
    match = pattern.search(text[:scan_chars])
    if match is None:
        return text
    return text[match.end():].lstrip()


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
    # 图区顶在页首/页尾时，上面按"块"判会漏：p21 开头是图 5 的子图标签+刻度（被当成 caption/ref），
    # 真正的续句 `input embedding vectors during…` 在图区后面。所以再加一层**正文回退**：
    # 只要本页/上页那半句没有句末标点，且对面那页开头的**第一行正文**是小写起头，就认它是续句。
    if not tail_continues and tail_text and not tail_text.endswith(_TERMINAL_PUNCT):
        tail_continues = bool(_continuation_line(paper_id, page + 1))
    if not head_continues and prev_text and not prev_text.endswith(_TERMINAL_PUNCT):
        head_continues = bool(_continuation_line(paper_id, page))
    return {"head": head_continues, "tail": tail_continues}


#: 续句开头常见的**占位噪声**（模型被"页尾半截"误导时会写这些）。
#: 注意**不包含「——」**：当上一页确实停在句中时，「——它出现在…」是合法的同位语衔接，
#: 剥掉反而会把两句粘成病句（实测「…通信瓶颈——它出现在加速器上…」是最好的读法）。
_LEAD_JUNK = re.compile(r"^\s*(?:（?承?接上页）?|\(?承?接上页\)?|【接上页】|…+|\.{2,})+")


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
    if rows:
        # 页尾的「……」是"知道被切断却没接上"的占位（用户截图：…矩阵施加于……）。
        # 它不是内容，一律剥掉 —— 读时处理，所以**存量产物不用重译**也已经干净了。
        rows[-1]["translation"] = re.sub(
            r"[.．…]{2,}\s*$", "", str(rows[-1].get("translation") or "")
        ).rstrip()


def render_page_prompt(*, page: int, page_total: int, body: str,
                       prefix: str = "", section: str = "", previous: str = "",
                       lookahead: str = "", edges: dict | None = None,
                       path: str | None = None, src_tail: str = "") -> str:
    """渲染一页的 user prompt。

    `path`：``None`` = 融合模式（`PAGE_USER_TEMPLATE`）；``"a"`` = 英文转录（**不给**下一页开头）；
    ``"b"`` = 中文翻译（给）。

    ``src_tail``（P4b 共享坐标契约）：**只追加在页面级尾部**（`【本页输出】` 之后），
    稳定前缀（system + 简报 + 骨架 + 前文）逐字节不变，所以前缀缓存照旧命中。
    """
    if path is None:
        template = PAGE_USER_TEMPLATE
    elif path == "a":
        template = PAGE_USER_TEMPLATE_A
        lookahead = ""      # A 路转录只写本页看到的内容，不需要下一页上下文
    elif path == "b":
        template = PAGE_USER_TEMPLATE_B
    else:
        raise ValueError(f"未知的路径：{path!r}（只能是 'a' / 'b'）")
    section_line = f"；章节：{section}" if section else ""
    return template.format(
        prefix=prefix, previous=f"\n\n{previous}" if previous else "",
        page=page, page_total=page_total,
        section_line=section_line, body=body or "（解析器没有抽到文本，请完全依据页图）",
        lookahead=f"\n\n{lookahead}" if lookahead else "",
        edges=render_edge_facts(edges or {}),
        src_tail=src_tail,
    )


#: 页边界参考块的开头（供测试/排查引用）
EDGE_FACTS_HEADER = "【页边界参考"


def render_edge_facts(edges: dict) -> str:
    """把解析器判定的页边界事实写进 prompt（键名与 `page_continuity` 一致：head / tail）。

    为什么给这个参考："本页末尾是不是半句" 在页图上并不显眼（往往只是没写句号），
    模型拿不准时容易把半句补成一个完整句，于是同一句被拆成两句（用户报过）。
    但**它只是参考** —— 以自然流畅为先，别为了迁就页边界去拆句。
    """
    if not edges:
        return ""
    head = ("承接上一页的句子（原文上半截在上一页；**这句已由上一页译完，本页跳过它、从下一句开始**）"
            if edges.get("head") else "新起句子")
    tail = ("句子在本页被截断（**请用【下一页开头】把这句在本页译成完整句**，不要留半句；"
            "若【下一页开头】里看不到这句的后半截（全是图/表/公式或不相关内容），"
            "就把这句停在断处 —— 不要写「……」，不要硬凑句号，也不要自己编后半句）"
            if edges.get("tail") else "本页句子已结束（页尾正常收句）")
    return (f"\n\n{EDGE_FACTS_HEADER}（解析器判定，**仅供参考**：用来看句子在哪里被页边界切开，"
            f"别为它牺牲中文的自然断句）】"
            f"\n- 本页开头：{head}\n- 本页末尾：{tail}")


# --------------------------------------------------------------------------
# 调用
# --------------------------------------------------------------------------


@dataclass
class PageCallResult:
    original_md: str
    translation_md: str
    carry_md: str = ""
    #: 语义边界锚点 `[{"start", "type"}]` —— 只服务检索分块，见 SYSTEM_PROMPT / TRANSCRIBE_SYSTEM / TRANSLATE_SYSTEM【breaks】
    breaks: list[dict] = field(default_factory=list)
    #: P4b 共享坐标契约：与输出块**一一对应**的解析器块号覆盖（见 `coords`）
    src: list[str] = field(default_factory=list)
    model: str = ""
    prompt_tokens: int = 0
    completion_tokens: int = 0
    reasoning_tokens: int = 0
    latency_ms: float = 0.0
    max_tokens: int = 0
    #: DeepSeek 前缀缓存：命中 / 未命中的 prompt token 数（用于验证"逐页追加式前文"真的吃到缓存）
    cache_hit_tokens: int = 0
    cache_miss_tokens: int = 0


def call_page(provider, prompt: str, image: bytes | None, *,
              max_tokens: int, path: str | None = None) -> PageCallResult:
    """一次页级调用（带页图）。图片不可用时退化为纯文本调用。

    `path`：``None`` = 融合模式（同一次回复产出 original_md + translation_md）；
    ``"a"`` / ``"b"`` = 两路模式，只要求**那一路**的输出（另一侧留空）。
    system prompt 按路取 —— 两路各有自己的提示前缀，前缀缓存也各算各的。
    """
    system = SYSTEM_PROMPT if path is None else path_system_prompt(path)
    try:
        if image and getattr(provider, "supports_vision", False):
            res = provider.generate_vision(prompt, [image], system=system,
                                           max_tokens=max_tokens)
        else:
            res = provider.generate(prompt, system=system,
                                    temperature=0.2, max_tokens=max_tokens)
    except ProviderError:
        raise
    except Exception as exc:  # noqa: BLE001
        raise ProviderError(f"页级翻译调用失败：{str(exc)[:200]}") from exc

    original, translation = parse_pair(res.text or "")
    # 图内文字兜底：提示词管新产物，"把图里画出来的字当正文"这种已产出的行在这里丢掉（见函数注释）
    original, noise_a = strip_figure_noise(original)
    translation, noise_b = strip_figure_noise(translation)
    if noise_a or noise_b:
        log.info("丢掉图内文字 %d 行（%s 侧 %d / %s 侧 %d）：图里画出来的句子/标记不进正文",
                 noise_a + noise_b, "A", noise_a, "B", noise_b)
    if path == "a":
        if not original.strip():
            raise ProviderError("模型未返回 original_md（A 路：页面转录）", retryable=True)
    elif path == "b":
        if not translation.strip():
            raise ProviderError("模型未返回 translation_md（B 路：中文翻译）", retryable=True)
    elif not original.strip() or not translation.strip():
        raise ProviderError(
            "模型未返回完整的 original_md / translation_md（页级调用）",
            retryable=True,
        )
    usage_raw = (getattr(res, "raw", None) or {}).get("usage") or {}
    return PageCallResult(
        original_md=original.strip(), translation_md=translation.strip(),
        carry_md=parse_carry(res.text or ""),
        breaks=parse_breaks(res.text or ""),
        src=_coords().parse_src(res.text or ""),
        model=getattr(res, "model", "") or provider.model_name,
        prompt_tokens=int(getattr(res, "prompt_tokens", 0) or 0),
        completion_tokens=int(getattr(res, "completion_tokens", 0) or 0),
        reasoning_tokens=int(getattr(res, "reasoning_tokens", 0) or 0),
        latency_ms=float(getattr(res, "latency_ms", 0.0) or 0.0),
        max_tokens=max_tokens,
        cache_hit_tokens=int(usage_raw.get("prompt_cache_hit_tokens") or 0),
        cache_miss_tokens=int(usage_raw.get("prompt_cache_miss_tokens") or 0),
    )


def _coords():
    """延迟导入 `coords`（它反向引用本模块的 `split_blocks`，模块级导入会成环）。"""
    from backend.services.translation import coords

    return coords


def page_source_blocks(paper_id: str, page: int) -> list[dict]:
    """该页的**解析器块**（P4b 共享坐标）：两路拿到同一份，且与两路产物无关。

    空文本块不返回（它们不参与坐标）。顺序按解析器的 `order`。
    """
    from backend.services.paper import reader_service

    index = reader_service.load_block_index(paper_id)
    rows = [b for b in (index.get("blocks") or [])
            if int(b.get("page") or 0) == int(page) and (b.get("text") or "").strip()]
    rows.sort(key=lambda b: b.get("order") or 0)
    return rows


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


def parse_carry(text: str) -> str:
    """从模型回复里取 ``carry_md``（本页从【下一页开头】借用的那段**原文**）。容错，缺就空串。"""
    data = _loads_lenient((text or "").strip())
    if isinstance(data, dict):
        value = data.get("carry_md")
        return str(value).strip() if isinstance(value, str) else ""
    return ""


def normalize_break_type(value) -> str:
    """把模型自报的 `type` 归一成 `BREAK_TYPES` 里的一个。

    未知类型 → ``"other"``；空值 → ``""``（表示"模型没给类型"）。
    **绝不因为分类不认识就把这条锚点丢掉**：锚点本身的价值远大于它的分类。
    """
    text = str(value or "").strip().lower()
    if not text:
        return ""
    return text if text in BREAK_TYPES else "other"


def normalize_breaks(raw) -> list[dict]:
    """把 `breaks` 归一成 ``[{"start": str, "type": str}]``。

    **必须兼容 v1**（纯字符串数组）：`md/pages.json` 里躺着的存量产物就是那个形状，
    而 `breaks` 是**原样留档**的（换模型重译时要拿它重新落回行，不用回头看模型的原始回复）。
    v1 条目等价于 ``{"start": s, "type": ""}``。
    """
    if raw is None:
        return []
    items = [raw] if isinstance(raw, (str, dict)) else list(raw)
    out: list[dict] = []
    for item in items:
        if isinstance(item, dict):
            start = str(item.get("start") or "").strip()
            btype = normalize_break_type(item.get("type"))
        else:
            start = str(item or "").strip()
            btype = ""
        if start:
            out.append({"start": start, "type": btype})
    return out


def parse_breaks(text: str) -> list[dict]:
    """从模型回复里取 ``breaks``（语义边界锚点）并归一。容错：缺字段、类型不对、夹空串都不报错。

    为什么容错到底：锚点是**加分项** —— 它缺失或写坏只应该让这一页退回"按块机械切分"，
    绝不能让整页翻译作废（重跑一页视觉模型很贵，而且页级任务是逐页落盘的）。
    """
    data = _loads_lenient((text or "").strip())
    if not isinstance(data, dict):
        return []
    return normalize_breaks(data.get("breaks"))


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
        # ⚠️ 行尾那两个空格是 Markdown 的**硬换行**标记，不能跟着 rstrip 一起剥掉：
        # 算法步骤、作者单位这类"一块里每行各自成行"的内容全靠它。剥掉之后，块文本里的
        # 单换行就成了软换行，前端 marked 会把整块折成一段 —— 表现是「Markdown 视图正常，
        # 对照 / 原文 / 译文里糊成一坨」（用户报过：算法 1 的 11 行粘成一段）。
        # 代码围栏里是字面量，不补。
        if mode != "code" and line and raw.endswith("  "):
            line += "  "
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


_MATH_SPAN = re.compile(r"\$([^$]+)\$", re.DOTALL)
#: 引用年份 `Shannon (1948)` / `Achiam et al. (2023)`：两侧都必须原样保留英文作者与年份
_CITE_YEAR = re.compile(r"\((\d{4}[a-z]?)\)")
#: 全大写缩略语：两侧都原样保留，是跨语言最密的对位证据（用来区分层级编号相同的同级标题）。
_CAPS_WORD = re.compile(r"\b[A-Z][A-Z0-9\-]{3,}\b")


def anchor_markers(text: str) -> tuple[str, ...]:
    """这一块里的**跨语言稳定标记**：公式片段 + 引用年份 + 全大写缩略语。

    为什么需要它：`pair_rows` 在两侧块数不等时靠 `_row_key` 做结构对齐，而"段落/标题"这两类
    如果只用类型（+层级编号）当键，`SequenceMatcher` 就**看不出多出来的那一段该插在哪**，
    也分不清两个同级标题谁是谁 —— 实测 turbo p1 因此把英文标题配到中文摘要上、并从第 4 行起
    整体错位一行（读者看到的每行都配着邻行译文）。

    公式、引用年份与全大写缩略语在两侧必须逐字保留（提示词硬要求），所以它们是可靠的对位证据。
    没有标记的块仍然退回纯类型键 —— 那类块本来就无从区分。
    """
    text = text or ""
    marks: list[str] = []
    for m in _MATH_SPAN.finditer(text):
        body = re.sub(r"\s+", "", m.group(1))
        if body:
            marks.append(f"m:{body[:48]}")
    for m in _CITE_YEAR.finditer(text):
        marks.append(f"y:{m.group(1)}")
    for m in _CAPS_WORD.finditer(text):
        marks.append(f"c:{m.group(0)}")
    return tuple(marks)


def _row_key(block: dict) -> tuple:
    """配对用的块签名 —— **别用正文文本**。

    中英文本永不相同，拿 `text[:40]` 当签名会让 `SequenceMatcher` 对正文全部退化成
    "按序号替换"，于是只要一侧少一块，后面全部**错位一行**（实测 p19：ZH 少的是
    上一页已译完的承接句，结果「证明。…」被挂到了上一条 bullet 上，最后一行空成「未翻译」）。

    所以：公式用 LaTeX 文本（两边一字不差，是真锚点），标题用层级 + 编号，
    其余只用**块类型** —— 序列对齐就变成纯结构对齐，错位自然消失。

    唯一的例外是**编号步骤块**（算法伪代码那种，见 `_is_step_block`）：它和正文段落都是
    `para`，只按类型配不出区别。实测 p006：EN 比 ZH 多一段正文（定理那句在上一页已译完），
    `SequenceMatcher` 于是"删掉"先出现的算法块、让后一段正文去配算法译文 ——
    对照时算法左右不挨着，单栏（译文）里还留下一个空行。给它一个独立的键即可；
    这类块两侧的行数与序号天然一一对应，是最可靠的锚点。

    第二个例外是**跨语言稳定标记**（`anchor_markers`：公式片段 + 引用年份）。纯类型键下
    "多出来的那一段该插在哪"是**不可判定**的，匹配器于是插在页尾 → 整页错位一行（实测 p1）。
    带上标记之后插入能被定位到正确位置。没有标记的块仍然只按类型配（无从区分，只能如此）。
    """
    btype = block.get("type") or "para"
    text = (block.get("text") or "").strip()
    if btype == "heading":
        return ("h", int(block.get("level") or 0), block.get("number") or "")
    if btype == "formula":
        return ("f", _norm(text)[:60])
    if btype == "para" and _is_step_block(text):
        return (btype, "steps")
    marks = anchor_markers(text)
    if marks:
        return (btype, marks)
    return (btype,)


# 「1: 」「2. 」「3、」这类行首序号（算法伪代码 / 编号清单）
_STEP_LINE = re.compile(r"^\s*\d+\s*[:：.、)）]")


def _is_step_block(text: str) -> bool:
    """多行、且至少两行以序号开头 → 当作编号步骤块（算法伪代码）。

    阈值故意松（两行即可），因为**假阳性代价很小**：两侧都是算法块时它照样配对成功；
    真正要避免的是"一侧被误判成步骤块、另一侧没有" —— 那需要给译文里的普通段落也配上
    两行序号的排版，正常翻译不会有。
    """
    lines = [ln for ln in (text or "").split("\n") if ln.strip()]
    if len(lines) < 2:
        return False
    return sum(1 for ln in lines if _STEP_LINE.match(ln)) >= 2


def _norm(text: str) -> str:
    return re.sub(r"\s+", " ", (text or "").lower()).strip()


# --------------------------------------------------------------------------
# 语义边界锚点（breaks）→ 行边界
# --------------------------------------------------------------------------
#: 锚点规范化后至少要有多长才算"能唯一定位"（太短的锚点会撞上别处 —— 宁可判非法回退）
MIN_BREAK_ANCHOR_KEY = 12
#: 短锚点的下限：低于这个长度连标题行都不认（`# 6 Results` 规范化后只有 8 个字符，是合法标题）
MIN_BREAK_ANCHOR_HEADING_KEY = 6
#: 结构块类型：表格 / 表注图注 / 图 —— 相邻的这类块构成"不许切开的连续结构"
_STRUCT_TYPES = ("table", "caption", "figure")
_ANCHOR_STRIP = re.compile(r"[^0-9a-z\u4e00-\u9fff]+")
#: 合法的句末字符（段落尾）：句末标点，或右括号/引号（`…(see Fig. 3)` 也算收句了）
_TAIL_OK = "。！？；：.!?;:）)』】]》」”’\"'"
#: 段落尾还要先剥掉这些 Markdown 装饰符再看最后一个字符（`… **` 的句子其实收在 `*` 之前）
_TAIL_TRIM = "*_` \t"


def anchor_key(text: str) -> str:
    """锚点 / 块首的规范化键：只留小写字母、数字、汉字。

    和 `anchor_map` 的块签名同一口径（只留字母数字汉字）：模型照抄锚点时层级符、空格、
    标点都可能与块文本有出入，压掉这些差异之后仍能唯一定位。
    """
    return _ANCHOR_STRIP.sub("", (text or "").lower())


def apply_semantic_breaks(rows: list[dict], breaks: list | None) -> dict:
    """把 LLM 的语义边界锚点落成**行边界标记**（``row["semantic_break"] = True``）。

    v2 还会把类型标签落到 ``row["semantic_type"]``（可选；只给「论文理解」用，不参与切分）。

    这是 marker 的**校验层**：锚点不可信，必须逐条验证。任何一条不合法只丢它自己，
    这一页剩下的锚点照用；全部不合法就等于"按块机械切分"（旧行为）。
    宁可少一条边界，也不许切点落在句子中间、公式内部或表格中间。

    合法锚点要同时满足：
    1. 规范化后 ≥ `MIN_BREAK_ANCHOR_HEADING_KEY` 个字符，且能在**某一行原文的块首**唯一匹配
       （原文匹配不到时退回译文块首 —— 模型偶尔给中文锚点；撞车则一律判非法，不猜）。
       短于 `MIN_BREAK_ANCHOR_KEY` 的锚点**只允许落在标题行**：标题本来就短
       （`# 6 Results` 规范化后只有 8 个字符），只认标题行就不会撞上正文段落的开头，
       判定仍然精确；落在正文段上的短锚点一律拒（原因 `short_not_heading`）；
    2. 不是第 0 行（页首没有"上一个语义单元"，跨页那句归上一页管）；
    3. 前一行不是标题（标题必须和它下面的第一段在一起）、不是公式（公式要与解释在一起）、
       不以冒号结尾（`… as follows:` 这种是在引出下一块）；
    4. 不在结构块内部切（表格 / 表注 / 图注相邻处不许切）；
    5. 两侧都是正文时，前一行要**真的收句了**（句末标点），否则就是切在一句话中间。

    `breaks` 可以是 v2 的对象数组，也可以是 v1 的纯字符串数组（存量产物）。
    两路拆分后这个方法**同时**吃两路的锚点：英文锚点落原文侧、中文锚点落译文侧
    （`_locate_anchor` 先原文后译文），并集天然成立，不需要"按路存标记"。
    """
    entries = normalize_breaks(breaks)
    stats: dict = {"anchors": len(entries), "accepted": 0, "duplicate": 0,
                   "rejected": [], "by_side": {"original": 0, "translation": 0},
                   "short_heading": 0, "flagged_rows": 0,
                   # v2：类型标签（只做标签，不参与切分判定）
                   "typed": 0, "by_type": {}}
    for row in rows:
        row.pop("semantic_break", None)
        row.pop("semantic_type", None)
        row.pop("semantic_break_side", None)
    if not rows or not entries:
        return stats

    wanted: dict[int, dict] = {}
    for entry in entries:
        anchor = entry["start"]
        key = anchor_key(anchor)
        if len(key) < MIN_BREAK_ANCHOR_HEADING_KEY:
            stats["rejected"].append({"anchor": anchor[:40], "reason": "too_short",
                                      "side": ""})
            continue
        heading_only = len(key) < MIN_BREAK_ANCHOR_KEY
        side, idx, why = _locate_anchor(rows, key, heading_only=heading_only)
        if side is None:
            stats["rejected"].append({"anchor": anchor[:40], "reason": why, "side": ""})
            continue
        reason = _break_reject_reason(rows, idx, side)
        if reason:
            # 记下**匹配到哪一侧**：`by_side` 只统计"被接受的"，光看它分不清
            # "落在译文侧但位置不合法"和"压根没匹配上"。
            stats["rejected"].append({"anchor": anchor[:40], "reason": reason,
                                      "side": side})
            continue
        stats["by_side"][side] += 1
        if heading_only:
            stats["short_heading"] += 1
        if idx in wanted:
            stats["duplicate"] += 1
            continue
        wanted[idx] = {**entry, "side": side}
    for idx, entry in sorted(wanted.items()):
        rows[idx]["semantic_break"] = True
        # **哪一路标的**：分块层按路取边界的关键（A 路的锚点是英文、B 路的是中文，
        # 于是两路各得到一套自己的单元划分 —— 这正是"两路 Chunk 允许不同"的实现方式）。
        rows[idx]["semantic_break_side"] = entry["side"]
        if entry["type"]:
            rows[idx]["semantic_type"] = entry["type"]
    stats["accepted"] = len(wanted)
    stats["flagged_rows"] = len(wanted)
    stats["typed"] = sum(1 for e in wanted.values() if e["type"])
    for entry in wanted.values():
        if entry["type"]:
            stats["by_type"][entry["type"]] = stats["by_type"].get(entry["type"], 0) + 1
    return stats


def _locate_anchor(rows: list[dict], key: str, *, heading_only: bool = False
                   ) -> tuple[str | None, int, str]:
    """锚点 → （匹配到哪一侧, 行下标, 失败原因）。只认**块首**匹配：锚点定义为"新单元开头那块的起始片段"。

    同一侧命中多行 = 锚点太普适（如 `In this section`），一律判非法 —— 猜错会把边界落到
    完全无关的位置，比少一条边界坏得多。`heading_only=True`（短锚点）时只在标题行里找。
    """
    for side, name in (("original", "original"), ("translation", "translation")):
        hits = [i for i, r in enumerate(rows)
                if (not heading_only or (r.get("type") or "") == "heading")
                and anchor_key(r.get(name) or "").startswith(key)]
        if len(hits) == 1:
            return side, hits[0], ""
        if len(hits) > 1:
            return None, -1, "ambiguous"
    return None, -1, ("short_not_heading" if heading_only else "unmatched")


def _break_reject_reason(rows: list[dict], idx: int, side: str = "original") -> str:
    """这个边界位置上允不允许切（返回空串 = 允许）。判据见 `apply_semantic_breaks` 的 2~5 条。

    `side` = 锚点匹配到哪一侧（`_locate_anchor` 的返回值）：**"前一行收句了吗"要用锚点自己
    那一种语言的文本去判**。两路拆分后这一点变成硬要求 —— A 路的英文锚点用英文侧判、
    B 路的中文锚点用中文侧判；拿英文的标点去判中文块，会出现"中文块以「：」结尾却被放行"
    这种漏判。融合模式（锚点全是英文、落在原文侧）行为与改造前**逐字相同**。
    """
    if idx <= 0:
        return "page_start"
    prev, cur = rows[idx - 1], rows[idx]
    ptype = prev.get("type") or "para"
    ctype = cur.get("type") or "para"
    if ptype == "heading":
        return "after_heading"
    if ptype == "formula":
        return "after_formula"
    if ptype in _STRUCT_TYPES and ctype in _STRUCT_TYPES:
        return "inside_structure"
    field = "translation" if side == "translation" else "original"
    prev_text = (prev.get(field) or "").strip()
    if prev_text.endswith((":", "：")):
        return "dangling_colon"
    if ptype == "para" and ctype == "para":
        tail = prev_text.rstrip(_TAIL_TRIM)
        if tail and tail[-1] not in _TAIL_OK:
            return "mid_sentence"
    return ""


# --------------------------------------------------------------------------
# 页产物
# --------------------------------------------------------------------------


def build_page_payload(*, page: int, page_total: int, result: PageCallResult,
                       source_chars: int = 0, cached: bool = False,
                       align_override: str | None = None) -> dict:
    rows, quality = pair_rows(result.original_md, result.translation_md, page)
    # 语义边界锚点在这里就落到行上（读时 `page_translations` 会用同一个函数再落一次：
    # 行是读时重算的，标记必须跟着行一起重算，否则重算出来的行会丢掉标记）。
    break_stats = apply_semantic_breaks(rows, result.breaks)
    return {
        "page": page,
        "page_total": page_total,
        "original_md": result.original_md,
        "translation_md": result.translation_md,
        "rows": rows,
        # 原始锚点**原样存盘**：行 id 是位置编号，将来若块数变化（换模型重译），
        # 锚点还能重新落到新行上，不用回头看模型的原始回复。
        "breaks": list(result.breaks),
        "break_stats": break_stats,
        "align": align_override or quality,
        "carry_md": result.carry_md,
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


def build_path_payload(*, path: str, page: int, page_total: int,
                       result: PageCallResult, source_chars: int = 0,
                       cached: bool = False) -> dict:
    """**一路**的页产物：A 只存 `original_md`，B 只存 `translation_md` + `carry_md`。

    为什么**不存 `rows`**：单路只有一侧内容，存下来的"行"必然全是单侧行；两路各存一份
    还会互相矛盾（谁说了算？）。行由 `reader_service.page_products` 在**读时**把两路合并出来
    —— 那里同时看得到两侧，`pair_rows` 才谈得上对齐。

    也不存 `align`：那是"两侧配得准不准"的度量，单路没有意义。读时算出来的合并质量
    才是前端要的那个数。
    """
    if path not in PATHS:
        raise ValidationError(f"未知的路：{path!r}（只能是 'a' / 'b'）")
    payload = {
        "page": page,
        "page_total": page_total,
        "path": path,
        # 锚点**原样留档**（v2 对象数组）：行是读时重算的，锚点要能重新落回新行上
        "breaks": list(result.breaks),
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
    if path == "a":
        payload["original_md"] = result.original_md
    else:
        payload["translation_md"] = result.translation_md
        # `carry_md` 是翻译侧独有的：本页从【下一页开头】借走的那段原文，下一页用它去重
        payload["carry_md"] = result.carry_md
    return payload


#: 图内文字的特征标记：这些 token 只会出现在"图里画的示例句 / 训练标记"里，论文正文不会出现
_FIGURE_NOISE_RE = re.compile(r"</?(?:eos|pad|unk|bos)>|</?s>", re.IGNORECASE)


def strip_figure_noise(md: str) -> tuple[str, int]:
    """丢掉"把图里画出来的文字当正文"的行；返回 ``(清洗后的 markdown, 丢掉的行数)``。

    实测（attention 附录 图 3 / 图 4 注意力可视化）：**B 路**把图内那句示例英文
    `It is in this spirit that … more difficult . <EOS> <pad> <pad> …` 当正文译了出来，
    中文栏于是多出一行英文 + `<EOS> <pad>`；A 路因为提示词里有"只写图注本身"所以是干净的。

    判定刻意**窄**：只有行里出现 `<EOS>` / `<pad>` / `<unk>` / `<s>` / `</s>` 这类训练标记才丢
    —— 论文正文不可能出现它们，所以不会误伤正常句子（含公式、引用、单位标记的行都不受影响）。
    提示词侧已同步补了"图/表内部文字不是正文"这条规则（双保险：提示词管新产物，这里管已产出的）。
    """
    if not md or "<" not in md:
        return md, 0
    kept: list[str] = []
    dropped = 0
    for line in md.split("\n"):
        if _FIGURE_NOISE_RE.search(line):
            dropped += 1
            continue
        kept.append(line)
    if not dropped:
        return md, 0
    # 丢行后可能出现连续 3+ 空行：Markdown 靠空行分段，多余空行会被渲染成空段
    return re.sub(r"\n{3,}", "\n\n", "\n".join(kept)).strip(), dropped


def absorb_one_sided_rows(rows: list[dict], page: int) -> dict:
    """把"只有一侧有内容"的行并进**上一行**的缺失侧（读时归一，不动产物）。

    为什么需要它：两路各自按页图分段，块数经常差 1~2 段（实测 22 页里 4~6 页），
    `pair_rows` 于是留下几行"只有中文/只有英文"。**这些差异绝大多数不是"一侧真的少了内容"，
    而是"一侧把相邻两段并成了一块"** —— 实测 turbo p1：A 把摘要里相邻两句并成一块、B 拆成两块。
    并进上一行的对应侧之后，两侧重新覆盖同一段文字，阅读器不会再出现"英文栏是空的"。

    为什么并进**上一行**而不是下一行：`pair_rows` 的插入点就在那一段分歧处，而"被并进的那一块"
    在另一侧是**和上一块连在一起的**（差分对齐给出的最短解释）。

    页首的单侧行没有上一行可并 → 原样保留（那确实是两侧各自多出的开头）。
    吸收的行连同它的行号一起消失，所以**行 id 会重排**（`p{页}-{序号}` 本来就是位置编号）；
    本函数在 `apply_semantic_breaks` **之前**调用，语义边界按合并后的行重新落一遍。

    ⚠️ **整页缺一侧时不吸收**（过渡态：只跑完 A 路那一侧还没有产物）。否则每一行都"缺同一侧"，
    会被连锁并进第一行，整页的行结构直接塌掉（有单测钉住这一条）。

    返回 ``{"rows": [...], "absorbed": n}``。
    """
    page_has_en = any((r.get("original") or "").strip() for r in rows)
    page_has_zh = any((r.get("translation") or "").strip() for r in rows)
    out: list[dict] = []
    absorbed = 0
    for row in rows:
        has_en = bool((row.get("original") or "").strip())
        has_zh = bool((row.get("translation") or "").strip())
        missing_side_present = (page_has_zh if not has_zh else page_has_en)
        if out and has_en != has_zh and missing_side_present:
            prev = out[-1]
            if has_en:
                prev["original"] = f"{prev.get('original') or ''}\n\n{row['original']}".strip()
            else:
                prev["translation"] = (f"{prev.get('translation') or ''}\n\n"
                                       f"{row['translation']}").strip()
            absorbed += 1
            continue
        out.append(dict(row))
    for i, row in enumerate(out):                 # 行 id/序号按新位置重排（位置编号）
        row["id"] = f"p{int(page):03d}-{i:02d}"
        row["index"] = i
    return {"rows": out, "absorbed": absorbed}


class PageStore:
    """页级产物文件的读写（逐页原子落盘 → 中断可续跑）。

    一个实例对应**一个文件**：旧格式的合并产物（`md/pages.json`）或某一路的产物
    （`md/path_a.json` / `md/path_b.json`，见 `paper_service.paper_paths`）。
    `version` 由调用方给：两路产物与旧合并产物的结构不同，版本号不该混用。
    """

    def __init__(self, paper_id: str, path: Path | None = None, *,
                 version: int = PAGES_VERSION) -> None:
        self.paper_id = paper_id
        self.path = path
        self.version = int(version)
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
                    "version": int(data.get("version") or self.version),
                    "page_total": int(data.get("page_total") or 0),
                    "model": data.get("model") or "",
                    "updated_at": data.get("updated_at"),
                    "pages": {int(k): v for k, v in pages.items()
                              if isinstance(v, dict)},
                }
            else:
                self._data = {"paper_id": self.paper_id, "version": self.version,
                              "page_total": 0, "model": "", "updated_at": None,
                              "pages": {}}
            return self._data

    def flush(self) -> None:
        with self._lock:
            data = self._load()
            data["version"] = self.version
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
        """已有产物的页。

        判据要同时认两侧：融合模式看 `rows` / `translation_md`，**A 路产物只有 `original_md`**
        （两路拆开后每路只存自己那一侧），漏掉它会让"已转录的页"永远算没做完、反复重跑。
        """
        return sorted(p for p, v in self._load()["pages"].items()
                      if (v.get("rows") or v.get("translation_md")
                          or v.get("original_md")))

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
            # 与 `done_pages()` 共用同一条判据（含 `original_md`：A 路产物只有原文，
            # 漏掉它会让"已转录的页"既算不出完成、又反复重跑）
            "pages_done": len(self.done_pages()),
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
