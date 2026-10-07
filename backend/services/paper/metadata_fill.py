"""论文元数据：**离线能读的先读**，读不出来的交给模型（或 agent）读全文再填。

用户口径（原话）：*论文元数据我想要么从论文获取自动填写，要么模型翻译后填写（和术语一同生成）*。
2026-10-07 又补了一句：元数据要包含**中英文摘要、中英文关键词、参考文献、所属领域、
核心创新点、前置知识与难度、推荐阅读时间、一句话概括**等，而且**翻译完成后自动执行元数据填写**。

## 三条来源，分工明确

| 来源 | 谁做 | 覆盖字段 | 花钱 |
|---|---|---|---|
| `pdf` | `offline()`：PDF 首页 + 页产物（`ref` 行 / 正文行） | 标题 / 作者 / 会议 / 年份 / **原文摘要** / DOI / 开源地址 / **参考文献** / **推荐阅读时间** | 否 |
| `model` | `from_model()`：读抽样内容（标题 + 摘要 + 各章节开头） | 会议 / 年份 / 中文关键词 / 中文摘要 / 英文关键词 / 领域 / 一句话概括 / 创新点 / 前置知识 / 难度 / 类型 | 一次调用 |
| `agent` | `agent_fill()` → agent 服务 `/v1/meta-fill` | 同 `model`，但**带工具读全文**（能翻页、能检索），质量更高 | 一个 agent 会话 |

`agent` 跑不起来（agent 服务没启动）时**自动回落**到 `model`：「模型补全」按钮永远有结果。

写回策略：默认**只填空字段**（``overwrite=False``）—— 每个字段的来源记在
``paper.meta_sources_json`` 里，所以"这一项是谁填的"看得见；要强制重填就显式传
``overwrite=True``（API 上的 `overwrite` 参数）。
"""

from __future__ import annotations

import re
from datetime import datetime

from backend.core.logging import get_logger
from backend.services.paper import paper_service

log = get_logger(__name__)

# 可补全的字段。标题单独处理：它更可能是用户手改过的。
# 作者只在**离线**这一路出现 —— 模型看不到首页作者块，硬让它猜名字只会编造。
FILL_FIELDS = (
    "authors", "venue", "year", "tags", "abstract",
    "abstract_zh", "keywords_en", "domain", "one_liner", "contributions",
    "prereq", "difficulty", "read_time_min", "paper_type", "doi", "code_url",
)
#: "这篇论文的元数据还缺不缺"的判据（能由模型补、且列表页看得见的那几项）
MISSING_FIELDS = ("venue", "year", "tags", "abstract_zh", "keywords_en",
                  "domain", "one_liner", "difficulty", "read_time_min")

#: 难度 / 类型：**闭集**。给自由文本的话，同一篇论文会出现"简单/入门/初级"三种写法，
#: 界面上就没法用它做筛选了。
DIFFICULTIES = ("入门", "进阶", "专家")
PAPER_TYPES = ("方法", "综述", "数据集", "系统", "理论", "其他")

# 模型这一次调用要产出的元数据。**与文档简报共用**（brief.BRIEF_SCHEMA 里嵌的就是它），
# 所以翻译流程里零额外成本地就把元数据补齐了 —— 用户口径"翻译完成后执行元数据填写"。
META_SCHEMA = {
    "type": "object",
    "properties": {
        "venue": {"type": "string",
                  "description": "会议或期刊名（含年份，如 ICLR 2026）；文中没有就留空"},
        "year": {"type": "integer", "description": "发表年份；判断不出填 0"},
        "tags": {"type": "array", "description": "3~6 个简体中文关键词",
                 "items": {"type": "string"}},
        "keywords_en": {"type": "array",
                        "description": "3~6 个英文关键词（论文自己的 keywords 优先）",
                        "items": {"type": "string"}},
        "abstract_zh": {"type": "string",
                        "description": "论文摘要的中文译文；抽样里没有摘要就留空"},
        "domain": {"type": "string", "description": "所属领域 / 研究方向（≤20 字）"},
        "one_liner": {"type": "string", "description": "一句话概括这篇论文做了什么（≤60 字）"},
        "contributions": {"type": "array",
                          "description": "2~5 条核心创新点，每条 ≤60 字，说清相对已有工作新在哪",
                          "items": {"type": "string"}},
        "prereq": {"type": "array",
                   "description": "2~4 条读懂这篇论文需要的前置知识（概念或技能名）",
                   "items": {"type": "string"}},
        "difficulty": {"type": "string", "enum": list(DIFFICULTIES),
                       "description": "阅读难度：入门 / 进阶 / 专家"},
        "paper_type": {"type": "string", "enum": list(PAPER_TYPES),
                       "description": "论文类型"},
        "read_time_min": {"type": "integer",
                          "description": "推荐阅读时间（分钟，按精读估）；估不出填 0"},
        "doi": {"type": "string", "description": "DOI（没有就留空）"},
        "code_url": {"type": "string", "description": "开源代码 / 项目主页地址（没有就留空）"},
    },
}

META_SYSTEM = """你在整理一篇学术论文的元数据。只依据给定内容判断，**不要猜、不要编造**：
1. venue：会议或期刊名。只有文中明确出现时才填（如 "Published as a conference paper at ICLR 2026" 或页脚的 "Proceedings of ..."）；判断不出就留空字符串。
2. year：发表年份（整数）。文中能明确推出才填，否则填 0。
3. tags：3~6 个简体中文关键词，覆盖研究领域与核心方法。
4. keywords_en：3~6 个英文关键词；论文自己列了 Keywords 就用它，没有就按主题给等价的英文词。
5. abstract_zh：把**论文摘要原文**译成简体中文（≤400 字）。抽样内容里没有摘要就留空字符串 —— 不要用一句话总结冒充摘要。
6. domain：所属领域或研究方向，≤20 字（如"向量检索与量化"）。
7. one_liner：一句话概括它做了什么、解决什么痛点，≤60 字。
8. contributions：2~5 条核心创新点。每条 ≤60 字，并说清**相对已有工作新在哪**；不要照抄"我们提出了 X"。
9. prereq：2~4 条读懂它需要的前置知识（如"乘积量化""注意力机制"）。
10. difficulty：入门 / 进阶 / 专家（按前置知识的门槛判断）。
11. paper_type：方法 / 综述 / 数据集 / 系统 / 理论 / 其他。
12. read_time_min：按精读估的推荐阅读时间（分钟，整数）。
13. doi / code_url：文中出现才填，否则留空字符串。
只输出 JSON，不要输出解释或 Markdown 代码块。"""


# --------------------------------------------------------------------------
# 工具
# --------------------------------------------------------------------------


def _is_empty(value) -> bool:
    return value in (None, "", 0, [], ())


def missing_fields(paper: dict) -> list[str]:
    return [k for k in MISSING_FIELDS if _is_empty(paper.get(k))]


def _clean_list(value, *, limit: int, item_max: int) -> list[str] | None:
    """列表字段归一：去空、去重、截长；一个都没有就返回 None（= 不写这一项）。"""
    raw = value if isinstance(value, (list, tuple, set)) else ([value] if value else [])
    out: list[str] = []
    for item in raw:
        text = str(item or "").strip()
        if not text:
            continue
        text = text[:item_max]
        if text not in out:
            out.append(text)
        if len(out) >= limit:
            break
    return out or None


# --------------------------------------------------------------------------
# 写回
# --------------------------------------------------------------------------


def apply_fills(paper_id: str, fills: dict, *, overwrite: bool = False,
                source: str = "model") -> dict:
    """把 ``fills`` 写回论文行；默认**只填空字段**，返回真正写进去的字段。

    ``source`` 写进 `meta_sources_json`（`pdf` / `model` / `agent`）—— 前端据此标
    "这一项是哪来的"，也让"只填空字段"这件事在界面上可解释。
    """
    clean = {k: v for k, v in (fills or {}).items()
             if k in FILL_FIELDS and not _is_empty(v)}
    if not clean:
        return {}
    paper = paper_service.get_paper(paper_id)
    patch = {k: v for k, v in clean.items() if overwrite or _is_empty(paper.get(k))}
    if patch:
        paper_service.update_paper(paper_id, patch, source=source)
        log.info("元数据补全 %s（%s）：%s", paper_id, source,
                 "、".join(f"{k}={str(v)[:40]}" for k, v in patch.items()))
    return patch


def _title_needs_fix(title: str, paper_id: str) -> bool:
    """标题是不是"明显不对"（文件名 / arXiv 水印 / Word 导出前缀）。

    为什么单独判：标题是用户最可能手动改过的字段，只有它一眼就是错的才自动覆盖。
    实测踩过的坑：某论文首页左侧竖排水印字号比标题还大，于是标题被抽成了
    ``arXiv:1706.03762v5 [cs.CL] 6 Dec 2017``，在库里一直这么显示。
    """
    t = (title or "").strip()
    if not t:
        return True
    if t == paper_id or t.lower().endswith(".pdf"):
        return True
    if re.match(r"^(?:arxiv|doi|https?)\b", t, re.I) or "arxiv:" in t.lower():
        return True
    if re.match(r"^microsoft\s+word\b", t, re.I):
        return True
    return False


# --------------------------------------------------------------------------
# 来源 ①：PDF / 页产物（离线，不花钱）
# --------------------------------------------------------------------------

#: 首页里能直接读出来的 DOI / 开源地址
_DOI_RE = re.compile(r"\b10\.\d{4,9}/[-._;()/:A-Za-z0-9]+\b")
_CODE_RE = re.compile(
    r"https?://(?:github\.com|gitlab\.com|huggingface\.co)/[^\s)\]}\"'<>，。]+", re.I)

#: 精读速度：**字符/分钟**。论文正文中英混排，取折中值（≈ 200 词/分钟）
READ_CHARS_PER_MIN = 900


def scan_rows(paper_id: str) -> list[tuple[int, str, str, str]]:
    """页产物的行 → ``[(页号, 类型, 原文, 译文)]``。

    只装一次：下面几个离线抽取（DOI / 阅读时间 / 参考文献）都要用它，各装一遍就是
    "同一篇论文的页产物被读三遍"（`page_translations` 每次都要读两路 JSON）。

    **两边都带回来**（2026-10-07）：参考文献要拿译文里的《中文标题》跟原文对齐着切
    （见 `split_ref_text`），只留"原文 or 译文"这一侧就做不到了。
    """
    from backend.services.paper import reader_service

    out: list[tuple[int, str, str, str]] = []
    try:
        pages = reader_service.page_translations(paper_id)
    except Exception as exc:  # noqa: BLE001 —— 没有页产物只是"离线抽不到"，不能让补全失败
        log.debug("读页产物失败（忽略）：%s", exc)
        return out
    for payload in pages:
        page = int(payload.get("page") or 0)
        for row in payload.get("rows") or []:
            # ⚠️ 行的键是 `original` / `translation`（见 `reader_service._merge_page`），
            # **不是** `text`：第一版按 `text` 取，于是这个函数永远空手而归 ——
            # 阅读时间永远填不上、DOI/开源地址永远抽不到（参考文献还有值，是靠 Markdown 兜底）。
            original = str(row.get("original") or "")
            translation = str(row.get("translation") or "")
            if original.strip() or translation.strip():
                out.append((page, str(row.get("type") or ""), original, translation))
    return out


def _row_text(original: str, translation: str) -> str:
    """这一行"给人看的文本"：有原文用原文，没有才用译文（单语论文/只译了半边）。"""
    return original or translation


def offline_extras(rows: list[tuple[int, str, str, str]]) -> dict:
    """首页里的 DOI / 开源地址（纯正则，不花一分钱）。

    只读**前两页**的正文：这些标识符要么在首页页脚、要么在摘要附近；翻遍全文
    只会捞到参考文献里别人的 DOI 与别人的仓库。
    """
    text = "\n".join(_row_text(o, t) for page, _kind, o, t in rows if page <= 2)
    out: dict = {}
    if (m := _DOI_RE.search(text)):
        out["doi"] = m.group(0).rstrip(".,;").lower()[:128]
    if (m := _CODE_RE.search(text)):
        out["code_url"] = m.group(0).rstrip(".,;")[:256]
    return out


def estimate_read_time(rows: list[tuple[int, str, str, str]]) -> int | None:
    """推荐阅读时间（分钟）：按正文行字数估。

    只算正文（`para` / `heading`）：公式、图注、参考文献不参与"读一遍"。
    没有页产物（还没翻译）时返回 None —— 别给个"1 分钟"骗人。
    """
    chars = sum(len(_row_text(o, t)) for _page, kind, o, t in rows if kind in ("para", "heading"))
    if chars < 500:
        return None
    return max(1, min(600, round(chars / READ_CHARS_PER_MIN)))


#: 译文里"中文标题"的约定（提示词里写死）：**只把标题译成中文并用《》括起来**
_ZH_TITLE_RE = re.compile(r"《([^》]{1,300})》")
#: 两段拼接处可能多出来的分隔符（`.`、`,`、`·`、空格…）：比对前后各自剥掉
_REF_SEP = " \t.,;:·、，。"
#: 条目前的编号（`[12]` / `(3)` / `4.`）—— 规则切题名时先剥掉
_REF_LABEL_RE = re.compile(r"^\s*(?:\[\d{1,4}\]|\(\d{1,4}\)|\d{1,4}[.)])\s*")
#: 人名片段：`Ran Hadad` / `PF Panter` / `Claude E. Shannon` / `Jianyang Gao` / `AI@Meta LlamaTeam`
#: 两条硬要求：① 每个词以**大写**起头（小写起头的词是机构名/网址/标题）；② **最后一个词至少两
#: 个字符** —— 否则 `Tom B.`、`Dawid J.` 这种"名字中间的缩写"会被当成作者段的结尾
#: （实测：`Max Belitsky, Dawid J. Kopiczko` 会被切成作者=`Max Belitsky, Dawid J.`、标题=`Kopiczko…`）。
_REF_NAME_RE = re.compile(r"^(?:[A-Z]\.?\s+)*[A-Z][\w@&'’\-]+"
                          r"(?:\s+(?:[A-Z]\.?\s+)*[A-Z][\w@&'’\-]+)*$")
#: "出处"该有的样子：年份 / 卷期页码 / 期刊会议关键词
_REF_VENUE_RE = re.compile(
    r"\b(?:19|20)\d{2}\b|\bpp\.|\bvol\.|\bno\.|\d+\s*\(\d+\)|arXiv|doi|Corr\b|"
    r"Proceedings|Conference|Journal|Transactions|Letters|Advances|International|"
    r"Symposium|Workshop|Benchmark|Dataset|preprint|Report|Press|Springer|IEEE|ACM|"
    r"Nature|Science|Systems|"
    r"ICLR|ICML|NeurIPS|CVPR|ACL|EMNLP|AAAI|SIGIR|KDD", re.I)
#: 标题尾巴上的年份（`… models, 2025.`）：那属于出处，不属于标题
_REF_TITLE_YEAR_RE = re.compile(r"[,;]?\s*\(?(?:19|20)\d{2}[a-z]?\)?\s*$")
#: 标题末尾的孤立首字母（`… Koyejo, S`）：那是被切开的作者名，不是标题
_REF_TITLE_TAIL_INITIAL_RE = re.compile(r"(?:^|[\s,(])[A-Z]$")
#: 标题两端要剥掉的 Markdown 强调记号与引号（侧栏不渲染 Markdown，留着就是字面星号）
_TITLE_JUNK = "*_`\"'“”‘’ \t"


def _looks_like_names(segment: str) -> bool:
    """这一段像不像**纯人名列表**（作者段）。

    判据故意严（宁可漏，也不要把机构名或标题当作者剥掉）：
    逗号 / 分号 / `and` / `&` 分开的每一段都必须是"名 姓"或"缩写 姓"，允许结尾 `et al.`。
    `Pgvector search`、`Google Research, New York University`、URL 都会在这里被挡掉。
    """
    seg = re.sub(r"\bet\s+al\.?$", "", str(segment or "").strip().rstrip(".").strip(),
                 flags=re.I).strip().rstrip(",").strip()
    # 上限宽松一点：GPT-3 那篇的作者段有 481 个字符、31 个人（卡在 300 就会漏掉它）
    if not seg or len(seg) > 1200:
        return False
    parts = [p.strip() for p in re.split(r"\s*(?:,|;|/|&|\band\b)\s*", seg) if p.strip()]
    if not parts or len(parts) > 60:
        return False
    return all(_REF_NAME_RE.match(p) for p in parts)


def _clean_title(text: str) -> str:
    """标题清洗：剥两端的强调记号 / 引号，去掉解析器留下的 `<!-- … -->` 标记并归一空白。

    侧栏不渲染 Markdown，留着就是字面星号；`<!-- ===== page 11 ===== -->` 这种页分隔注释
    会被原文读出串进来（实测 GPT-3 那条就夹在标题中间）。
    """
    out = re.sub(r"<!--.*?-->", " ", str(text or ""), flags=re.S)
    return " ".join(out.split()).strip(_TITLE_JUNK).strip()


def _take_ref_title(rest: str) -> tuple[str, bool]:
    """从"作者段之后"取 **（标题, 是否剥掉了尾巴上的年份）**。

    斜体段优先（书名/期刊名常斜体）；否则取到**第一个"后面像出处"的句号**为止：
    取到句号那一版还要过一道闸 —— 剩下的部分必须像出处（年份 / pp.vol. / 期刊会议词）。
    否则 `… in Proc. of the U.S. …` 这种标题里带缩写的会被切短（切错不如不切）。
    标题尾巴上的 `, 2025` 属于出处（`authors. title, year.` 这种 ACM 样式），一并剥掉。
    """
    m = re.match(r"^\s*(\*[^*\n]{8,300}\*)\s*\.?\s*(.*)$", rest, re.S)
    if m:
        return _clean_title(m.group(1)), False
    for j, ch in enumerate(rest):
        if ch != ".":
            continue
        title, venue = rest[:j].strip(), rest[j + 1:].strip()
        stripped = False
        if _REF_TITLE_YEAR_RE.search(title):
            title, stripped = _REF_TITLE_YEAR_RE.sub("", title).rstrip(_REF_SEP), True
        if len(title) < 12 or _REF_TITLE_TAIL_INITIAL_RE.search(title) or title.endswith("-"):
            continue
        if _REF_VENUE_RE.search(venue) or (stripped and not venue):
            return _clean_title(title), stripped
    return "", False


def split_ref_by_rule(raw: str) -> dict | None:
    """**不靠译文**、只按"作者段 / 标题 / 出处"的形状切一条参考文献。

    用户口径（2026-10-07）：像 `Ran Hadad and Uri Erez. Dithered quantization via orthogonal
    transformations. IEEE Transactions on Signal Processing, 64(22):5887–5900, 2016.` 这样，
    **作者名后面是 `.`** 的，侧栏直接显示**文献名称**，不要再把作者堆在脸上；
    形状对不上就保持默认（整条原文照旧显示）。

    判据（宁可漏，不许错）：
    1. 某个句号之前那段必须是**纯人名列表**（见 `_looks_like_names`）；
    2. 标题段 ≥ 12 字符，或本身就是一段斜体；
    3. 以句号收尾的那一版，剩下的部分必须像**出处**（或标题尾巴就是那个年份）。

    任一条不满足 → ``None``（调用方回退到"显示整条原文"）。
    """
    text = _REF_LABEL_RE.sub("", str(raw or "").strip())
    for i, ch in enumerate(text):
        if ch != ".":
            continue
        head, rest = text[:i], text[i + 1:].lstrip()
        if not rest or not _looks_like_names(head):
            continue
        title, _stripped = _take_ref_title(rest)
        if title:
            return {"title": None, "title_en": title, "authors": f"{head.strip()}."}
    return None


def split_ref_text(raw: str, translated: str | None) -> dict:
    """一条参考文献的「原文 + 译文」→ ``{title, title_en, authors}``。

    为什么切得动：提示词要求译文**只把标题换成《中文标题》**，作者与会议/年份/链接逐字照抄原文。
    于是译文与原文是**同一副文字骨架**，可以这样切：

    - `《…》` **之前**那段 = 作者段（与原文逐字相同）→ 从原文头部切掉；
    - `《…》` **之后**那段 = 会议/年份/DOI/链接 → 从原文尾部切掉；
    - 剩下的中间那段就是**英文标题**（连它自己的句号一起留着）。

    没有《》时（还没重译 / 模型没照抄）**退到纯规则切法** `split_ref_by_rule` ——
    用户口径：作者名后面是 `.` 的就显示文献名称，形状对不上才显示整条原文。
    """
    raw = str(raw or "").strip()
    out = {"title": None, "title_en": None, "authors": None}
    text = str(translated or "")
    m = _ZH_TITLE_RE.search(text)
    if m:
        out["title"] = m.group(1).strip() or None
        # 作者段只剥**空白**（不剥标点）：它要能原样对上原文的前缀，也方便日后显示
        head = text[:m.start()].rstrip()
        tail = text[m.end():].lstrip(_REF_SEP)
        if head and raw.startswith(head):
            out["authors"] = head
            body = raw[len(head):].lstrip(_REF_SEP)
            if tail and body.endswith(tail):
                body = body[: len(body) - len(tail)].rstrip(_REF_SEP)
            out["title_en"] = _clean_title(body) or None
    if not out["title_en"]:
        # 英文标题缺了（没译文、或模型没照抄作者段）→ 走规则切；切不出来就维持"只显示整条原文"
        rule = split_ref_by_rule(raw)
        if rule:
            out["title_en"] = rule["title_en"]
            out["authors"] = out["authors"] or rule["authors"]
    return out


def reference_pairs(paper_id: str,
                    rows: list[tuple[int, str, str, str]] | None = None
                    ) -> list[tuple[str, str | None]]:
    """参考文献条目（离线）→ ``[(原文, 译文 or None)]``。

    来源是**页产物里 `ref` 类型的行** —— 解析阶段已经把参考文献切成了独立的块，
    阅读器用的就是它。页产物还没有时退回解析出的 Markdown（找 References / 参考文献 那一节），
    那时只有原文（Markdown 里没有逐块译文）。

    为什么要连译文一起回（2026-10-07）：译文里带着《中文标题》，与原文对齐着切就能同时得到
    「中文标题 / 英文标题 / 作者段」（见 `split_ref_text`）—— 侧栏要中英对照着显示。
    """
    out: list[tuple[str, str | None]] = []
    data = rows if rows is not None else scan_rows(paper_id)
    for _page, kind, original, translation in data:
        if kind != "ref":
            continue
        raw = _ws(original)
        if not raw:
            raw = _ws(translation)                 # 只译了半边：拿译文当原文
        zh = _ws(translation) or None
        if raw:
            out.append((raw, zh))
    if out:
        return out
    # **没有编号**的参考文献（ICLR 那种 `Elastic search., 2025. https://…`）不会被
    # `split_blocks` 认成 `ref` 类型 → 退回 Markdown 抽条目；但**译文就在页产物的行里**
    # （块模式下第 i 行 ↔ A 第 i 块），所以拿"空白归一的原文"去行里**认领译文**，
    # 中文标题才切得出来（实测 turboquant 的 75 条全靠这条路）。
    by_original: dict[str, str] = {}
    for _page, _kind, original, translation in data:
        key, zh = _ws(original), _ws(translation)
        if key and zh:
            by_original.setdefault(key, zh)
    return [(_clean_raw(raw), by_original.get(_ws(raw)))
            for raw in _references_from_markdown(paper_id)]


def _clean_raw(text: str) -> str:
    """落库的 `raw`（tooltip 里显示的那一份）：去掉结构注释、折叠空白，**保留**斜体记号。"""
    out = re.sub(r"<!--.*?-->", " ", str(text or ""), flags=re.S)
    return " ".join(out.split())


def _ws(text: str) -> str:
    """归一化到"可比较"：去结构注释 + 去 Markdown 强调记号 + 空白折叠。

    Markdown 抽出来的条目与页产物的行是**同一段文字**，只差这几处：
    换行 / 多空格、夹在中间的 `<!-- blk:… -->`（`<!-- ===== page 11 ===== -->` 也会夹进来）、
    以及 A 路转录给出处加的斜体（`*arXiv preprint arXiv:2303.08774*` 对 `arXiv preprint arXiv:2303.08774`）。
    """
    out = re.sub(r"<!--.*?-->", " ", str(text or ""), flags=re.S)
    return " ".join(out.replace("*", "").replace("`", "").split())


def references(paper_id: str, rows: list[tuple[int, str, str, str]] | None = None) -> list[str]:
    """只要原文那一侧（老的调用方/测试用它；要中英成对就用 `reference_pairs`）。"""
    return [raw for raw, _zh in reference_pairs(paper_id, rows)]


#: Markdown 里的参考文献小节标题
_REF_HEAD_RE = re.compile(r"^\s{0,3}#{1,6}\s*(?:\d+\.?\s*)?"
                          r"(references|bibliography|参考文献)\s*$", re.I)
#: 条目开头：`[12]` / `(12)` / `12.` （参考文献几乎都这么编号）
_REF_ITEM_RE = re.compile(r"^\s*(?:\[\d{1,4}\]|\(\d{1,4}\)|\d{1,4}[.)])\s+")
#: 解析器写在每个块前面的结构标记（**结构，不是条目内容**）
_MD_BLOCK_RE = re.compile(
    r"^\s*<!--\s*blk:\S+\s+type:(?P<type>\w+)\s+page:(?P<page>\d+)\s+"
    r"bbox:(?P<x0>[\d.]+),(?P<y0>[\d.]+),(?P<x1>[\d.]+),(?P<y1>[\d.]+)\s*-->\s*$")
#: 同上：块锚点
_MD_ANCHOR_RE = re.compile(r"^\s*<a id=\"[^\"]*\"></a>\s*$")
#: "顶格"的容差（pt）：同一栏里只有顶到左边距的那一行才是新条目的开头，续行会缩进
_REF_INDENT_TOL = 3.0


def _parse_ref_section(text: str) -> list[str]:
    """References 节（Markdown 全文）→ 条目列表。**纯函数**，便于单测。

    实测要容忍的两件事（TurboQuant / ICLR-26 那篇，用户 2026-10-07 报"0 条"）：
    ① 条目**没有编号**（`Elastic search., 2025. https://…`），不是 `[12] Title…` 那种；
    ② 每个块之间夹着解析器写的结构标记：`<!-- blk:… type:ref … bbox:… -->` 与 `<a id=…></a>`。

    切条目的两套办法：
    - **有结构标记**（且里面有 `type:ref` 块）→ 按 bbox 的**左边界**切：顶格 = 新条目，
      缩进 = 上一条的续行（这条论文里顶格 x0=108、续行 x0=118/258，一眼分得开）；
    - 没有标记（别的产物 / 更老的 md）→ 退回"空行分段"，段内有编号就按编号开新条。
    """
    lines = text.splitlines()
    start = next((i for i, ln in enumerate(lines) if _REF_HEAD_RE.match(ln)), None)
    if start is None:
        return []

    body: list[str] = []                       # 这一节的正文行（去掉了结构标记）
    marks: list[tuple[int, str, float]] = []   # (在 body 里的下标, 块类型, 左边界 x0)
    for ln in lines[start + 1:]:
        if ln.startswith("#"):                 # 下一个标题 → 本节结束
            break
        m = _MD_BLOCK_RE.match(ln)
        if m:
            marks.append((len(body), m.group("type"), float(m.group("x0"))))
            continue
        if _MD_ANCHOR_RE.match(ln):
            continue
        body.append(ln)

    refs = _split_refs_by_bbox(body, marks)
    if refs:
        return refs
    return _split_refs_by_paragraph(body)


def _split_refs_by_bbox(body: list[str], marks: list[tuple[int, str, float]]) -> list[str]:
    """按"块结构 + 左边界"切条目：只有顶格那一行才开新条目，缩进的是它的续行。"""
    ref_marks = [(i, x0) for i, kind, x0 in marks if kind == "ref"]
    if not ref_marks:
        return []
    base = min(x0 for _i, x0 in ref_marks)          # 本节最左边（= 条目的开头那一栏）
    bounds = [i for i, _kind, _x0 in marks] + [len(body)]
    out: list[str] = []
    pending: list[str] = []                         # 还没归到任何条目的"缩进的续行"
    for idx, x0 in ref_marks:
        end = next((b for b in bounds if b > idx), len(body))
        text = " ".join(" ".join(body[idx:end]).split())
        if not text:
            continue
        if x0 <= base + _REF_INDENT_TOL:
            # 顶格 = 新条目；它前面那些缩进的块（右栏被切出来的半截 URL 之类）归到它身上
            if pending:
                text = f"{' '.join(pending)} {text}"
                pending = []
            out.append(text)
        elif out:
            out[-1] = f"{out[-1]} {text}"
        else:
            pending.append(text)
    if not out:                                     # 整节都是缩进的（没有顶格行）：只能各自成条
        return [t for t in pending if len(t) > 8]
    return [t for t in out if len(t) > 8]


def _split_refs_by_paragraph(body: list[str]) -> list[str]:
    """没有块结构时的兜底：**有编号就按编号切**（编号可能不带空行紧跟下一条），
    一条编号都没有才退回"空行分段、一段一条"。"""
    if any(_REF_ITEM_RE.match(ln) for ln in body):
        out: list[str] = []
        for ln in body:
            text = " ".join(ln.split())
            if not text:
                continue
            if _REF_ITEM_RE.match(text) or not out:
                out.append(text)
            else:
                out[-1] = f"{out[-1]} {text}"       # 续行
        return [t for t in out if len(t) > 8]

    paras: list[str] = []
    buf: list[str] = []
    for ln in body:
        if ln.strip():
            buf.append(ln)
        elif buf:
            paras.append(" ".join(" ".join(buf).split()))
            buf.clear()
    if buf:
        paras.append(" ".join(" ".join(buf).split()))
    return [p for p in paras if len(p) > 8]


def _references_from_markdown(paper_id: str) -> list[str]:
    paths = paper_service.paper_paths(paper_id)
    md_path = paths.get("markdown")
    if md_path is None or not md_path.is_file():
        return []
    try:
        text = md_path.read_text(encoding="utf-8")
    except OSError:
        return []
    return _parse_ref_section(text)


def _cap(text: str | None, limit: int) -> str | None:
    """截到列宽上限；空串一律回 `None`（NULL 才表示"没有"，与"读过、确实是空"分开）。"""
    return text[:limit] if text else None


def save_references(paper_id: str, items: list[str] | list[tuple[str, str | None]]) -> int:
    """把参考文献写进 `paper_reference`（**整篇替换**：重抽就该覆盖旧的）。

    为什么整篇替换而不是增量：抽取是纯函数（同一份页产物 → 同一份条目），
    增量比对只会带来"删掉的那几条还留在库里"的脏数据。

    `items` 现在收 **`(原文, 译文)` 成对**（2026-10-07）：落库时顺手把中文标题、英文标题、作者段
    一起拆出来存好（`split_ref_text`），侧栏才能中英对照显示、并把作者那一段收起来。
    也兼容老写法（一串纯字符串）—— 那时只有原文，中文标题留空。
    """
    import uuid

    from backend.repositories.mysql.models import PaperReference
    from backend.repositories.mysql.session import session_scope

    pairs: list[tuple[str, str | None]] = []
    for it in (items or []):
        if isinstance(it, (tuple, list)):
            raw, zh = (list(it) + [None])[:2]
        else:
            raw, zh = it, None
        if str(raw or "").strip():
            pairs.append((str(raw), str(zh) if zh else None))
    pairs = pairs[:500]
    with session_scope() as sess:
        sess.query(PaperReference).filter(PaperReference.paper_id == paper_id).delete()
        for i, (raw, zh) in enumerate(pairs, 1):
            split = split_ref_text(raw, zh)
            sess.add(PaperReference(id=uuid.uuid4().hex[:32], paper_id=paper_id,
                                    order_no=i, raw=raw[:4000], source="pdf",
                                    title=_cap(split["title"], 512),
                                    title_en=_cap(split["title_en"], 512),
                                    authors=_cap(split["authors"], 512)))
    return len(pairs)


def offline(paper_id: str, *, overwrite: bool = False) -> dict:
    """离线补元数据：PDF 首页 + DOI/开源地址 + 推荐阅读时间 + 参考文献。

    三段各自 try：**PDF 读不出来（扫描件）不该连参考文献一起丢掉** —— 后者读的是
    页产物，与 PDF 文本层无关。
    """
    fills: dict = {}
    sources: dict = {}
    errors: list[str] = []

    try:
        import pymupdf as fitz

        from backend.parsing.metadata import extract_paper_meta

        path = paper_service.resolve_pdf(paper_id)
        with fitz.open(str(path)) as doc:
            meta = extract_paper_meta(doc, path)
        fills.update({k: v for k, v in meta.fills().items() if k in FILL_FIELDS})
        sources.update({k: f"pdf:{v}" for k, v in (meta.sources or {}).items()})
        paper = paper_service.get_paper(paper_id)
        title = (meta.title or "").strip()
        if title and (overwrite or _title_needs_fix(paper.get("title") or "", paper_id)):
            paper_service.update_paper(paper_id, {"title": title[:512]}, source="pdf")
    except Exception as exc:  # noqa: BLE001
        log.warning("读 PDF 首页失败 %s：%s", paper_id, exc)
        errors.append(f"pdf: {exc}")

    rows = scan_rows(paper_id)
    fills.update(offline_extras(rows))
    if (minutes := estimate_read_time(rows)):
        fills["read_time_min"] = minutes

    applied = apply_fills(paper_id, fills, overwrite=overwrite, source="pdf")
    refs = reference_pairs(paper_id, rows)
    ref_count = save_references(paper_id, refs) if refs else 0
    return {"fills": fills, "applied": applied, "references": ref_count,
            "errors": errors, "sources": sources}


#: 兼容旧名字（老文档与外部脚本里用的是 `from_pdf`）
from_pdf = offline


# --------------------------------------------------------------------------
# 来源 ②：模型（与术语同一次调用）
# --------------------------------------------------------------------------


def normalize_meta(data: dict) -> dict:
    """模型返回的 meta 块 → 能直接写回论文行的字段（脏值一律丢掉）。"""
    data = data or {}
    out: dict = {}
    venue = str(data.get("venue") or "").strip()
    if len(venue) >= 2:
        out["venue"] = venue[:128]
    try:
        year = int(data.get("year") or 0)
    except (TypeError, ValueError):
        year = 0
    if 1950 <= year <= datetime.now().year + 1:
        out["year"] = year
    if (tags := _clean_list(data.get("tags"), limit=8, item_max=32)):
        out["tags"] = tags
    if (kw := _clean_list(data.get("keywords_en"), limit=8, item_max=48)):
        out["keywords_en"] = kw
    abstract = str(data.get("abstract_zh") or "").strip()
    if len(abstract) >= 60:               # 太短的"摘要"多半是模型随手写的一句话，不如不填
        out["abstract_zh"] = abstract[:4000]
    if (domain := str(data.get("domain") or "").strip()):
        out["domain"] = domain[:60]
    if (liner := " ".join(str(data.get("one_liner") or "").split())):
        out["one_liner"] = liner[:200]
    if (contrib := _clean_list(data.get("contributions"), limit=6, item_max=120)):
        out["contributions"] = contrib
    if (prereq := _clean_list(data.get("prereq"), limit=6, item_max=60)):
        out["prereq"] = prereq
    if (difficulty := str(data.get("difficulty") or "").strip()) in DIFFICULTIES:
        out["difficulty"] = difficulty
    if (ptype := str(data.get("paper_type") or "").strip()) in PAPER_TYPES:
        out["paper_type"] = ptype
    try:
        minutes = int(data.get("read_time_min") or 0)
    except (TypeError, ValueError):
        minutes = 0
    if 1 <= minutes <= 600:
        out["read_time_min"] = minutes
    if (doi := str(data.get("doi") or "").strip()):
        out["doi"] = re.sub(r"^https?://(?:dx\.)?doi\.org/", "", doi, flags=re.I)[:128]
    if (code := str(data.get("code_url") or "").strip()).startswith("http"):
        out["code_url"] = code[:256]
    return out


def model_meta(paper_id: str, *, provider=None) -> dict:
    """让模型读一遍抽样内容，产出元数据（失败返回空 dict）。"""
    from backend.services.translation.brief import sample_paper_text

    paper = paper_service.get_paper(paper_id)
    sample = sample_paper_text(paper_id)
    prompt = (f"论文标题：{paper.get('title') or paper_id}\n\n"
              f"以下是论文的抽样内容（标题 + 摘要 + 各章节开头）：\n\n{sample}\n")
    try:
        if provider is None:
            from backend.services.model.model_manager import model_manager
            provider = model_manager.get_generation()
        data = provider.structured(prompt, META_SCHEMA, system=META_SYSTEM) or {}
    except Exception as exc:  # noqa: BLE001 —— 模型失败不该让"补全"整个失败
        log.warning("模型元数据补全失败：%s", exc)
        return {}
    return normalize_meta(data)


def from_model(paper_id: str, *, provider=None, overwrite: bool = False) -> dict:
    fills = model_meta(paper_id, provider=provider)
    applied = apply_fills(paper_id, fills, overwrite=overwrite, source="model")
    return {"fills": fills, "applied": applied, "sources": {k: "model" for k in applied}}


# --------------------------------------------------------------------------
# 来源 ③：agent（读全文 + 工具；结果由 agent 通过内部接口落库）
# --------------------------------------------------------------------------


def agent_fill(paper_id: str, *, timeout: float = 900.0) -> dict:
    """让 **agent 服务**跑一次元数据填写（`POST /v1/meta-fill`）。

    为什么要绕到 agent：元数据要"读懂整篇论文"才填得准，而这正是 agent 那套
    （工具 + 多轮 + 宿主接口）已有的能力。**过程不展示**（用户口径：完整 agent 功能，
    不用显示细节），所以这里同步等它跑完，只把"填了哪些字段"带回去。

    返回 ``{ok, applied, rounds, tools, error}``；agent 服务没起来或跑挂了返回
    ``ok=False``，调用方（`fill_all`）据此回落到单次模型调用。
    """
    import httpx

    from backend.agent.config import load as load_agent_cfg
    from backend.core.config import get_settings

    try:
        cfg = load_agent_cfg()
    except Exception as exc:  # noqa: BLE001 —— 没有令牌/配置就是"agent 不可用"
        return {"ok": False, "applied": {}, "error": f"agent 配置不可用：{exc}"[:200]}
    port = int(get_settings().agent_port)
    url = f"http://127.0.0.1:{port}/v1/meta-fill"
    try:
        resp = httpx.post(url, json={"paper_id": paper_id},
                          headers={"X-Internal-Token": cfg.token}, timeout=timeout)
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "applied": {}, "error": f"agent 服务不可达：{exc}"[:200]}
    if resp.status_code != 200:
        return {"ok": False, "applied": {},
                "error": f"agent 返回 {resp.status_code}：{resp.text[:160]}"}
    data = resp.json() or {}
    return {"ok": bool(data.get("ok")), "applied": data.get("applied") or {},
            "rounds": data.get("rounds"), "tools": data.get("tools") or [],
            "error": data.get("error") or ""}


# --------------------------------------------------------------------------
# 编排：API / UI / 任务用的"一键补全"
# --------------------------------------------------------------------------


def auto(paper_id: str, *, model: bool = False, overwrite: bool = False,
         provider=None) -> dict:
    """先离线读 PDF（+ 参考文献 + 阅读时间），仍缺的字段（且 ``model=True``）再问一次模型。"""
    report: dict = {"paper_id": paper_id, "applied": {}, "sources": {},
                    "errors": [], "references": 0}
    try:
        off = offline(paper_id, overwrite=overwrite)
        report["applied"].update(off["applied"])
        report["sources"].update(off["sources"])
        report["references"] = off.get("references") or 0
        report["errors"].extend(off.get("errors") or [])
    except Exception as exc:  # noqa: BLE001
        log.warning("离线补元数据失败 %s：%s", paper_id, exc)
        report["errors"].append(f"pdf: {exc}")

    paper = paper_service.get_paper(paper_id)
    if model and missing_fields(paper):
        try:
            got = from_model(paper_id, provider=provider, overwrite=False)
            report["applied"].update(got["applied"])
            report["sources"].update(got["sources"])
        except Exception as exc:  # noqa: BLE001
            log.warning("模型补元数据失败 %s：%s", paper_id, exc)
            report["errors"].append(f"model: {exc}")

    paper = paper_service.get_paper(paper_id)
    report["paper"] = paper
    report["missing"] = missing_fields(paper)
    return report


def fill_all(paper_id: str, *, overwrite: bool = False,
             use_agent: bool = True) -> dict:
    """一次"补全到底"：离线 → **agent**（不可用就回落模型）→ 参考文献。

    这是任务处理器（`META_FILL`）与前端「模型补全」按钮共用的入口。
    """
    report = auto(paper_id, model=False, overwrite=overwrite)
    report["method"] = "offline"
    if not missing_fields(paper_service.get_paper(paper_id)):
        return report

    if use_agent:
        got = agent_fill(paper_id)
        report["agent"] = {"ok": got.get("ok"), "applied": got.get("applied") or {},
                           "rounds": got.get("rounds"), "tools": got.get("tools") or [],
                           "error": got.get("error") or ""}
        report["applied"].update(got.get("applied") or {})
        if got.get("ok"):
            report["method"] = "agent"
            paper = paper_service.get_paper(paper_id)
            report["paper"] = paper
            report["missing"] = missing_fields(paper)
            if not report["missing"]:
                return report
            # agent 提交了但还缺字段（它读了但判断不出来）：再用一次单次调用试试，
            # 这次几乎不花钱，而"按钮点了却什么都没填上"是更坏的体验。
        else:
            log.warning("agent 补元数据没成功（%s），回落到单次模型调用", paper_id)

    try:
        got = from_model(paper_id, overwrite=overwrite)
        report["applied"].update(got["applied"])
        report["method"] = "model"
    except Exception as exc:  # noqa: BLE001
        log.warning("模型补元数据失败 %s：%s", paper_id, exc)
        report["errors"].append(f"model: {exc}")

    paper = paper_service.get_paper(paper_id)
    report["paper"] = paper
    report["missing"] = missing_fields(paper)
    return report
