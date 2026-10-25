"""知识库服务（产线 C：chunk 化 → 向量化 → 入库）。

规划 §11 / §21.6：chunk 化 → Embedding → 写入 MySQL chunk 元数据 → 写 LanceDB。
检索（向量/词法/混合/重排/RAG 问答）在阶段 14–17 的后续轮次实现；
本模块负责 chunk 与索引链路，保证「删除论文 → 清索引」与「重建索引」可用。

**双语 chunk**：同一个 chunk 出 ``zh`` / ``en`` 两行（chunk id 加 ``:zh`` / ``:en`` 后缀，
``lang`` 进 ``metadata_json``）—— **不改表结构**（不动 MySQL 列，避免 ALTER 迁移）。
理由见 ``build_chunks_for_paper`` 的 docstring。

**公式的两类索引单元（本轮）**：
- *语义 chunk*（``text``）：正文里**内联保留完整公式**（``$$…$$`` 原样、绝不拆开），
  「公式前的定义/说明 + 公式 + 公式后的 where/解释」落在同一条；
- *公式原子 chunk*（``formula``）：一条公式一条，用于公式专项检索与公式裁图。
两类互为补充、不做去重，靠 ``formula_ids`` / ``text_chunk_id`` 双向指认。
公式正文与语言无关，只出 **1 行**（``lang = any``）—— 旧实现 zh/en 各存一份，
实测 76 组正文完全相同的行（占全库 19%）纯属浪费。

**索引层预处理**（作者/单位/邮箱/arXiv/会议行/脚注过滤、Markdown 表现格式清理、
图注去重、表格数字行不当标题）在 ``backend/services/knowledge/preprocess.py``：
只服务知识库，**不动解析产物、不动阅读器视图**，改完只需重建索引（不重新解析）。
"""

from __future__ import annotations

import re
from pathlib import Path

from backend.core.config import get_settings
from backend.core.errors import NotFoundError, ValidationError
from backend.core.logging import get_logger
from backend.core.utils import (
    iso_utc, json_dumps, new_id, now, sha256_text, truncate,
)
from backend.repositories.mysql.models import Block, Chunk, Paper
from backend.repositories.mysql.session import session_scope
from backend.services.knowledge import formula_source
from backend.services.model.model_manager import model_manager
from backend.services.paper import paper_service, reader_service

log = get_logger(__name__)

#: 两条路（顺序 = 入库顺序：先英文、后中文）。
#: A 路 = 英文转录的正文与边界；B 路 = 中文译文的正文与边界。**两路的划分允许不同** ——
#: 用户口径：两个模型对语义边界的理解可能不同，不要强制它们一致。
ROADS = ("a", "b")

# chunk_type 的完整取值（规划 §10 ⑫）。
# 注意 `unit`：它是"检索单位"那一档（`kb.unit_index`），不是"内容种类"那一档 ——
# 早期这里漏了它，于是这个真实在用、且是检索默认候选的类型不在规范枚举里（dev-log §17.66）。
CHUNK_TYPES = ("text", "unit", "formula", "figure", "table")
# 正文语义 chunk 的目标长度（到点即 flush）
TARGET_CHUNK_CHARS = 1400
# 小正文 chunk 的**合并门槛**（不是丢弃门槛：能和相邻正文块合并就合并，
# 合并不了就照样入库 —— 用户口径"尝试与相邻语义块合并"）
MIN_CHUNK_CHARS = 200
# 单个 chunk 的硬上限：append 之前预检 buffer + block，任何正文 chunk 都不得超过
HARD_CHUNK_CHARS = 2240
# 真正的"残渣"丢弃门槛（英文侧）：低于这个长度的正文块才丢
RESIDUE_CHARS = 40
# 双语：同一 chunk 的两种语言入库行（顺序即入库顺序，zh 在前便于人肉核对）
LANGS = ("zh", "en")
# 语言无关（公式）：只出 1 行、zh/en 检索都要能召回
ANY_LANG = "any"
# 中文侧的最小长度门槛按**字**算：中文 1 字的信息量约等于英文 2~3 字符，沿用 40 字符
# 会把大量正常的中文块误杀（实测把 "## B.2 QJL：1 比特内积量化" 这类小节标题全丢了）。
# 门槛只用来兜"取到的是残渣"：译文缺失的行压根不会出 zh 行（见 `_chunk_variants_rows`）。
MIN_ZH_CHARS = 8
#: `chunk.section` 的列宽（与 `repositories/mysql/models.Chunk.section` 一致）。
#: 兜底截断：实测 3360-mmdfnd 有一处把整段 `CCS CONCEPTS • **Information systems** → …`
#: 当成章节标题（134 字符），撞上老库 varchar(128) → MySQL 1406 让**整个 INDEX 任务失败**
#: （2026-09-27 用户报的"建立索引错误"）。一条脏标题不该毁掉整次索引；全文另存
#: `metadata.section_path`，截断只影响那一列用于显示的字段。
SECTION_MAX_CHARS = 255


def _setting_int(key: str, default: int) -> int:
    """读 setting 表里的整数参数（读不到/坏值一律回退默认）。

    键名用**裸名**（`chunk_target_chars`），`kb.` 前缀由 `retrieval.setting` 统一补 ——
    全库只有那一条解析规则（`kb.` 优先 + 裸键兼容），这里不再各写一套。

    为什么要落成 setting：粒度是**要按真实检索效果反复调**的参数，改一次不该动代码；
    但改完必须**重建索引**才生效（chunk 是建出来的，不是查出来的）。
    """
    from backend.services.knowledge import retrieval

    return retrieval.setting_int(key, default)


def using_breaks() -> bool:
    """本次建索引是否使用 LLM 语义锚点（`kb.chunk_use_breaks`，默认开）。"""
    from backend.services.knowledge import retrieval

    return retrieval.setting_flag(USE_BREAKS_SETTING, True)


def unit_index_enabled() -> bool:
    """是否把语义单元也建进索引（`kb.unit_index`，默认开）。"""
    from backend.services.knowledge import retrieval

    return retrieval.setting_flag(UNIT_INDEX_SETTING, True)


def chunk_params() -> tuple[int, int, int]:
    """``(target, min, hard)``；setting 键：``kb.chunk_target_chars`` 等。"""
    target = max(200, _setting_int("chunk_target_chars", TARGET_CHUNK_CHARS))
    floor = max(RESIDUE_CHARS, _setting_int("chunk_min_chars", MIN_CHUNK_CHARS))
    hard = max(target, _setting_int("chunk_hard_chars", HARD_CHUNK_CHARS))
    return target, floor, hard




def _safe_preview(content: str, limit: int = 200) -> str:
    r"""预览文本：截断后若 `$` 没配对就补一个收尾定界符。

    为什么单独给预览做：左侧结果列表走通用 markdown 渲染，**截断切进公式中间**会留下
    没闭合的 `$` → 那一条就整段变纯文本（用户看到的"左侧不渲染"）。这只影响预览展示，
    不动 chunk 正文本身。
    """
    text = truncate(content, limit)
    if text.count("$") % 2:
        # 补定界符**不能超列长**（content_preview 有长度上限）；重截一次后要**再判一次**
        # 奇偶（少截一个字符可能刚好把一个 `$` 切掉）
        text = truncate(content, limit - 1)
        if text.count("$") % 2:
            text += "$"
    return text


def _inline_embed_math(text: str) -> str:
    r"""把**夹在句子中间**的 `$$…$$` 降级成行内 `$…$`。

    渲染端遇到 `$$` 会另起一块（display math）：夹在句子里就会出现"句子被拦腰截断、
    公式在下面摊成一堆分式"的观感（用户截图那处）。**行首的** `$$`（整段展示公式）
    保持不动 —— 那是正常的展示公式。
    """
    out: list[str] = []
    for line in (text or "").split("\n"):
        if line.lstrip().startswith("$$"):
            out.append(line)
            continue
        if "$$" in line:
            # 句中展示公式 → 行内公式：**`$` 后面不能有空格**（渲染端不把 `$ x$` 当数学，
            # 会原样显示成纯文本 —— 踩过），所以顺带把定界符内侧的空白去掉
            line = re.sub(r"\$\$\s*(.+?)\s*\$\$", lambda m: f"${_strip_tag_inline(m.group(1))}$", line)
        out.append(line)
    return "\n".join(out)

_TAG_RE = re.compile(r"\\tag\*?\{[^}]*\}")


def _inline_formula_block(latex: str) -> str:
    r"""把公式块的 LaTeX 变成**能进正文的定界形式**。

    - 单行公式 → `$…$`（行内；`$` 后不能有空格，渲染端不认 `$ x$`，会显示成纯文本）；
    - **多行公式**（`\begin{aligned}…\end{aligned}`）→ 独立成段的 `$$…$$`。

    为什么多行必须换定界符：行内定界符**不跨行** —— marked 的行内数学规则匹配不到
    （实测 `$…$` 跨行 katex=0 段、整段回落成纯文本），而且里面的 `_` / `*` 会被当 markdown
    解析（`\|y-\tilde{y}\|_2^2` 变成 `<em>2^2`，正文被吃掉字）；换成 `$$…$$` 后
    跨行正常渲染（katex≥1、err=0）。p18 的 `\begin{aligned}` 就是这条。
    """
    body = _strip_tag_inline(latex)
    if "\n" in body.strip() or "\\begin{" in body:
        return f"\n\n$$\n{body.strip()}\n$$"
    return f"${body}$"


def _strip_tag_inline(latex: str) -> str:
    r"""去掉 `\tag{n}`：**行内模式下 KaTeX 会直接报错**（`\tag works only in display equations`），
    渲染端于是回退成纯文本 —— 用户看到的"公式变裸 LaTeX"就是它。

    公式**条目**是展示模式（`$$…$$`），`\tag` 保留；只有内联进正文的行内公式要去掉，
    编号本来也已单独存在 metadata 的 `formula_number` 里。
    """
    return _TAG_RE.sub("", latex or "").strip()

_CJK_RE = re.compile(r"[\u3400-\u9fff\uf900-\ufaff]")
# 高置信 LaTeX 命令（判断"这一段确实是公式"；宁可少补也不要误伤正文）
_STRONG_TEX_RE = re.compile(
    r"\\(?:frac|dfrac|sqrt|sum|prod|int|mathbb|mathcal|boldsymbol|tilde|langle|rangle|"
    r"cdot|geq|leq|approx|min|max|left|right|tag|bar|hat|gamma|Gamma|ell|sigma|"
    r"mu|theta|alpha|beta|lambda|nabla|partial|infty|sim|to|in|exp|log|arg)")
_TEX_TOKEN_RE = re.compile(r"[^\s，。；：、！？（）【】《》]+")


def _scan_math(line: str) -> tuple[list[tuple[str, str]], str | None]:
    r"""把一行切成 ``[(kind, 片段)]``（``kind`` = ``plain`` / ``math``），并报告行末**未闭合**的定界符。

    为什么必须单独抽出来：`$$…$$` 要当**一个整体**配对。第一版是"从每个 `$` 找下一个 `$`"，
    于是 `$$(\text{unbiased inner-prod}) \quad \mathbb{E}_Q\left[\langle y, Q^{-1}(Q(x))\rangle\right]$$`
    被配成"`$$`（空公式）+ 正文 + `$$`（空公式）"——公式**内部**落进了 ``plain`` 片段，
    规则②紧接着在展示公式中间又包了一层 `$`（`…\langle y, $Q^{-1}(Q(x))\rangle\right]$ …`），
    `\left[…\right]` 被劈开，KaTeX 报 `Expected group as argument to '\right'`，
    它那个报错节点的 `title` 里带引号、把 HTML 属性撕开，整段源码就漏进正文了（用户截图那处）。

    第二个返回值给调用方做**跨行**判定：`$…$` / `$$…$$` 都可能被排版噪声拆到多行
    （`\begin{aligned}` 那种），下一行再补定界符就会把公式切碎。``None`` = 本行配平。
    """
    pieces: list[tuple[str, str]] = []
    cursor = 0
    i = 0
    leftover: str | None = None
    while True:
        i = line.find("$", i)
        if i < 0:
            break
        if line.startswith("$$", i):
            j = line.find("$$", i + 2)
            if j < 0:
                leftover = "$$"
                break
            pieces.append(("plain", line[cursor:i]))
            pieces.append(("math", line[i:j + 2]))
            cursor = i = j + 2
            continue
        j = line.find("$", i + 1)
        if j < 0:
            leftover = "$"
            break
        pieces.append(("plain", line[cursor:i]))
        pieces.append(("math", line[i:j + 1]))
        cursor = i = j + 1
    pieces.append(("plain", line[cursor:]))
    return [(k, s) for k, s in pieces if s], leftover


def _wrap_bare_latex(text: str) -> str:
    r"""给**裸写的 LaTeX**补定界符：按"未包裹的片段"补，**不**因为"这行已有 `$`"整行放弃。

    为什么索引层也要做：知识库页的渲染链没有阅读器那套 `wrapBareMath()`，而解析文本块会把
    展示公式**原样**带进正文（`\mathcal{C}(f_X, b) := \min…`）—— 用户看到的就是
    "公式变成纯文本"。前端那条规则是"这行已经有 `$` 就不动"，可这一行前面恰好有 `$c_i$`，
    于是整段裸 LaTeX 被漏掉。

    守则（与前端一致，宁可少补）：
    ① 整行无 `$`、无中文、含高置信命令（≥2 个或带 `^{`/`_{`）→ 整行包 `$$…$$`；
    ② 其余按 `$…$` / `$$…$$` 配对切开（见 `_scan_math`），只在**公式之外**的片段里，把
       "含 `^{`/`_{`、无中文、不过长"的连续片段包成 `$…$`（已包好的数学不动）；
    ③ 定界符**配不平**或**跨行**（本行留下未闭合的 `$`/`$$`）→ 这一行与后续行一律原样保留：
       补错比不补更糟，会把公式劈开、让 KaTeX 整段报错。
    """
    if not text or ("\\" not in text and "^{" not in text and "_{" not in text):
        return text
    out: list[str] = []
    carry: str | None = None        # 上一行留下的未闭合定界符
    for line in text.split("\n"):
        if carry is not None:
            out.append(line)
            if carry in line:
                carry = None
            continue
        t = line.strip()
        if "$" not in line and t and not _CJK_RE.search(t):
            strong = len(_STRONG_TEX_RE.findall(t))
            if _STRONG_TEX_RE.search(t) and (strong >= 2 or re.search(r"[_^]\{", t)):
                out.append(f"$${t}$$")
                continue
        pieces, leftover = _scan_math(line)
        if leftover is not None:
            out.append(line)
            carry = leftover
            continue

        def _wrap(m: "re.Match[str]") -> str:
            tok = m.group(0)
            if not re.search(r"[_^]\{", tok) or _CJK_RE.search(tok) or len(tok) > 60:
                return tok
            return f"${_strip_tag_inline(tok)}$"

        out.append("".join(seg if kind == "math" else _TEX_TOKEN_RE.sub(_wrap, seg)
                           for kind, seg in pieces))
    return "\n".join(out)


def _strip_unpaired_bold(text: str) -> str:
    r"""去掉**行首落单的加粗标记**（`**图 5：…` 这种没有收尾的 `**`）。

    来源：译文把 `**Figure 5:**` 的收尾 `**` 丢了，于是 `**` 原样显示在正文里
    （用户报的"细微 markdown 问题"）。只处理"行首是 `**` 且整行 `**` 出现奇数次"的情形，
    成对的加粗原样保留 —— 不做别的 markdown 改写。
    """
    out: list[str] = []
    for line in (text or "").split("\n"):
        if line.lstrip().startswith("**") and line.count("**") % 2:
            i = line.index("**")
            line = line[:i] + line[i + 2:]
        out.append(line)
    return "\n".join(out)




































def _variant_row(c: dict, lang: str, content: str) -> dict:
    """一条入库行（``:zh`` / ``:en`` / ``:any``）。"""
    meta = {**(c.get("metadata") or {}), "lang": lang}
    if lang != ANY_LANG:
        meta["pair_id"] = c["id"]
    return {
        **{k: v for k, v in c.items() if not k.startswith("_")},
        "id": f"{c['id']}:{lang}",
        "lang": lang,
        "content": content,
        "content_hash": sha256_text(content),
        "content_preview": _safe_preview(content),
        "token_count": _approx_tokens(content),
        "metadata": meta,
    }


def _chunk_variants_rows(base: list[dict], langs: tuple[str, ...]) -> list[dict]:
    """把「单语 chunk」展开成语言行（**新路径**：两侧内容在建块时就都在 chunk 上了）。

    - **公式原子 chunk 只出 1 行**（``lang = any``）：正文是 LaTeX，与语言无关。
      旧实现 zh/en 各出一行、正文完全相同（实测 76 组冗余行）—— 语言过滤那边
      （``retrieval._filter_rows`` / ``/knowledge/chunks``）把 ``any`` 视为"两种语言都通过"，
      所以中文、英文检索都召得回。
    - 正文：中文来自 ``chunk["_zh"]``（同一批行的 ``translation`` 拼出来的，与英文**逐行对位**）；
      整块都没有译文 → 不出 zh 行（不拿原文冒充译文）。
    - 展示层清洗（去落单加粗 / 句中展示公式降级 / 裸 LaTeX 补定界符）两侧都要做 ——
      它们修的是**渲染**问题，与语种无关。
    """
    out: list[dict] = []
    for c in base:
        if c["chunk_type"] == "formula":
            out.append(_variant_row(c, ANY_LANG, c["content"]))
            continue
        for lang in langs:
            if lang == "en":
                content = c["content"]
            else:
                content = c.get("_zh") or ""
                if not content.strip():
                    continue                  # 还没翻译 → 不出 zh 行
            floor = RESIDUE_CHARS if lang == "en" else MIN_ZH_CHARS
            if c["chunk_type"] in ("text", "unit", "figure", "table"):
                content = _strip_unpaired_bold(content)
            if c["chunk_type"] in ("text", "unit"):
                content = _inline_embed_math(content)
                content = _wrap_bare_latex(content)
            # 单元不吃"残渣门槛"：短单元（单行小段）在细粒度检索里是有用的，
            # 门槛只用来兜正文块的残渣（见 `_chunk_variants_rows` 的 docstring）
            if c["chunk_type"] == "text" and len(content) < floor:
                continue
            out.append(_variant_row(c, lang, content))
    return out


# --------------------------------------------------------------------------
# Chunk 化（规划 §21.6 步骤 1）
# --------------------------------------------------------------------------


#: 是否使用 LLM 语义锚点（1/0，默认 1）。键名是**裸名**：`retrieval.setting` 会先试
#: `kb.chunk_use_breaks`（文档里的写法），再回退裸键。老库两种写法都能读。
USE_BREAKS_SETTING = "chunk_use_breaks"
#: 是否把语义单元也建进索引（1/0，默认 1）—— 单元是**检索单位**，chunk 是上下文单位
UNIT_INDEX_SETTING = "unit_index"


def build_chunks_for_paper(paper_id: str, *,
                           langs: tuple[str, ...] = LANGS) -> list[dict]:
    """按「TOC 层级 + 页级行 + LLM 语义锚点」生成四类 chunk，并展开成 zh/en 双语行。

    为什么要双语：库内正文的事实源是**页级产物**（``pages.json`` 有原文 + 译文），
    而中文提问与英文原文之间隔着一次跨语检索 —— 模型的跨语能力能用，但实测（§17.33）
    中文问句对中文段落的相似度明显更"实"；反之英文术语（MSE、inner product）在英文行上
    更准。两行都入库、由重排统一裁决，是召回最稳的做法。

    **两路各一套**：A 路（英文转录）在英文侧切分、B 路（中文翻译）在中文侧切分，
    两边的**边界允许不同**（见 `_base_chunks` / `ROADS`）。所以这里的"双语"有两种来源：
    正文/单元来自各自那一路（一条 chunk 只有一个语言面），图/表/公式是两路共用（天然双语）。

    **不改表结构**：路号进 id（``{paper}:{A|B}:…``）与 ``metadata_json``，
    语言后缀 ``:zh`` / ``:en`` 照旧，``lang`` 仍只写进 ``metadata_json``。
    向量库额外加一列 ``road``（检索侧要按路过滤，元数据列在 LanceDB 里过滤不了）。
    """
    base, _road_summary = _base_chunks(paper_id)
    by_road: dict[str | None, list[dict]] = {None: []}
    for road in ROADS:
        by_road[road] = []
    for chunk in base:
        by_road[(chunk.get("metadata") or {}).get("road")].append(chunk)
    out: list[dict] = []
    for road in ROADS:
        # 一路只出自己那一种语言行（A → `:en`、B → `:zh`）；正文本来就在 `content` 里
        out.extend(_path_variants_rows(by_road[road], road))
    # 共用块（图/表/公式）照旧展开成 zh/en（公式内部只会得到 `any` 一行）
    out.extend(_chunk_variants_rows(by_road[None], langs))
    # 最后一道防线：任何来源的空正文都不许进库（向量接口会 400，见 `_path_variants_rows`）
    return [c for c in out if (c.get("content") or "").strip()]


def _path_variants_rows(base: list[dict], road: str) -> list[dict]:
    """一条路自己的语言行：A 路只出 ``:en``、B 路只出 ``:zh``。

    与 `_chunk_variants_rows` 的区别：那个是"一条 chunk 同时有两个语言面"时代的产物
    （英文本体 + ``_zh`` 译文），而**按路分块**之后每条 chunk 只有一个语言面 ——
    中文那条的正文本来就在 ``content`` 里，不需要再去 ``_zh`` 取一次。

    ⚠️ **空正文的块不出行**。展示层清洗（去落单加粗等）有可能把一小块洗成空串 ——
    实测 A 路 p001 的第一个块就是空的（模型发了一个只有空白的块）：
    这种行入库后**没有任何检索价值**，而且会让向量接口直接 400
    （`text input should not be empty`），整篇的向量入库全失败（MySQL 元数据已经写进去了，
    于是出现"元数据有 359 条、向量 0 条"的半截状态）。宁可不出这一行。
    """
    lang = "en" if road == "a" else "zh"
    out: list[dict] = []
    for c in base:
        content = c["content"] or ""
        if not content.strip():
            log.warning("跳过空正文的块（%s 路）：%s", road, c.get("id"))
            continue
        out.append(_variant_row(c, lang, content))
    return out


# ==========================================================================
# TOC 层级 + 页级行原子 + LLM 语义锚点
# ==========================================================================


def _base_chunks(paper_id: str) -> tuple[list[dict], dict]:
    """**两路**的正文块 + 单元 + 两路共用的图/表/公式块；另返回 `road_summary`（日志/回归用）。

    与改造前的旧路径的根本差别：**切分骨架与内容原子都换了来源**。

    - 骨架 = 阅读器目录（`chunking.section_tree`）：节之间不因为长度合适而合并，
      节内边界由 LLM 语义锚点给出（`chunking.semantic_units`）；
    - 原子 = 页级行（`chunking.row_units`）：A 路取 `original`、B 路取 `translation`；
    - **两路各装配一次**（`chunking.build(..., road="a"/"b")`）：各用各的锚点、各按自己那一侧
      的字数卡长度 —— 于是两路的单元/块划分可以不同（用户口径：不要强制一致）。
      每一路内部仍然满足 `row ⊆ unit ⊆ chunk`、`chunk = 整数个 unit`；
    - 图 / 表：结构行（caption / table / figure）按"同页 + 同图号"归组，各自一条 chunk，
      表体（markdown）**进正文**；**两路共用**一份（内容本就与语言无关或天然双语）；
    - 公式：来自 canonical 清单（`reader_service.markdown_formulas` → C 口径 → 挂锚点），
      同样两路共用。

    装配结果会过一遍校验层（`chunking.validate`）：行覆盖恰好一次、顺序不变、不跨节、
    ≤ hard、正文里不混结构行、单元与块的父子关系确定。**每一路各自**：不通过就先退回
    "不用锚点"再装配一次，仍不通过则原样入库并把错误写进日志（**宁可入库也不静默丢内容**）。
    """
    from backend.services.knowledge import chunking

    with session_scope() as sess:
        paper = sess.get(Paper, paper_id)
        authors = (paper.authors or "") if paper is not None else ""

    # 图/表区域先算：**它决定哪些"图注行"是真图注**（其余以 Figure/Table 开头的行是正文，
    # 见 `_caption_rows`）。这一判据必须传给分块层，否则那类正文行会被当结构行丢掉。
    # 读不到区域（缺 IR / 路径异常）不算错：退回"没有图注"，图/表块改由行类型判定。
    from backend.services.paper import figure_regions

    try:
        regions = figure_regions.all_regions(paper_id)
    except Exception as exc:  # noqa: BLE001
        log.debug("读取图/表区域失败（%s）：%s", paper_id, exc)
        regions = {}
    caption_ids, region_of_caption = _caption_rows(paper_id, regions)

    rows, tree = chunking.load(paper_id, authors=authors,
                               structural_caption_ids=caption_ids)
    if not rows:
        raise ValidationError(f"没有可用行：{paper_id} 是否尚未解析？")

    target, floor, hard = chunk_params()
    use_breaks = using_breaks()

    def build_road(road: str | None) -> tuple[list[dict], list[dict], dict, dict]:
        """按路装配一次（含"校验不过就退回不用锚点"的既有策略）。"""
        breaks_on = use_breaks
        built = chunking.build(rows, tree, target=target, hard=hard, floor=floor,
                               use_breaks=breaks_on, road=road)
        if not built["report"]["ok"] and breaks_on:
            log.warning("语义锚点装配未通过校验（%s，路 %s），退回「只按目录层级」：%s",
                        paper_id, road or "共用", built["report"]["errors"][:3])
            breaks_on = False
            built = chunking.build(rows, tree, target=target, hard=hard, floor=floor,
                                   use_breaks=False, road=road)
        if not built["report"]["ok"]:
            log.error("行级装配仍未通过校验（%s，路 %s）：%s",
                      paper_id, road or "共用", built["report"]["errors"][:5])
        return (built["chunks"], built["units"], built["stats"],
                {**built["report"], "use_breaks": breaks_on})

    # 解析块只在两处还需要：IR 的公式/图表元数据、以及"块 → 行"映射（给公式条目定行）。
    md_map = reader_service.load_markdown_map(paper_id)
    from backend.parsing.ir.ir_reader import IRReader

    ir = IRReader(paper_service.paper_paths(paper_id)["ir"])
    formulas = {f["block_id"]: f for f in ir.formulas()}
    with session_scope() as sess:
        blocks = (sess.query(Block).filter(Block.paper_id == paper_id)
                  .order_by(Block.order_global.asc()).all())
        parse_rows = [_block_row(b, md_map) for b in blocks]
    f_entries = formula_source.kb_formula_entries(paper_id, formulas, parse_rows)
    block_to_row = _block_row_ids(paper_id, parse_rows)

    # ------------------------------------------------------------------
    # 两路各装配一套正文块 + 语义单元（**划分允许不同**），图/表/公式两路共用一套
    # ------------------------------------------------------------------
    out: list[dict] = []
    road_summary: dict[str, dict] = {}
    _, _, chunk_hard = chunk_params()
    for road in ROADS:
        chunks, units, stats, report = build_road(road)
        # 只有标题的正文块：并进行序上紧邻的下一个块（见 `_merge_heading_only`）
        merged = _merge_heading_only(chunks, road, limit=chunk_hard)
        if merged:
            log.info("标题块并入相邻正文块（%s，%s 路）：%d 块", paper_id, road, merged)
        key_to_id: dict[str, str] = {}
        for chunk in chunks:
            made = _row_text_chunk(paper_id, chunk["rows"], chunk.get("node"), road=road)
            key_to_id[str(chunk.get("key"))] = made["id"]
            out.append(made)
        if unit_index_enabled():
            # 只有标题的单元**不写进库**：命中了也只有一个章节名（内容在 `section` 字段里）。
            # 分区阶段一个字节都不动 ——「单元恰好覆盖每个正文行一次」是校验层与测试钉着的不变量，
            # 这里只是让「写库」这一步不产出这种行（线上实测 37 条 < 60 字符的 unit 全是这种）。
            from backend.services.knowledge.chunking import is_heading_like as _heading_like
            row_by_id = {r["id"]: r for r in rows}
            kept = [u for u in units
                    if not all(_heading_like(row_by_id.get(rid, {})) for rid in u["row_ids"])]
            if len(kept) != len(units):
                log.info("跳过只有标题的单元（%s，%s 路）：%d 条",
                         paper_id, road, len(units) - len(kept))
            out.extend(_row_unit_chunks(paper_id, kept, key_to_id,
                                        use_breaks=report["use_breaks"], road=road))
        road_summary[road] = {"chunks": len(chunks), "units": len(units),
                              "stats": stats, "ok": report["ok"],
                              "use_breaks": report["use_breaks"],
                              "errors": report["errors"][:3]}
        log.info("行级分块（%s，%s 路）：行 %d / 正文块 %d / 单元 %d；装配 %s；校验 %s",
                 paper_id, road, len(rows), len(chunks), len(units), stats,
                 "通过" if report["ok"] else f"未通过 {report['errors'][:2]}")

    # 图 / 表 / 公式：**两路共用**（内容本来就与语言无关或天然双语，切了也没有意义）
    shared = _row_structure_chunks(paper_id, rows, ir, regions, caption_ids,
                                   region_of_caption)
    shared.extend(_row_formula_chunks(paper_id, rows, f_entries, block_to_row))
    out.extend(shared)
    _attach_formula_ids(out, rows, f_entries, block_to_row)
    # 行身份：pid 只从页产物取（不重算），与 row_ids 同序等长 —— 只加字段
    _attach_row_pids(out, _pid_by_row_id(paper_id))
    out.sort(key=lambda c: (c["order_global"],
                            (c.get("metadata") or {}).get("road") or "",
                            0 if c["chunk_type"] in ("text", "unit") else 1))
    log.info("分块完成（%s）：行 %d / 总计 %d 条（A %d + B %d + 共用 %d）",
             paper_id, len(rows), len(out),
             road_summary["a"]["chunks"] + road_summary["a"]["units"],
             road_summary["b"]["chunks"] + road_summary["b"]["units"], len(shared))
    return _link_formulas(out), road_summary


# --------------------------------------------------------------------------
# 行身份（pid）：让 chunk 记住“这块是哪些行”的稳定身份（阶段 2a）
# --------------------------------------------------------------------------


def _pid_by_row_id(paper_id: str) -> dict[str, str]:
    """`{行 id: pid}` —— pid **只从页产物取**，这里不算也不发身份。

    `_finalize_page` 是唯一写 `row[pid]` 的地方，而它写的就是
    `reader_service.page_translations`（= `page_products(paper_id)[0]`，见
    reader_service.py:451）这份读模型；所以“行 id → pid”只能从那里取，
    **绝不调 `row_identity.assign` 自己算一遍** —— 重算出来的号与阅读器下发的
    对不上，引用一存就指错行。

    读不到产物（未翻译 / 异常）就给空表：chunk 少这个字段是小事，建索引不能因此失败。
    """
    try:
        pages = reader_service.page_translations(paper_id)
    except Exception as exc:  # noqa: BLE001 —— 与 pid 挂钩点同口径：身份失败只丢字段
        log.warning("读页产物失败，本次不给 chunk 带 pid（%s）：%s", paper_id, exc)
        return {}
    out: dict[str, str] = {}
    for page in pages or []:
        if not isinstance(page, dict):
            continue
        for row in page.get("rows") or []:
            if not isinstance(row, dict):
                continue
            rid = str(row.get("id") or "")
            pid = str(row.get("pid") or "").strip()
            if rid and pid:
                out[rid] = pid
    return out


def _attach_row_pids(chunks: list[dict], pid_of: dict[str, str]) -> None:
    """给每条 chunk 的 metadata 补 `pids`：与 `row_ids` **同序等长**，只加字段。

    某行没有 pid 就**跳过这一条**（不填假值），缺了几条写进 `pids_missing` ——
    于是“有 pid 的都在这里、还缺几条”都能从元数据里读出来，audit 也能分级报警。
    `chunk[id]` / `row_ids` / 排序 / 检索候选集一律不动（本函数只写这两个键）。
    """
    for chunk in chunks:
        meta = chunk.get("metadata")
        if not isinstance(meta, dict):
            meta = chunk["metadata"] = {}
        row_ids = [str(x) for x in (meta.get("row_ids") or [])]
        pids = [pid_of[rid] for rid in row_ids if rid in pid_of]
        meta["pids"] = pids
        missing = len(row_ids) - len(pids)
        if missing > 0:
            meta["pids_missing"] = missing

def _row_unit_chunks(paper_id: str, units: list[dict], key_to_id: dict[str, str], *,
                     use_breaks: bool, road: str | None = None) -> list[dict]:
    """语义单元 → ``chunk_type="unit"`` 的入库行（**检索单位**）。

    正文由单元的 `en` / `zh` 直接给出 —— 那本来就是"按行原文拼起来的"，本函数不生成任何文字。
    元数据里带 `parent_chunk_id`（small-to-big 往上取上下文）与 `prev/next_unit_id`（±1 邻居），
    检索侧与上下文扩展都直接用这两个指针，不必在查询时重算分块。

    `road` 给定时：正文只取**本路**那一面（A→`en` / B→`zh`）、所有单元 id 都带路号，
    且 `key_to_id` 必须是**同一路**的正文块表（两路的 chunk key 是同一批行 id，混用会指错父块）。
    """
    prefix = f"{paper_id}:{road}:" if road else f"{paper_id}:"
    out: list[dict] = []
    for unit in units:
        first, last = unit["first_row"], unit["last_row"]
        content = (unit["zh"] if road == "b" else unit["en"]) if road else unit["en"]
        meta = {
            "row_ids": list(unit["row_ids"]),
            "pages": list(unit["pages"]),
            "section_kind": unit["section_kind"],
            "parent_chunk_id": key_to_id.get(str(unit.get("parent_chunk_id"))),
            "prev_unit_id": (f"{prefix}unit:{unit['prev']}" if unit.get("prev") else None),
            "next_unit_id": (f"{prefix}unit:{unit['next']}" if unit.get("next") else None),
            "unit_chars": unit["chars"],
            "unit_ids": [f"{prefix}unit:{unit['unit_id']}"],
            # 这条单元的左边界是 LLM 锚点、还是"长度兜底/小节边界"切出来的（回归时能分开量）
            "anchor_used": bool(unit.get("anchor_used")),
            "unit_source": "llm_break" if (use_breaks and unit.get("anchor_used"))
                           else "deterministic",
        }
        chunk = _make_chunk(
            paper_id, "unit", content,
            {"id": first["id"], "page": first["page"], "order": first["index"]},
            {"id": last["id"], "page": last["page"], "order": last["index"]},
            section=unit.get("section_title") or "",
            section_path=unit.get("section_path") or "",
            metadata=meta,
            chunk_id=f"{prefix}unit:{unit['unit_id']}",
            road=road,
        )
        if road is None:
            chunk["_zh"] = unit["zh"] or None
        out.append(chunk)
    return out


def _merge_heading_only(chunks: list[dict], road: str | None, *, limit: int) -> int:
    """把「只有标题」的正文块（``rows`` 全是 heading）并进行序上**紧邻的下一个**块。

    为什么需要：分块器在「节点自己的行只有标题、且下一块的首行正好是语义断点」时，
    会按纪律把引言单独成块（``assemble`` 里的 ``intro_alone``）—— 于是索引里多出一条
    ``## 0.2 PRIOR WORK`` 这种 17 字符的块，检索命中它等于交白卷（线上 430 块里有 8 块）。

    **为什么不靠改模型提示词**：断点提示词里早就写了「不要标标题行的下一行」，
    但这 8 块的元数据里 ``anchor_used`` 是空的、类型是 ``text`` —— 它们来自**打包层**，
    与模型标不标断点无关。打包层的纪律是"宁可引言单独成块"，这里给它补一条反向约束：
    **标题块必须带上它下面的正文**（合并后仍须 ≤ ``hard``，否则不动）。

    只动 ``text`` 层（上下文单位）：``unit`` 层（检索单位）的边界不动，break 纪律不受影响。
    """
    from backend.services.knowledge.chunking import is_heading_like, road_text

    def size(items: list[dict]) -> int:
        """与分块器同一个长度口径（按路取文本、去空白后累加）—— `chunking` 里的
        `chars` / `hard_len` 是 `assemble` 的内部函数，这里按同一口径自己算，避免复制语义。"""
        return sum(len(road_text(row, road).strip()) for row in items)

    merged = 0
    index = 0
    while index < len(chunks):
        chunk = chunks[index]
        rows = chunk.get("rows") or []
        if not rows or any(not is_heading_like(r) for r in rows):
            index += 1
            continue
        nxt = chunks[index + 1] if index + 1 < len(chunks) else None
        nxt_rows = (nxt or {}).get("rows") or []
        if nxt is not None and nxt_rows and size(rows + nxt_rows) <= limit:
            nxt["rows"] = rows + nxt_rows   # 标题跟在正文前面，行序不变
            nxt["node"] = chunk.get("node") or nxt.get("node")
            chunks.pop(index)
            merged += 1
            continue
        # 末尾那种「只有标题、后面没有块」的（实测 attention 篇的 `# Attention Visualizations`）：
        # 往前并 —— 追加到前一块的尾部，行序仍然连续；合不下就保持原样。
        prev = chunks[index - 1] if index else None
        prev_rows = (prev or {}).get("rows") or []
        if prev is not None and prev_rows and size(prev_rows + rows) <= limit:
            prev["rows"] = prev_rows + rows
            chunks.pop(index)
            merged += 1
            index = max(0, index - 1)
            continue
        index += 1                          # 合不下就保持原样：宁可多一条小标题块，也不造超长块
    return merged


def _row_text_chunk(paper_id: str, items: list[dict], node: dict | None,
                    road: str | None = None) -> dict:
    """一段连续行 → 一条正文 chunk。

    `road` 给定时正文取**那一路**的文本（A 英文 / B 中文），另一侧不产出 ——
    按路分块之后每条 chunk 只有一个语言面。`road=None` 是旧口径：英文本体 + `_zh` 译文
    （图/表这类共用的块仍走这条路）。
    """
    first, last = items[0], items[-1]
    content = "\n\n".join(_road_row_text(r, road) for r in items)
    zh = None
    if road is None:
        zh = "\n\n".join(r["translation"].strip() for r in items
                         if (r["translation"] or "").strip()) or None
    section = (node or {}).get("title") or first.get("section_title") or ""
    section_path = (node or {}).get("path") or first.get("section_path") or ""
    meta = {
        "row_ids": [r["id"] for r in items],
        "pages": sorted({r["page"] for r in items}),
        "section_kind": first.get("section_kind") or "body",
        "section_number": (node or {}).get("number") or "",
        # 语义锚点落在这条 chunk 里的行（用于量"锚点真的改变了哪些边界"）——
        # **按路判断**：A 路的边界与 B 路不同，混在一起就量不出"这一路的锚点起了什么作用"。
        "anchor_rows": [r["id"] for r in items if _is_road_break(r, road)],
        "row_span": [first["id"], last["id"]],
    }
    chunk = _make_chunk(
        paper_id, "text", content,
        {"id": first["id"], "page": first["page"], "order": first["index"]},
        {"id": last["id"], "page": last["page"], "order": last["index"]},
        section=section, section_path=section_path, metadata=meta,
        heading_only=all(r["type"] == "heading" for r in items),
        road=road,
    )
    if zh:
        chunk["_zh"] = zh
    return chunk


def _is_road_break(row: dict, road: str | None) -> bool:
    """这一行是不是**本路**标的语义边界。

    口径只有一份（`chunking.is_break`）：一条行可能被**两路同时**标为边界，
    单值的 `semantic_break_side` 只记得住一个，所以判据要以 `semantic_break_sides` 为准。
    """
    from backend.services.knowledge import chunking  # 本模块在函数内导入它（见 index_paper）

    return chunking.is_break(row, road)


def _road_row_text(row: dict, road: str | None) -> str:
    """按路取行的正文（A/并集 → 原文；B → 译文，缺译文退回原文）。"""
    if road == "b":
        return str(row.get("translation") or row.get("original") or "")
    return str(row.get("original") or "")


def _row_structure_chunks(paper_id: str, rows: list[dict], ir,
                          regions: dict, caption_ids: set[str],
                          region_of_caption: dict[str, dict]) -> list[dict]:
    """图 / 表行 → figure / table chunk（图注 + **表体**，与 IR 的裁图对齐）。

    与旧路径的差别：
    1. 表体（markdown 表格行）**进 chunk 正文** —— 旧路径只索引图注（实测英文侧表体 0 条），
       表格数值类问题因此检不到；
    2. 两侧都有（旧路径 zh 侧有表体、en 侧没有，两边不对称）；
    3. 图注与表体的**配对按"同页 + 同图号"**，与图注在表体上方还是下方无关
       （实测 turbo 的图注在表体下方、attention 在上方，两种顺序都要能配）。
    """
    image_of = {str(f.get("caption_block_id")): f.get("image_path") for f in ir.figures()}
    image_of.update({str(t.get("caption_block_id")): t.get("image_path")
                     for t in ir.tables()})

    out: list[dict] = []
    for group in _structure_groups(rows, caption_ids):
        region = _match_region(group, regions, region_of_caption)
        kinds = {r["type"] for r in group}
        chunk_type = (region or {}).get("kind") or ("table" if "table" in kinds else "figure")
        caption_row = next((r for r in group if r["type"] == "caption"), None)
        body_row = next((r for r in group if r["type"] in ("table", "figure")), None)
        caption = ((caption_row or {}).get("original") or "").strip() \
            or str((region or {}).get("caption") or "").strip()
        match = _FIG_NUMBER_RE.search(caption)
        number = match.group(1) if match else str((region or {}).get("number") or "")
        label = "表格" if chunk_type == "table" else "插图"
        tag = number or (caption_row or group[0])["id"]
        content = f"{label} {tag}：{caption}".strip()
        if body_row is not None:
            body = (body_row.get("original") or "").strip()
            if body and body != caption:
                content = f"{content}\n\n{body}"
        zh_parts = [r["translation"].strip() for r in group
                    if (r["translation"] or "").strip()]
        first, last = group[0], group[-1]
        meta = {
            "caption": caption, "number": number,
            "row_ids": [r["id"] for r in group],
            "pages": sorted({r["page"] for r in group}),
            "section_kind": first.get("section_kind") or "body",
            "image_path": image_of.get(str((region or {}).get("caption_block_id"))),
            "table_body_rows": [r["id"] for r in group if r["type"] == "table"],
        }
        chunk = _make_chunk(
            paper_id, chunk_type, content,
            {"id": first["id"], "page": first["page"], "order": first["index"]},
            {"id": last["id"], "page": last["page"], "order": last["index"]},
            section=first.get("section_title") or "",
            section_path=first.get("section_path") or "", metadata=meta,
        )
        chunk["_zh"] = "\n\n".join(zh_parts) or None
        out.append(chunk)
    return out


def _caption_rows(paper_id: str, regions: dict) -> tuple[set[str], dict[str, dict]]:
    """IR/figure_regions 里真实存在的图 / 表 → （图注行 id 集合, 行 id → 区域）。

    为什么必须过这一层：页级行里"以 Figure/Table 开头"的行不都是图注 ——
    attention 的 `Table 2 summarizes our results…` 是一段正文。只有 IR 里真有这一项
    （图号 + 页号能对上）才算图注，否则它会从正文里被删掉。
    """
    ids: set[str] = set()
    by_row: dict[str, dict] = {}
    for page, items in (regions or {}).items():
        for region in items:
            row_id = reader_service.row_id_of_caption(paper_id, int(page),
                                                      str(region.get("number") or ""))
            if not row_id:
                continue
            ids.add(str(row_id))
            by_row.setdefault(str(row_id), region)
    return ids, by_row


def _match_region(group: list[dict], regions: dict, region_of_caption: dict[str, dict],
                  ) -> dict | None:
    """一组结构行 → 它对应的图/表区域（先用图注行 id 认，再用页 + 图号 / 页 + 类别认）。"""
    for row in group:
        region = region_of_caption.get(str(row["id"]))
        if region:
            return region
    page = group[0]["page"]
    items = [r for r in (regions or {}).get(page, []) if isinstance(r, dict)]
    match = _FIG_NUMBER_RE.search(
        " ".join((r.get("original") or "") for r in group))
    if match:
        number = match.group(1)
        for region in items:
            if str(region.get("number")) == number:
                return region
    want = "table" if any(r["type"] == "table" for r in group) else "figure"
    same_kind = [r for r in items if r.get("kind") == want]
    return same_kind[0] if len(same_kind) == 1 else None


def _structure_groups(rows: list[dict], caption_ids: set[str]) -> list[list[dict]]:
    """把"结构行"（图注 / 表体 / 图）按页归组：一张图或一张表 = 一组。

    配对规则（实测两种排版都有：图注在表体上方或下方）：

    - 连续的结构行才可能同组（中间夹了正文就断开）；
    - 遇到新图注且**当前组已经有表体**、图号又不同 → 另起一组
      （attention p9：表 3 表体 → "Table 4" 图注 → 表 4 表体，必须切成两组）；
    - 当前组还没有图注 → 认领这个图注（turbo p9：表体在前、图注在后）。

    **只认 IR 里真的有这一项的图注行**（`caption_ids`）：attention 的
    `Table 2 summarizes our results…` 是一段正文，只是以 "Table 2" 开头，
    把它当图注会从正文里删掉一段真内容（`attached_caption_ids` 就是为这个加的）。
    """
    groups: list[list[dict]] = []
    cur: list[dict] = []
    cur_page = None
    cur_number = ""
    has_body = False

    def flush() -> None:
        nonlocal cur, cur_page, cur_number, has_body
        if cur:
            groups.append(cur)
        cur, cur_page, cur_number, has_body = [], None, "", False

    for row in rows:
        if row.get("skip_reason") or row["type"] not in chunking_struct_types():
            flush()
            continue
        if row["type"] == "caption" and row["id"] not in caption_ids:
            flush()          # 看着像图注其实是正文 → 让它回正文块
            continue
        number = ""
        if row["type"] == "caption":
            match = _FIG_NUMBER_RE.search(row.get("original") or "")
            number = match.group(1) if match else ""
        if cur and row["page"] != cur_page:
            flush()
        if cur and number and cur_number and number != cur_number and has_body:
            flush()
        if not cur:
            cur_page = row["page"]
            cur_number = number
        cur.append(row)
        if number:
            cur_number = number
        if row["type"] in ("table", "figure"):
            has_body = True
    flush()
    return groups


def chunking_struct_types() -> tuple[str, ...]:
    """结构行类型（放函数里是为了避免 `chunking` 模块的循环 import）。"""
    from backend.services.knowledge.chunking import STRUCT_TYPES

    return STRUCT_TYPES


def _row_formula_chunks(paper_id: str, rows: list[dict], entries: list[dict],
                        block_to_row: dict[str, str]) -> list[dict]:
    """canonical 公式条目 → 公式原子 chunk（章节与排序改成"它所在的行"）。"""
    row_of = {r["id"]: r for r in rows}
    out: list[dict] = []
    # 没有锚点 id 的公式条目（既缺 formula_block_id 也缺 row_id）：跳过并汇总一条日志
    skipped_no_anchor = 0
    for e in entries:
        latex = (e.get("latex") or "").strip()
        if not latex or formula_source.is_noise_latex(latex):
            continue
        # 锚点 id **不许造假**：以前缺 row_id 时写 f"row-x"，于是所有这类条目共用
        # 一条 `{paper}:formula:row-x` 的 chunk id（互相覆盖）和一条指向不存在的锚点。
        # 现在只有"真有 id"这一种情况：formula_block_id 优先，其次 row-<row_id>；
        # 两者都没有说明这一条定位不到任何东西 —— 跳过，不造 id。
        row_id = str(e.get("row_id") or "").strip()
        formula_block_id = str(e.get("formula_block_id") or "").strip()
        if formula_block_id:
            first_id = formula_block_id
        elif row_id:
            first_id = f"row-{row_id}"
        else:
            skipped_no_anchor += 1
            continue
        unique_key = formula_block_id or e.get("id") or row_id
        cid = f"{paper_id}:formula:{unique_key}"
        label = row_id or formula_block_id or e.get("id")
        content = f"公式 {label}：$${latex}$$"
        anchor_row = row_of.get(str(e.get("row_id") or "")) or row_of.get(
            str(block_to_row.get(str(e.get("formula_block_id") or "")) or ""))
        meta = {
            "latex": latex, "display": bool(e.get("display")),
            "row_id": e.get("row_id"), "page": e.get("page"),
            "context": e.get("context"), "context_zh": e.get("context_zh"),
            "formula_number": e.get("formula_number"),
            "latex_source": "page_vision",
            "anchor_type": e.get("anchor_type"),
            "anchor_precision": e.get("anchor_precision"),
            "source_item_ids": e.get("source_item_ids"),
            "duplicate_rows": e.get("duplicate_rows"),
            "section_kind": (anchor_row or {}).get("section_kind") or "body",
            "row_ids": [anchor_row["id"]] if anchor_row else [],
        }
        if e.get("formula_block_id"):
            meta.update({"formula_block_id": e["formula_block_id"],
                         "parsed_latex": e.get("parsed_latex"),
                         "grade": e.get("grade"), "crop": e.get("crop"),
                         "align_score": e.get("align_score")})
        first = {"id": first_id,
                 "page": int((anchor_row or {}).get("page") or e.get("page") or 1),
                 "order": int((anchor_row or {}).get("index") or 0)}
        out.append(_make_chunk(
            paper_id, "formula", content, first, first,
            section=(anchor_row or {}).get("section_title") or "",
            section_path=(anchor_row or {}).get("section_path") or "",
            metadata=meta, chunk_id=cid))
    if out:
        log.info("canonical 公式条目 %d 条（%s）：%s", len(out), paper_id,
                 [c["id"].rsplit(":", 1)[-1] for c in out][:6])
    if skipped_no_anchor:
        # 一条汇总，不一条一条打：这些条目定位不到任何位置（无 formula_block_id 也无
        # row_id），强行造 id 只会造出一个指向不存在锚点的 chunk。
        log.warning("公式条目缺少锚点 id，已跳过 %d 条（%s）",
                    skipped_no_anchor, paper_id)
    return out


def _attach_formula_ids(chunks: list[dict], rows: list[dict], entries: list[dict],
                        block_to_row: dict[str, str]) -> None:
    """把"这条公式落在哪条内容行里"写成双向关系（内容块的 ``formula_ids`` + 公式块的指回）。

    内容块 = 正文 chunk **和语义单元**：检索单位现在是单元（见 `docs/语义单元索引-设计.md`），
    所以"哪条公式在这段里"必须挂到单元上，否则"公式上下文"这类判据在单元检索下恒为 0。

    按路分块之后同一批行有 **A、B 两套**内容块，两者都含这条公式 —— 所以这里**都挂**
    （去掉早期的 `break`：那时一条行只有一套块，第一条命中就是唯一答案）。挂两边之后
    无论是按 A 还是按 B 检索，"公式上下文"这条判据都成立。公式块指回**哪一条**由
    `_link_formulas` 按固定优先级挑（B 路单元优先）。
    """
    row_index = {r["id"]: r["index"] for r in rows}
    holders = [c for c in chunks if c["chunk_type"] in ("unit", "text")]
    for e in entries:
        anchor = e.get("formula_block_id") or e.get("id")
        row_id = str(e.get("row_id") or block_to_row.get(str(e.get("formula_block_id") or "")) or "")
        idx = row_index.get(row_id)
        if idx is None:
            continue
        for chunk in holders:
            span = (chunk.get("metadata") or {}).get("row_ids") or []
            if not span:
                continue
            first, last = row_index.get(span[0]), row_index.get(span[-1])
            if first is not None and last is not None and first <= idx <= last:
                chunk["metadata"].setdefault("formula_ids", []).append(anchor)






def _block_row_ids(paper_id: str, rows: list[dict]) -> dict[str, str]:
    """``{块 id: 行 id}``（``anchor_map`` 正向映射：块落在页级产物的哪一行）。

    页**没有译文行**（未翻译页）或块签名太短时映射不到 —— 调用方一律按"该块自成一行"
    处理，行为与没有这次映射时一致。
    """
    try:
        from backend.services.paper import anchor_map, reader_service

        payloads = reader_service.page_translations(paper_id)
        return anchor_map.build_block_row_map(
            [{"id": r["id"], "page": r["page"], "text": r.get("text") or ""} for r in rows],
            anchor_map.rows_by_page(payloads))
    except Exception as exc:  # noqa: BLE001 —— 映射只用于对齐切点，失败不让建索引挂掉
        log.debug("块→行映射失败（%s）：%s", paper_id, exc)
        return {}






def _link_formulas(chunks: list[dict]) -> list[dict]:
    """给公式原子 chunk 补 ``text_chunk_id``：它被内联在哪条内容块里。

    关系是双向的：内容块的 ``metadata.formula_ids`` 列出它包含的公式块，
    公式 chunk 反过来指回内容块（合并/切分之后再算，所以不会指错）。

    内容块 = **语义单元**（优先，检索单位，定位更精）→ 正文 chunk（上下文单位）。

    按路分块之后同一批行有 A、B 两套内容块，公式块只能指回**一条** ——
    固定优先级：**B 路单元 → A 路单元 → B 路正文 → A 路正文**。
    选 B 路（中文）是因为"公式配上下文"主要服务中文阅读与后续的论文理解（用户口径里
    Related Concepts / 公式解释都由中文那条路承担）；A 路那一套仍在 `formula_ids` 里可查。
    排序里带 `id` 兜底，保证**同一个索引版本里结果确定**（不依赖字典顺序/排序稳定性）。
    """
    def rank(c: dict) -> tuple:
        road = (c.get("metadata") or {}).get("road") or ""
        road_rank = 0 if road == "b" else (1 if road == "a" else 2)
        type_rank = 0 if c["chunk_type"] == "unit" else 1
        return (road_rank, type_rank, c["id"])

    owner: dict[str, str] = {}
    holders = [c for c in chunks if c["chunk_type"] in ("unit", "text")]
    holders.sort(key=rank)
    for c in holders:
        for fid in (c.get("metadata") or {}).get("formula_ids") or []:
            owner.setdefault(fid, c["id"])
    for c in chunks:
        if c["chunk_type"] != "formula":
            continue
        target = owner.get(c["block_id"])
        if target:
            c.setdefault("metadata", {})["text_chunk_id"] = target
    return chunks








def _block_row(b: Block, md_map: dict | None = None) -> dict:
    import json

    meta = {}
    try:
        meta = json.loads(b.meta_json or "{}")
    except ValueError:
        meta = {}
    # 正文只在 Markdown（规划 §8 铁律），MySQL 里不存正文
    text = ((md_map or {}).get(b.id, {}) or {}).get("text") or b.preview or ""
    return {"id": b.id, "page": b.page_no, "type": b.type, "order": b.order_global,
            "text": text, "hash": b.content_hash,
            "section": meta.get("section_title") or b.section_id or "",
            # `section_kind` 由 parser 的状态机标注（body / refs / appendix）：
            # 附录内容**要进知识库**，但要能认出来（检索侧将来可降权/过滤）
            "section_kind": meta.get("section_kind") or "body"}


def _make_chunk(paper_id: str, chunk_type: str, content: str, first: dict,
                last: dict, *, section: str = "", section_path: str = "",
                metadata: dict | None = None, parts: list[tuple] | None = None,
                heading_only: bool = False, chunk_id: str | None = None,
                road: str | None = None) -> dict:
    """建一条 chunk 记录。

    `road` = ``"a"``/``"b"`` 时它是**那一路自己的**正文块，id 里带路号
    （``{paper}:A:text:…``）：两路的划分本来就可能不同，id 撞在一起会出现
    "同一条 id 指两份内容"。`road=None` = 图/表/公式这类**两路共用**的原子块。
    """
    cid = chunk_id or f"{paper_id}:{road + ':' if road else ''}{chunk_type}:{first['id']}"
    if not chunk_id and chunk_type == "text" and first["id"] != last["id"]:
        cid = f"{paper_id}:{road + ':' if road else ''}text:{first['id']}-{last['id']}"
    meta = dict(metadata or {})
    if road:
        # 路号进元数据：检索侧要能按路过滤/上报（向量库另有 `road` 列）
        meta.setdefault("road", road)
    if chunk_type in ("text", "figure", "table"):
        # 行首落单的 `**`（`**图 5：…`）会在渲染时原样显示 → 在这里去掉（成对加粗不动）
        content = _strip_unpaired_bold(content)
    if chunk_type == "text":
        # 句子中间的 `$$…$$` 会被渲染成另起一块 → 降级成行内 `$…$`（行首展示公式不动）
        content = _inline_embed_math(content)
        # 解析文本块把展示公式原样带进正文 → 补定界符（否则整段渲染成纯文本）
        content = _wrap_bare_latex(content)
    if section_path:
        meta["section_path"] = section_path
    if chunk_type == "text" and heading_only:
        meta["heading_only"] = True
    return {
        "id": cid, "paper_id": paper_id, "chunk_type": chunk_type,
        "page_no": first["page"], "order_global": first["order"],
        "block_id": first["id"], "end_block_id": last["id"],
        "section": (section or "")[:SECTION_MAX_CHARS],
        "content": content,
        "content_hash": sha256_text(content),
        "content_preview": _safe_preview(content),
        "token_count": _approx_tokens(content),
        "metadata": meta,
        "_parts": parts or [],
        "_heading_only": heading_only,
        "_section_path": section_path,
    }


def _approx_tokens(text: str) -> int:
    cjk = sum(1 for c in text if "\u4e00" <= c <= "\u9fff")
    return int(cjk / 1.5 + (len(text) - cjk) / 4) + 1


# --------------------------------------------------------------------------
# 入库（规划 §21.6 步骤 2–5）
# --------------------------------------------------------------------------


def index_paper(paper_id: str, *, rebuild: bool = False,
                embed: bool = True, progress=None) -> dict:
    """EMBED + INDEX：写 MySQL chunk 元数据 + 生成向量 + 写 LanceDB。"""
    paper_service.get_paper(paper_id)
    chunks = build_chunks_for_paper(paper_id)
    if not chunks:
        raise ValidationError(
            f"没有可用 chunk：{paper_id} 是否尚未解析？")

    cfg = model_manager.get_config(None, "embedding")
    model_id = cfg.get("id")
    dim = (cfg.get("parameters") or {}).get("dim")

    # 索引版本号（换模型即重建，规划 §11.3）
    with session_scope() as sess:
        row = sess.get(Paper, paper_id)
        prev = sess.query(Chunk).filter(Chunk.paper_id == paper_id).first()
        index_version = int(prev.index_version or 0) + 1 if prev else 1
        old_ids = {i for (i,) in sess.query(Chunk.id)
                   .filter(Chunk.paper_id == paper_id).all()}
        sess.query(Chunk).filter(Chunk.paper_id == paper_id).delete()
        for c in chunks:
            sess.add(Chunk(
                id=c["id"], paper_id=paper_id, block_id=c["block_id"],
                chunk_type=c["chunk_type"], page_no=c["page_no"],
                order_global=c["order_global"], section=c["section"],
                content_preview=c["content_preview"],
                content_hash=c["content_hash"], token_count=c["token_count"],
                index_version=index_version, embedding_model_id=model_id,
                metadata_json=json_dumps(c["metadata"]),
            ))
        if row is not None:
            # **建索引即纳入知识库**（用户口径：两个动作合并，不再有单独的"纳入"开关）
            row.in_knowledge_base = True

    embedded = 0
    vector_error = None
    if embed:
        try:
            provider = model_manager.get_embedding()
            batch = int((cfg.get("parameters") or {}).get("batch") or 32)
            vectors: list[list[float]] = []
            for i in range(0, len(chunks), batch):
                part = chunks[i:i + batch]
                vectors.extend(provider.embed([c["content"] for c in part]))
                if progress:
                    progress(min(i + batch, len(chunks)), len(chunks))
            dim = len(vectors[0]) if vectors else dim
            model_manager.remember_dim(cfg, dim)
            from backend.repositories.vector.lancedb_store import vector_store

            vector_store.upsert_chunks(chunks, vectors, index_version,
                                       embedding_model_id=model_id)
            # 旧 id 要显式清掉：chunk id 由**首末块 id** 拼成，切点/合并一变 id 就变，
            # 只按新 id upsert 会把上一版的向量留在库里（检索召回到"已经不存在的片段"，
            # 概览里两侧条数也对不上）。向量是派生数据，删干净是安全的。
            stale = sorted(old_ids - {c["id"] for c in chunks})
            if stale:
                vector_store.delete_chunks(stale)
                log.info("清掉 %d 条上一版残留向量（%s）", len(stale), paper_id)
            embedded = len(vectors)
        except Exception as exc:  # noqa: BLE001
            vector_error = f"{type(exc).__name__}: {str(exc)[:300]}"
            log.warning("向量入库失败（MySQL chunk 元数据已写入）：%s", exc)

    return {
        "paper_id": paper_id, "chunks": len(chunks),
        "embedded": embedded, "index_version": index_version,
        "model_id": model_id, "dim": dim,
        "by_type": _count_by_type(chunks),
        "by_lang": _count_by_lang(chunks),
        "vector_error": vector_error,
        "rebuilt": rebuild,
        "at": iso_utc(now()),
    }


def reindex_changed(paper_id: str, *, embed: bool = True) -> dict:
    """**增量**重建：只重算"内容变了"的那些 chunk（改完页级产物后用，见 `page_edit`）。

    为什么不直接调 `index_paper`：它是**整篇重建 + 整篇重嵌**。实测一篇 22 页 / 352 chunk
    的论文要 30+ 次嵌入调用（几十秒、几十次计费）—— 而一次纠错通常只影响**十几个** chunk，
    每改一处就整篇重嵌，改十处就把单轮问答的时限（600s）吃光了。

    做法：chunk 的 id 与 `content_hash` 都是**确定性**的（`_variant_row`：id 由首末块 id 拼成、
    hash 是正文的 sha256），所以拿"重算出来的 chunk 清单"与库里那批逐个比 hash，就能只挑出
    真正变了的：删掉变了的旧行与已经消失的 id，插入新行，**只嵌这一批**；没变的行连向量都
    原样不动（检索侧不按 `index_version` 过滤，所以不需要给它们升版本号）。

    兜底（都会退回整篇 `index_paper`）：这篇还没建过索引（只嵌几个 chunk 会得到半截索引）、
    或者嵌入模型换过（新旧向量不同源，混在一张表里没法查）。
    """
    paper_service.get_paper(paper_id)
    chunks = build_chunks_for_paper(paper_id)
    if not chunks:
        raise ValidationError(f"没有可用 chunk：{paper_id} 是否尚未解析？")

    cfg = model_manager.get_config(None, "embedding")
    model_id = cfg.get("id")
    with session_scope() as sess:
        rows = sess.query(Chunk).filter(Chunk.paper_id == paper_id).all()
        prev = {r.id: (r.content_hash, int(r.index_version or 0), r.embedding_model_id)
                for r in rows}
    if not prev or any(v[2] != model_id for v in prev.values()):
        return {**index_paper(paper_id, rebuild=True), "mode": "full",
                "changed": len(chunks), "stale_removed": 0}

    version = max(v[1] for v in prev.values()) or 1
    changed = [c for c in chunks
               if prev.get(c["id"], (None,))[0] != c["content_hash"]]
    stale = sorted(set(prev) - {c["id"] for c in chunks})

    with session_scope() as sess:
        drop = [c["id"] for c in changed] + stale
        for i in range(0, len(drop), 500):          # 变了的行先删再插（内容 + 元数据一起更新）
            sess.query(Chunk).filter(Chunk.id.in_(drop[i:i + 500])) \
                .delete(synchronize_session=False)
        for c in changed:
            sess.add(Chunk(
                id=c["id"], paper_id=paper_id, block_id=c["block_id"],
                chunk_type=c["chunk_type"], page_no=c["page_no"],
                order_global=c["order_global"], section=c["section"],
                content_preview=c["content_preview"],
                content_hash=c["content_hash"], token_count=c["token_count"],
                index_version=version, embedding_model_id=model_id,
                metadata_json=json_dumps(c["metadata"]),
            ))
        paper = sess.get(Paper, paper_id)
        if paper is not None:
            paper.in_knowledge_base = True

    embedded = 0
    vector_error = None
    if embed and changed:
        try:
            provider = model_manager.get_embedding()
            batch = int((cfg.get("parameters") or {}).get("batch") or 32)
            vectors: list[list[float]] = []
            for i in range(0, len(changed), batch):
                vectors.extend(provider.embed(
                    [c["content"] for c in changed[i:i + batch]]))
            if vectors:
                model_manager.remember_dim(cfg, len(vectors[0]))
            from backend.repositories.vector.lancedb_store import vector_store

            vector_store.upsert_chunks(changed, vectors, version,
                                       embedding_model_id=model_id)
            if stale:
                vector_store.delete_chunks(stale)
            embedded = len(vectors)
        except Exception as exc:  # noqa: BLE001
            vector_error = f"{type(exc).__name__}: {str(exc)[:300]}"
            log.warning("增量向量入库失败（MySQL chunk 已更新）：%s", exc)
    elif stale:
        try:
            from backend.repositories.vector.lancedb_store import vector_store

            vector_store.delete_chunks(stale)
        except Exception as exc:  # noqa: BLE001
            vector_error = f"{type(exc).__name__}: {str(exc)[:300]}"

    return {
        "mode": "incremental", "paper_id": paper_id, "chunks": len(chunks),
        "changed": len(changed), "stale_removed": len(stale), "embedded": embedded,
        "index_version": version, "model_id": model_id,
        "vector_error": vector_error, "at": iso_utc(now()),
    }


def _count_by_type(chunks: list[dict]) -> dict:
    out: dict[str, int] = {}
    for c in chunks:
        out[c["chunk_type"]] = out.get(c["chunk_type"], 0) + 1
    return out


def _count_by_lang(chunks: list[dict]) -> dict:
    out: dict[str, int] = {}
    for c in chunks:
        lang = c.get("lang") or "en"
        out[lang] = out.get(lang, 0) + 1
    return out


def overview() -> dict:
    """知识库概览（规划 §20.5）。"""
    from sqlalchemy import func

    with session_scope() as sess:
        papers = sess.query(Paper).filter(Paper.deleted_at.is_(None)).all()
        chunk_count = sess.query(func.count(Chunk.id)).scalar() or 0
        by_type = dict(sess.query(Chunk.chunk_type, func.count(Chunk.id))
                       .group_by(Chunk.chunk_type).all())
        # 语言行数按 id 后缀数（`…:zh` / `…:en` / `…:any`）—— 不用解析 metadata_json，
        # 免得依赖 JSON 的序列化空格这种细节（规划 §8：一处事实源）。
        # `any` 是语言无关的公式原子行（本轮新增）：不统计它，总数就对不上。
        by_lang = {
            "zh": int(sess.query(func.count(Chunk.id))
                      .filter(Chunk.id.like("%:zh")).scalar() or 0),
            "en": int(sess.query(func.count(Chunk.id))
                      .filter(Chunk.id.like("%:en")).scalar() or 0),
            "any": int(sess.query(func.count(Chunk.id))
                       .filter(Chunk.id.like("%:any")).scalar() or 0),
        }
        # 路号同样按 id 数（`{paper}:{A|B}:…`）：共用块（图/表/公式）的 id 里没有路号段，
        # 差值就是"shared"。不用解析 metadata_json，与语种统计同一口径。
        by_road = {
            "a": int(sess.query(func.count(Chunk.id))
                     .filter(Chunk.id.like("%:A:%")).scalar() or 0),
            "b": int(sess.query(func.count(Chunk.id))
                     .filter(Chunk.id.like("%:B:%")).scalar() or 0),
        }
        by_road["shared"] = int(chunk_count) - by_road["a"] - by_road["b"]
        index_versions = dict(sess.query(Chunk.paper_id, func.max(Chunk.index_version))
                              .group_by(Chunk.paper_id).all())
        model_ids = [m for (m,) in sess.query(Chunk.embedding_model_id).distinct().all()]
    vector = None
    try:
        from backend.repositories.vector.lancedb_store import vector_store

        vector = vector_store.stats()
    except Exception:  # noqa: BLE001
        vector = {"available": False}
    return {
        "papers_total": len(papers),
        "papers_in_kb": sum(1 for p in papers if p.in_knowledge_base),
        "chunks": int(chunk_count),
        "chunks_by_type": by_type,
        "chunks_by_lang": by_lang,
        "chunks_by_road": by_road,
        "embedding_models": _model_labels([m for m in model_ids if m]),
        "index_versions": index_versions,
        "vector_store": vector,
        "items": _overview_items(papers),
    }


def _model_labels(model_ids: list[str]) -> list[dict]:
    """把 ``embedding_model_id`` 换成**人能看的名字**。

    以前这里直接把 id 贴出去，界面上就出现 `embedding-7036089f` 这种看不懂的徽标
    （用户报"模型显示错误"）。id 认不出来时原样回退，不隐藏。
    """
    import json

    from backend.repositories.mysql.models import ModelConfig

    if not model_ids:
        return []
    with session_scope() as sess:
        rows = (sess.query(ModelConfig.id, ModelConfig.name, ModelConfig.model_name,
                           ModelConfig.parameters_json)
                .filter(ModelConfig.id.in_(model_ids)).all())
    known = {}
    for mid, name, model_name, params_json in rows:
        try:
            dim = (json.loads(params_json or "{}") or {}).get("dim")
        except ValueError:
            dim = None
        label = model_name or name or mid
        known[mid] = {"id": mid, "label": f"{label}（{dim} 维）" if dim else label,
                      "model_name": model_name, "dim": dim}
    return [known.get(m, {"id": m, "label": m, "model_name": None, "dim": None})
            for m in model_ids]


def _local_iso(value) -> str | None:
    """库里的时间 → **本地墙上时间**的 ISO 串 ``YYYY-MM-DDTHH:MM:SS``（``None`` 原样透传）。

    为什么先按 UTC 认领再转本地：库里的 ``datetime`` 列全是 UTC naive
    （`core.utils.now`），原样 ``isoformat()`` 贴给前端就是那条"差 8 小时"的老 bug。
    也不用 `core.utils.iso_utc`（那个带 ``Z``）—— 这里的口径是"与 ``generated_at`` 同一种
    本地写法"，前端直接显示，不再转。
    """
    from datetime import datetime, timezone

    if value is None:
        return None
    if isinstance(value, str):   # SQLite 的 func.max() 可能直接给回字符串（同 trend 的 DATE()）
        value = datetime.fromisoformat(value)
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    # 转本地之后**去掉 tzinfo**：口径是"与 generated_at 同样的朴素本地串"，
    # 带 `+08:00` 就又是一种新写法了（前端只做展示，不需要偏移量）。
    return value.astimezone().replace(tzinfo=None).isoformat(timespec="seconds")


def _overview_items(papers: list[Paper]) -> list[dict]:
    """概览表格的每一行：论文 + chunk 数 + **最近索引时间**。

    ``indexed_at`` = 该篇**现存 chunk 里最晚的** ``Chunk.created_at``。

    它代表"上次建 / 更新索引的时间"（前端叫「最近索引时间」）：重建索引是"删掉旧 chunk
    行 + 写新行"，所以这个时间自然跟着重建走；没有 chunk（没建过索引 / 已移出知识库）
    就是 ``None``。它**不是**论文的修改时间（`Paper.updated_at`：改笔记、划线、改标题
    都会动它，跟索引无关）。格式见 `_local_iso`（与 `index_status` / `trend` 的
    `generated_at` 同一种本地 ISO 写法）。

    计数与最晚时间都走一次分组查询（``GROUP BY paper_id``），不按论文逐篇查。
    """
    from sqlalchemy import func

    with session_scope() as sess:
        counts = dict(sess.query(Chunk.paper_id, func.count(Chunk.id))
                      .group_by(Chunk.paper_id).all())
        last_indexed = dict(sess.query(Chunk.paper_id, func.max(Chunk.created_at))
                            .group_by(Chunk.paper_id).all())
    return [{"id": p.id, "title": p.title, "status": p.status,
             "in_knowledge_base": bool(p.in_knowledge_base),
             "chunks": int(counts.get(p.id, 0)),
             "indexed_at": _local_iso(last_indexed.get(p.id))} for p in papers]


def set_kb_membership(paper_id: str, include: bool) -> dict:
    with session_scope() as sess:
        row = sess.get(Paper, paper_id)
        if row is None or row.deleted_at is not None:
            raise NotFoundError(f"论文不存在：{paper_id}")
        row.in_knowledge_base = include
        sess.flush()
        return {"paper_id": paper_id, "in_knowledge_base": include}


def remove_from_kb(paper_id: str) -> dict:
    """**从知识库移除**：删向量 + 删 chunk 元数据 + 取消"已纳入"标记。

    「纳入 / 移出」原来是只翻一个布尔（索引还留着），用户口径是把它和索引合并：
    纳入 = 建索引；所以"移除"必须真的把索引清掉，否则 MySQL 与向量库会长期不一致。
    论文本体、译文、笔记、高亮**都不动** —— 移除的是知识库（派生数据），不是论文。
    """
    paper_service.get_paper(paper_id)
    from backend.repositories.vector.lancedb_store import vector_store

    vectors = 0
    vector_error = None
    try:
        vectors = vector_store.delete_paper(paper_id)
    except Exception as exc:  # noqa: BLE001
        vector_error = f"{type(exc).__name__}: {str(exc)[:200]}"
        log.warning("移除向量失败（继续清元数据）：%s", exc)
    with session_scope() as sess:
        chunks = (sess.query(Chunk).filter(Chunk.paper_id == paper_id)
                  .delete(synchronize_session=False))
        row = sess.get(Paper, paper_id)
        if row is not None:
            row.in_knowledge_base = False
    return {"paper_id": paper_id, "removed_chunks": int(chunks),
            "removed_vectors": int(vectors), "in_knowledge_base": False,
            "vector_error": vector_error,
            "at": iso_utc(now())}


def reconcile(*, paper_id: str | None = None, fix: bool = False) -> dict:
    """MySQL chunk 元数据 ↔ 向量库 对账。

    - ``orphans``：MySQL 有、向量库没有 —— 换 embedding 模型时向量表被整体重建，
      旧元数据就留了下来（真实案例：`scanned-demo` 的 10 条，导致概览显示"两侧条数不一致"
      且 embedding 模型徽标里多出一个陈旧的 `mock-embedding`）；
    - ``missing``：向量库有、MySQL 没有（元数据被删但向量还在）。

    ``fix=True`` 时两边都清掉（向量库是派生数据，规划 §6.2）。
    """
    from backend.repositories.vector.lancedb_store import vector_store

    store_ids = set(vector_store.chunk_ids(paper_id=paper_id))
    with session_scope() as sess:
        q = sess.query(Chunk.id)
        if paper_id:
            q = q.filter(Chunk.paper_id == paper_id)
        db_ids = {i for (i,) in q.all()}

    orphans = sorted(db_ids - store_ids)
    missing = sorted(store_ids - db_ids)
    removed_meta = removed_vectors = 0
    if fix:
        if missing:
            removed_vectors = vector_store.delete_chunks(missing)
        for i in range(0, len(orphans), 500):
            part = orphans[i:i + 500]
            with session_scope() as sess:
                removed_meta += int(sess.query(Chunk).filter(Chunk.id.in_(part))
                                    .delete(synchronize_session=False))
    return {
        "paper_id": paper_id,
        "mysql_chunks": len(db_ids), "vector_chunks": len(store_ids),
        "orphans": len(orphans), "missing": len(missing),
        "orphan_sample": orphans[:5], "missing_sample": missing[:5],
        "consistent": not orphans and not missing,
        "fixed": bool(fix), "removed_metadata": removed_meta,
        "removed_vectors": int(removed_vectors),
        "at": iso_utc(now()),
    }


def index_status(paper_id: str | None = None) -> dict:
    from sqlalchemy import func

    with session_scope() as sess:
        q = sess.query(Chunk)
        if paper_id:
            q = q.filter(Chunk.paper_id == paper_id)
        total = q.count()
        with_hash = q.filter(Chunk.content_hash.isnot(None)).count()
        versions = (sess.query(Chunk.paper_id, func.max(Chunk.index_version))
                    .group_by(Chunk.paper_id).all())
        db_ids = {i for (i,) in sess.query(Chunk.id).all()}
    vector = None
    store_ids: set[str] = set()
    try:
        from backend.repositories.vector.lancedb_store import vector_store

        store_ids = set(vector_store.chunk_ids(paper_id=paper_id))
        vector = {"available": True, "count": len(store_ids)}
    except Exception as exc:  # noqa: BLE001
        vector = {"available": False, "error": str(exc)[:200]}
    orphans = len(db_ids - store_ids) if not paper_id else None
    missing = len(store_ids - db_ids) if not paper_id else None
    # 维度体检：**模型声明的维数**（配置 `parameters.dim`）与**向量库实际的维数**（表 schema）
    # 必须一致。换嵌入模型（比如 2560 → 1024）时，旧库不会自动变 —— 有了这两个数与
    # `dim_match`，界面上就能一眼看出"库还是旧维度、要重建索引"，而不是等检索报错。
    # 实测（2026-09-27）：两边都是 2560、条数 706↔706。
    model_dim: int | None = None
    store_dim: int | None = None
    try:
        from backend.services.model.model_manager import model_manager

        model_dim = int((model_manager.get_config(None, "embedding").get("parameters") or {})
                        .get("dim") or 0) or None
    except Exception as exc:  # noqa: BLE001
        log.debug("取嵌入模型维数失败（忽略）：%s", exc)
    try:
        from backend.repositories.vector import lancedb_store as _ls

        table = _ls.vector_store._table()
        store_dim = _ls._vector_dim(table) if table is not None else None
    except Exception as exc:  # noqa: BLE001
        log.debug("取向量库维数失败（忽略）：%s", exc)
    dim_match = None if (model_dim is None or store_dim is None) else (model_dim == store_dim)
    return {"paper_id": paper_id, "chunks": total, "chunks_with_hash": with_hash,
            "index_versions": dict(versions), "vector": vector,
            "model_dim": model_dim, "store_dim": store_dim, "dim_match": dim_match,
            # 只看**全库**对账（单篇视图下这两个数没意义，交给 /knowledge/reconcile）
            "orphan_chunks": orphans, "missing_vectors": missing,
            "consistent": None if paper_id else (not orphans and not missing)}


def index_trend(days: int = 30) -> dict:
    """索引增长趋势（设置页「索引管理」）：每天新增 + 累计。

    **这是"现存记录口径"，不是历史流水。** 没有趋势表：直接对 ``paper`` / ``chunk``
    表**现有行**的 ``created_at`` 分桶。所以有两个必然的偏差，写在这里免得后人当 bug 查：

    - 删掉的论文不进图（``deleted_at IS NOT NULL`` 的行不算），移除知识库的 chunk 行
      本来就已经删了；
    - **重建索引会重置该篇 chunk 的创建时间** —— 曲线上会看到"某天突然掉下去/跳上来"，
      那不是统计错，是这份数据本身没有留历史流水。

    - 窗口：``days`` 夹取到 1..180，右端 = 服务器本地"今天"，左端 = 今天 -(days-1) 天；
      每天一条、**连续不缺天**（没数据的日子补 0，前端画图不用自己补洞）。
    - ``papers`` / ``chunks``：当天新增条数；``cum_*``：窗口开始**之前**的累计 + 当天为止
      的累加，所以单调不减、最后一天等于 ``overview()`` 的 ``papers_total`` / ``chunks``。
    - 分桶用 SQL 的 ``func.date(created_at)``：MySQL ``DATE()`` 与 SQLite ``date()`` 都认；
      ``DATE_FORMAT`` 只有 MySQL 有，测试用 SQLite 会直接报错。

    ⚠️ 库里的 ``created_at`` 是 **UTC naive**（全库约定），窗口边界取的是**本地日期**：
    东八区下 UTC 的"今天"可能还是本地"昨天"，跨日附近的一两条会落在前一天的柱子上。
    柱子口径是"记录写进库的那天"，不做时区折算 —— 折算反而会让累计数与 overview 对不上。
    "未来"时间戳（时钟偏差）折进今天，保证最后一天的累计与总数严格相等。

    只读：不建表、不写库、不触发任务。
    """
    from datetime import date, datetime, timedelta

    from sqlalchemy import func

    days = max(1, min(30 if days is None else int(days), 180))
    today = date.today()
    start = today - timedelta(days=days - 1)
    start_key = start.isoformat()
    today_key = today.isoformat()

    def _by_day(rows) -> dict[str, int]:
        # MySQL 的 DATE() 回 datetime.date、SQLite 的 date() 回 'YYYY-MM-DD' 字符串
        out: dict[str, int] = {}
        for day, count in rows:
            if day is None:
                continue
            key = str(day)[:10]
            if key > today_key:  # 时钟偏差写进"未来"的行：折进今天，否则累计会漏
                key = today_key
            out[key] = out.get(key, 0) + int(count)
        return out

    with session_scope() as sess:
        # 只取窗口内的分桶 + 窗口前的累计（整表按天分组在长库上会返回一堆用不上的日期）。
        paper_rows = (sess.query(func.date(Paper.created_at), func.count(Paper.id))
                      .filter(Paper.deleted_at.is_(None),
                              func.date(Paper.created_at) >= start_key)
                      .group_by(func.date(Paper.created_at)).all())
        chunk_rows = (sess.query(func.date(Chunk.created_at), func.count(Chunk.id))
                      .filter(func.date(Chunk.created_at) >= start_key)
                      .group_by(func.date(Chunk.created_at)).all())
        papers_before = int(sess.query(func.count(Paper.id))
                            .filter(Paper.deleted_at.is_(None),
                                    func.date(Paper.created_at) < start_key).scalar() or 0)
        chunks_before = int(sess.query(func.count(Chunk.id))
                            .filter(func.date(Chunk.created_at) < start_key).scalar() or 0)

    papers_per_day = _by_day(paper_rows)
    chunks_per_day = _by_day(chunk_rows)
    series = []
    cum_papers, cum_chunks = papers_before, chunks_before
    for i in range(days):
        day = (start + timedelta(days=i)).isoformat()
        new_papers = papers_per_day.get(day, 0)
        new_chunks = chunks_per_day.get(day, 0)
        cum_papers += new_papers
        cum_chunks += new_chunks
        series.append({"date": day, "papers": new_papers, "chunks": new_chunks,
                       "cum_papers": cum_papers, "cum_chunks": cum_chunks})
    return {"days": days,
            "generated_at": datetime.now().isoformat(timespec="seconds"),
            "series": series,
            "note": "按现存记录的创建时间统计：重建索引会重置该篇 chunk 的创建时间"}


def new_chunk_id(paper_id: str, chunk_type: str) -> str:
    return f"{paper_id}:{chunk_type}:{new_id('c')[-8:]}"


# --------------------------------------------------------------------------
# 知识条目的图片（插图 / 表格 / 公式的裁图）
#
# 用户口径：**图片也是知识库的一类内容** —— 检索命中插图/表格/公式时要能看到缩略图，
# 浏览"插图"时要能像图库一样翻。所以每个条目都带一个浏览器能直接取的 `image_url`。
#
# 两个来源，优先级不同：
# 1. 插图 / 表格 → **按图注锚定的裁图**（`figure_regions`），与阅读器里显示的是**同一张**
#    （阅读器早就因为"解析阶段的裁图会把相邻的图和小标题一起裁进来"换成了这张）；
# 2. 公式（以及匹配不到区域时的插图/表格）→ **解析阶段已经落盘的裁图**，
#    走 `data/papers/` 的静态挂载（`main._mount_static` 把 `/papers` 指到它）。
#
# 拿不到图就返回 `None`：前端不显示缩略图，**绝不塞一张错图**。
# --------------------------------------------------------------------------

#: 图号识别（`Figure 3` / `Fig. 3` / `Table 1` / `图 2`）—— 与 figure_regions 同一套规则
_FIG_NUMBER_RE = re.compile(r"(?:Figure|Fig\.?|Table|图|表)\s*(\d{1,3})", re.IGNORECASE)


def _static_crop_url(image_path: str | None) -> str | None:
    """解析阶段的裁图文件 → 浏览器可取的 URL（不在论文目录下 / 文件不在 → None）。"""
    if not image_path:
        return None
    try:
        root = Path(get_settings().papers_dir).resolve()
        path = Path(str(image_path)).resolve()
        rel = path.relative_to(root)
    except (ValueError, OSError):        # 不在 data/papers 下（或路径非法）
        return None
    if not path.is_file():
        return None
    return f"/papers/{rel.as_posix()}"


def _figure_region_url(paper_id: str, chunk_type: str, page_no: int,
                       caption: str) -> str | None:
    """插图/表格 → 「按图注锚定的裁图」URL（同页 + 同类 + 图号相同）。

    图号缺失时用图注文本前缀兜底；仍匹配不到返回 `None`（退到解析阶段那张裁图）。
    """
    from backend.services.paper import figure_regions

    if not paper_id or page_no <= 0:
        return None
    try:
        regions = figure_regions.page_regions(paper_id, int(page_no))
    except Exception as exc:  # noqa: BLE001 —— 缺 IR / 读盘失败都不该让检索整条失败
        log.debug("读取插图区域失败（%s p%s）：%s", paper_id, page_no, exc)
        return None
    kind = "figure" if chunk_type == "figure" else "table"
    match = _FIG_NUMBER_RE.search(caption or "")
    number = match.group(1) if match else ""
    head = (caption or "").strip()[:40]
    for i, region in enumerate(regions):
        if region.get("kind") != kind:
            continue
        # `v=<区域哈希>`：裁图接口允许浏览器缓存 7 天，URL 不加版本号的话，
        # 区域算法一变，知识库缩略图会一直显示旧图（实测事故，见 figure_regions.media_of_page）。
        # 版本串里还编进**图注开关**（`region_version`）：开关不影响 bbox，光靠哈希挡不住旧图。
        ver = figure_regions.region_version(region, paper_id)
        suffix = f"?v={ver}" if ver else ""
        if number and str(region.get("number") or "") == number:
            return (f"{get_settings().api_prefix}/papers/{paper_id}"
                    f"/figures/region/{int(page_no)}/{i}{suffix}")
        if not number and head and (region.get("caption") or "").strip()[:40] == head:
            return (f"{get_settings().api_prefix}/papers/{paper_id}"
                    f"/figures/region/{int(page_no)}/{i}{suffix}")
    return None


def chunk_image_url(paper_id: str, chunk_type: str, page_no: int,
                    metadata: dict | None = None) -> str | None:
    """知识条目的缩略图 URL；正文（text）没有图，返回 `None`。"""
    meta = metadata or {}
    if chunk_type in ("figure", "table"):
        url = _figure_region_url(paper_id, chunk_type, page_no,
                                 str(meta.get("caption") or ""))
        return url or _static_crop_url(meta.get("image_path"))
    if chunk_type == "formula":
        return _static_crop_url(meta.get("crop"))
    return None


def _chunk_row_meta(chunk_id: str) -> dict:
    """MySQL 里那条 chunk 元数据（token 数 / 索引版本）；没有就返回空。"""
    with session_scope() as sess:
        row = sess.get(Chunk, chunk_id)
        if row is None:
            return {}
        return {"tokens": row.token_count, "index_version": row.index_version,
                "content_hash": row.content_hash}


def chunk_detail(chunk_id: str) -> dict:
    """单个 chunk 的**完整正文**（知识库页「展开全文」用，规划 §8：全文只在向量库）。

    为什么单独开一个接口：列表接口按规划 §8 只下发 200 字 preview，而"展开全文"要的正是全文。
    这里只读那一条向量行（顺便把 MySQL 的 token/版本补齐），不触发任何任务、不写任何状态。

    **不下发同 `pair_id` 的对侧语种**（用户口径：展开全文只看当前 chunk，中英对照回阅读器）。
    只给 `pair_id`，需要时前端/后续再取。
    """
    from backend.repositories.vector.lancedb_store import vector_store

    cid = str(chunk_id or "").strip()
    if not cid:
        raise ValidationError("缺少 chunk_id")
    rows = vector_store.get_rows([cid])
    if not rows:
        raise NotFoundError(f"chunk 不存在或还没入库：{cid}")
    row = rows[0]
    meta = row.get("metadata") or {}
    paper_id = str(row.get("paper_id") or "")
    page_no = int(row.get("page_no") or 0)
    chunk_type = str(row.get("chunk_type") or "")
    content = str(row.get("content") or "")
    extra = _chunk_row_meta(cid)
    return {
        "chunk_id": cid,
        "paper_id": paper_id,
        "paper_title": _paper_titles([paper_id]).get(paper_id, paper_id) if paper_id else "",
        "block_id": row.get("block_id"), "page": page_no,
        "section": row.get("section"), "chunk_type": chunk_type,
        "lang": row.get("lang") or "en",
        "pair_id": meta.get("pair_id") or None,
        "content": content, "chars": len(content),
        "preview": truncate(content, 200),
        "tokens": extra.get("tokens"), "index_version": extra.get("index_version"),
        "image_url": chunk_image_url(paper_id, chunk_type, page_no, meta),
        # 公式条目的完整契约（用户口径 §7）：来源 = 页级 canonical 清单，
        # 锚点精度与解析侧对应信息一并下发（知识库页据此渲染 KaTeX / 显示锚点精度）。
        "latex": meta.get("latex") or None,
        "latex_source": meta.get("latex_source") or None,
        "display": bool(meta.get("display")) if meta.get("display") is not None else None,
        "row_id": meta.get("row_id") or None,
        "context": meta.get("context") or None,
        "context_zh": meta.get("context_zh") or None,
        "formula_number": meta.get("formula_number") or None,
        "anchor_type": meta.get("anchor_type") or None,
        "anchor_precision": meta.get("anchor_precision") or None,
        "formula_block_id": meta.get("formula_block_id") or None,
        "parsed_latex": meta.get("parsed_latex") or None,
        "grade": meta.get("grade") or None,
    }


# --------------------------------------------------------------------------
# 相似检索（规划 §19.1 / §20.4；任务书功能⑥「跨文献技术比对」）
# --------------------------------------------------------------------------


def similar(payload: dict) -> dict:
    """跨文献技术相似度检索：向量 + 词法 → RRF → 重排。

    查询可以是**一段文本**，也可以是**库里已有的 chunk**（`chunk_id`）——
    后者是"我这篇的这段技术，别的论文里谁在做类似的"这个用法的主路径：
    直接拿该 chunk 的原文当查询，并把它自己排除掉。
    """
    from backend.services.knowledge import retrieval

    query = str(payload.get("query") or "").strip()
    chunk_id = payload.get("chunk_id")
    paper_id = payload.get("paper_id") or None
    source = None
    exclude_chunks: list[str] = []

    if chunk_id:
        from backend.repositories.vector.lancedb_store import vector_store

        rows = vector_store.get_rows([chunk_id])
        if not rows:
            raise NotFoundError(f"chunk 不存在或还没入库：{chunk_id}")
        row = rows[0]
        query = query or str(row.get("content") or "").strip()
        paper_id = paper_id or row.get("paper_id")
        exclude_chunks = [chunk_id]
        source = {"chunk_id": chunk_id, "paper_id": row.get("paper_id"),
                  "block_id": row.get("block_id"), "page": row.get("page_no"),
                  "lang": row.get("lang")}

    if not query:
        raise ValidationError("要么给 query，要么给库里已有的 chunk_id")

    scope = str(payload.get("scope") or ("cross" if paper_id else "all"))
    if scope not in ("all", "cross", "paper"):
        raise ValidationError("scope 只能是 all / cross / paper")

    chunk_types = payload.get("chunk_types") or None
    langs = payload.get("langs") or None
    roads = payload.get("roads") or None
    top_k = payload.get("top_k")
    result = retrieval.search(
        query, mode="similar", scope=scope, paper_id=paper_id,
        chunk_types=chunk_types, langs=langs, roads=roads,
        exclude_chunks=exclude_chunks,
        top_k=int(top_k) if top_k else None,
        # 列表型结果（知识库检索页）：同一节至多 2 条，别让一节占满整页
        section_cap=retrieval.SECTION_CAP,
    )
    items = [public_item(i) for i in _decorate_items(result["items"])]
    return {
        "implemented": True,
        "query": query,
        "source": source,
        "scope": scope,
        "items": items,
        "retrieval": result["retrieval"],
    }


def _decorate_items(items: list[dict]) -> list[dict]:
    """补上论文标题与展示用摘录（检索层不该知道"标题"这种业务字段）。

    **保留 `content` / `metadata`**：RAG 的 ContextBuilder 要用原文与 `pair_id`
    （踩过一次：装饰时把 `content` 丢了，上下文里一条来源都拼不出来）。
    对外响应在各接口边界上再裁掉这两个字段。
    """
    titles = _paper_titles(sorted({i.get("paper_id") for i in items if i.get("paper_id")}))
    out = []
    for i in items:
        paper_id = str(i.get("paper_id") or "")
        chunk_type = str(i.get("chunk_type") or "")
        page_no = int(i.get("page_no") or 0)
        metadata = i.get("metadata") or {}
        out.append({
            "chunk_id": i.get("chunk_id"), "paper_id": i.get("paper_id"),
            "paper_title": titles.get(i.get("paper_id") or "", ""),
            "block_id": i.get("block_id"), "page": i.get("page_no"),
            "section": i.get("section"), "chunk_type": i.get("chunk_type"),
            "lang": i.get("lang") or "en",
            "langs": i.get("langs") or [i.get("lang") or "en"],
            "snippet": truncate(str(i.get("content") or ""), 260),
            # 图注本身（插图/表格 chunk 的正文就是"插图 <块 id>：<图注>"，
            # 列表里显示图注更干净；正文 chunk 没有这个字段）
            "caption": metadata.get("caption"),
            "content": i.get("content"),
            "metadata": metadata,
            # 插图 / 表格 / 公式的裁图（正文条目为 None）——知识库页靠它出缩略图
            "image_url": chunk_image_url(paper_id, chunk_type, page_no, metadata),
            "scores": {
                "vector": i.get("vector_score"), "lexical": i.get("lexical_score"),
                "rrf": round(float(i.get("rrf") or 0.0), 6),
                "rerank": i.get("rerank_score"),
            },
            "ranks": {"rrf": i.get("rrf_rank"), "rerank": i.get("rerank_rank"),
                      **{f"{k}_rank": v for k, v in (i.get("sources") or {}).items()}},
        })
    return out


#: 只给前端看的字段（`content` / `metadata` / 内部校验用文本都不下发）
PUBLIC_ITEM_FIELDS = ("chunk_id", "paper_id", "paper_title", "block_id", "page",
                      "section", "chunk_type", "lang", "langs", "snippet", "caption",
                      "image_url", "scores", "ranks")


def public_item(item: dict) -> dict:
    return {k: item.get(k) for k in PUBLIC_ITEM_FIELDS if k in item}


def _paper_titles(paper_ids: list[str]) -> dict[str, str]:
    if not paper_ids:
        return {}
    with session_scope() as sess:
        rows = (sess.query(Paper.id, Paper.title)
                .filter(Paper.id.in_(paper_ids)).all())
    return {pid: title or pid for pid, title in rows}
















