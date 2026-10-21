"""第二阶段「论文理解」：两路各做一次全篇理解，结果是**自由 Markdown + 引用校验**。

## 在整条流水线里的位置

``PDF → 解析 → 两路并发（A 转录 / B 翻译）→ 两路各自分块 → 两路各自理解 → 前端统一展示``

理解是**第二阶段**：它读的是**已经落库的 chunk**（不是页图、也不是原始解析文本），
所以"理解结果引用了哪一段"与知识库检索用的是**同一套 chunk id** —— 这正是用户要的
"Chunk 是论文理解的引用基准单位"。

**做什么**（用户 2026-10-02 口径）：原 B 路「阅读地图 + 相关概念」**整体删除**
（前端入口、页签、整页、后端提示词与任务一起清掉），只保留
**A 路（英文转录侧）= 论文精读**（基本信息 / 背景与问题 / 动机与 gap / **贡献与创新点** /
方法 / 实验 / 结论、局限与启发）。阅读地图那条模型能力转去做**论文元数据生成与填写**
（`services/paper/metadata_fill.py`）。A 路原先的"技术解剖"早先已被判定与阅读地图重复、
换成现在这套精读框架（见 `prompts.py` 顶部说明）。

## 为什么清单里给"短码"

让模型直接引 `turboquant-online-vector-iclr-26:b:unit:p006-04-p006-06:zh` 这种 id 不现实
（太长、容易抄错，抄错一个字符就没法校验）。所以每路把材料按论文顺序编号成
`A001…` / `B001…`，模型只引短码，落库时把短码解析回真正的 chunk id。
用户举的例子就是 `来源 Chunk: B023`，这里与之一致。

**短码的稳定性**：短码 = 该路材料在**论文顺序**里的序号。重新分块会让序号变化，
所以短码映射**跟结果一起落库**（`refs_json` 里既存短码也存解析后的 chunk id），
理解结果的自我一致性不依赖"下次重建还得到同样的编号"。

## 校验（用户口径 ⑦ 与 ⑧）

- 结果**不按 schema 校验**：Markdown 部分原样保留，模型增删合并小节都合法。
- 只校验**引用**：短码必须在本路清单里；引用到别路的短码、或不存在的短码都记进
  `invalid_refs`，并在结果上打 `refs_ok=False`。**不丢结果**（理解内容本身仍然有用），
  但前端会明确标出"引用不可信"。
"""

from __future__ import annotations

import re
import time

from backend.core.logging import get_logger
from backend.core.utils import json_dumps, json_loads, now_iso
from backend.repositories.mysql.models import Chunk, Paper, Understanding
from backend.repositories.mysql.session import session_scope
from backend.services.translation.brief import generate_brief
#: 输出预算与页级**同一个常量**（`page_translate.MAX_OUTPUT_TOKENS` = 模型自己的上限 384K）：
#: 两处各写一个字面量迟早会漂，所以直接从那里拿（`page_translate` 顶层不 import 本模块，不成环）。
from backend.services.translation.page_translate import MAX_OUTPUT_TOKENS
from backend.services.understanding import prompts

log = get_logger(__name__)

#: 哪一路看哪些路的 chunk：``a`` 看 A 路 + 共用
ROAD_OF_PATH = {"a": "a"}
#: 本路材料清单里的语言：A 路是英文行 + 语言无关行（公式）
LANGS_OF_PATH = {"a": ("en", "any")}
#: 清单里要哪些类型：正文 text 不进清单（单元更细、且 unit/formula/figure/table 已经是检索单位）
TYPES = ("unit", "formula", "figure", "table")
PATH_TASK_TYPES = {"a": "UNDERSTAND_A"}
#: 任务面板/日志里用的中文名。写清"这一路负责什么"而不是"A 路"
#: （前端只呈现一份解读，不暴露"路"这个概念）。
PATH_LABELS = {"a": "论文精读（结构与创新点）"}
MAX_ITEMS = 420
_REF_RE = re.compile(r"(?:来源\s*Chunk\s*[:：]\s*)?\b(A\d{2,4})\b")


def normalize_path(value) -> str:
    """路径归一：只接受 ``a``（``b`` 已随「阅读地图」一起删除）。"""
    if value is None:
        return "a"
    text = str(value).strip().lower()
    if text not in ROAD_OF_PATH:
        raise ValueError(f"未知的路径：{value!r}（只支持 'a'）")
    return text


# --------------------------------------------------------------------------
# 材料清单（chunk → 短码）
# --------------------------------------------------------------------------


def chunk_inventory(paper_id: str, path: str) -> list[dict]:
    """本路要看的材料清单（按论文顺序），每项带短码。

    只看**本路 + 共用**的 chunk，且只看本路语言的行 —— A 路读英文材料、B 路读中文材料，
    与"两路各自分块"是同一条口径（B 路的中文块本来就是按中文语义切的）。
    """
    road = ROAD_OF_PATH[path]
    langs = LANGS_OF_PATH[path]
    with session_scope() as sess:
        rows = (sess.query(Chunk)
                .filter(Chunk.paper_id == paper_id, Chunk.chunk_type.in_(TYPES))
                .order_by(Chunk.order_global.asc())
                .limit(MAX_ITEMS * 3).all())
        items: list[dict] = []
        for row in rows:
            meta = json_loads(row.metadata_json, {}) or {}
            if (meta.get("road") or "shared") not in (road, "shared"):
                continue
            lang = meta.get("lang") or "en"
            if lang not in langs:
                continue
            items.append({"chunk_id": row.id, "chunk_type": row.chunk_type,
                          "page": int(row.page_no or 0),
                          "section": row.section or "",
                          "preview": row.content_preview or ""})
    # 短码按论文顺序编号（A 路 A001… / B 路 B001…）
    prefix = path.upper()
    for i, item in enumerate(items, 1):
        item["code"] = f"{prefix}{i:03d}"
    if len(items) > MAX_ITEMS:
        log.warning("%s 路材料清单 %d 条，截到 %d 条（理解只看主干）",
                    path, len(items), MAX_ITEMS)
        items = items[:MAX_ITEMS]
    _attach_full_content(items)
    return items


#: 这几类 chunk 的**数值/公式证据**必须给全文：预览 200 字会把表格切成"表题 + 表头"，
#: 于是模型会（正确地）抱怨"材料中表格数值被截断，无法核验提升是否显著"——用户 2026-09-27
#: 报的就是这个：翻译产物里表格是完整的（模型看的是整页图像），而解读阶段看到的是被截断的预览。
#: 这两类总量很小（一篇通常几张表 + 若干公式），给全文对 prompt 体积影响可控。
FULL_CONTENT_TYPES = ("table", "formula")


def _attach_full_content(items: list[dict]) -> None:
    """给表格/公式条目补上**全文**（`content_preview` 只存了 200 字）。

    全文在向量库里（chunk 正文就存在那儿），按 id 取，取不到就退回预览（不影响理解任务本身）。
    """
    wanted = [i["chunk_id"] for i in items if i.get("chunk_type") in FULL_CONTENT_TYPES]
    if not wanted:
        return
    try:
        from backend.repositories.vector.lancedb_store import vector_store

        by_id = {r["chunk_id"]: r.get("content") or "" for r in vector_store.get_rows(wanted)}
    except Exception as exc:  # noqa: BLE001 —— 取不到全文只是材料差一点，不该让理解任务失败
        log.warning("取表格/公式全文失败（理解将只用预览）：%s", exc)
        return
    filled = 0
    for item in items:
        text = by_id.get(item["chunk_id"])
        if text:
            item["content"] = text
            filled += 1
    log.info("材料清单补全文：%d/%d 条（表格/公式）", filled, len(wanted))


def code_map(inventory: list[dict]) -> dict[str, str]:
    """短码 → 真正的 chunk id。"""
    return {item["code"]: item["chunk_id"] for item in inventory}


# --------------------------------------------------------------------------
# 解析与校验
# --------------------------------------------------------------------------


def parse_result(text: str) -> dict:
    """把模型回复拆成 ``{"markdown", "refs"}``。

    **宽容**：模型没给 JSON 收尾时，整段回复就当 Markdown，`refs` 从正文里的
    `来源 Chunk: XXXX` 兜底提取 —— 理解内容不该因为格式差一点就丢掉（用户口径 ⑦）。
    """
    raw = (text or "").strip()
    markdown, refs = raw, []
    got_json = False
    fence = re.search(r"```(?:json)?\s*(\{.*?\})\s*```", raw, re.DOTALL)
    candidates = []
    if fence:
        candidates.append(fence.group(1))
    start, end = raw.rfind("{"), raw.rfind("}")
    if 0 <= start < end:
        candidates.append(raw[start:end + 1])
    for cand in candidates:
        data = json_loads(cand, None)
        if isinstance(data, dict) and ("refs" in data or "markdown" in data):
            got_json = True
            md = data.get("markdown")
            if isinstance(md, str) and md.strip():
                markdown = md.strip()
            else:
                markdown = _strip_json_tail(raw)
            got = data.get("refs")
            if isinstance(got, list):
                refs = [str(r).strip() for r in got if str(r).strip()]
            break
    if not refs:
        refs = _scan_refs(raw)
    return {"markdown": markdown, "refs": refs, "had_json": got_json}


def _strip_json_tail(raw: str) -> str:
    """去掉收尾的 ``json`` 代码块，只留自由 Markdown。

    ⚠️ 必须找**开**围栏（``rfind("```json")``），不能 `rfind("```")` —— 后者拿到的是
    **闭**围栏，切出来的 Markdown 会把整个 JSON 块带上（实测就是这样漏进来的）。
    """
    for needle in ("```json", "```JSON", "```"):
        cut = raw.rfind(needle)
        if cut > 0:
            return raw[:cut].strip()
    return raw.strip()


def _scan_refs(text: str) -> list[str]:
    """从正文里扫 `来源 Chunk: A023` / 裸 `A023`，按出现顺序去重。"""
    out: list[str] = []
    for m in _REF_RE.finditer(text or ""):
        code = m.group(1).upper()
        if code not in out:
            out.append(code)
    return out


def validate_refs(refs: list[str], valid: dict[str, str], *, path: str = "a") -> dict:
    """校验引用：短码必须在本路清单里。

    返回 ``{"resolved": {code: chunk_id}, "invalid": [...], "cross_road": [...],
    "ok": bool}``。**不抛错**：调用方把结果落库并打标记，理解内容不丢。

    ``cross_road`` 在只有 A 路之后仍保留：老库里存着 B 路生成的解读，模型偶尔会写
    `来源 Chunk: B023`（那是它上一轮的格式），这类短码既不在清单里、也算不上"无效"，
    单独归一类，读的时候才说得清是哪种错。
    """
    resolved: dict[str, str] = {}
    invalid: list[str] = []
    cross: list[str] = []
    for code in refs:
        if code in valid:
            resolved[code] = valid[code]
            continue
        if code.startswith("B"):
            cross.append(code)
        else:
            invalid.append(code)
    return {"resolved": resolved, "invalid": invalid, "cross_road": cross,
            "ok": not invalid and not cross}


# --------------------------------------------------------------------------
# 生成
# --------------------------------------------------------------------------


def _provider(path: str):
    from backend.services.translation.translation_service import (
        _current_model_id, _path_provider,
    )

    return _path_provider(path), _current_model_id(path)


#: 理解的输出预算：**与页级共用同一个常量**（模型自己的上限 384K，用户口径
#: "不应该有 token 生成限制"）。以前这里写 12000，于是"思考占满预算 → 正文为空"会白烧一轮。
MAX_TOKENS = MAX_OUTPUT_TOKENS

#: 一次理解调用最多试几次（**同预算**重试）。为什么必须有：理解是"一次调用 = 整个任务"，
#: 任务级失败不会被重新排队（`task_service._execute` → `finish(FAILED)`），
#: 而思考模式偶发"只想不写"（用户 2026-10-08 报的那条 turboquant 精读失败就是它）。
_ATTEMPTS = 3
#: 同预算重试之间的等待（秒，按次数递增）：偶发问题隔一两秒再来一次通常就正常了。
_ATTEMPT_SLEEP = 1.5


def _page_context_head(path: str, paper_id: str) -> str:
    """**页级调用的「稳定前缀 + 完整前文」原样搬过来**（逐字节相同）。

    为什么这么做（用户 2026-09-27 口径："解读模型和论文阅读模型要分别完全命中这两路模型的缓存，
    确保它们有完整的记忆"）：

    页级转录/翻译是串行的，第 N 页的 user prompt 结构固定为
    ``[稳定前缀（简报+术语表+标题骨架）] + [第 1..N-1 页全文] + [本页]``，
    而且**追加式**渲染、表头不带页码 —— 这正是"前缀缓存"能一路命中的原因
    （`page_translate.history_context` 的 docstring 写得很清楚，实测稳定态命中 80%+）。

    解读阶段以前是**另起一套 prompt**（另一套 system + chunk 清单），与页级调用零公共前缀
    → 每次全额计费，而且模型只看到每条 chunk 的 200 字预览（"表格数值被截断"就是这么来的）。
    现在改成：解读的 user prompt **以页级那段前缀开头**（`稳定前缀 + 第 1..T 页全文`，
    `history_context(..., page_no=T+1)` 与页级第 T 页对第 1..T-1 页的渲染**逐字节相同**），
    于是最长公共前缀 = system + 稳定前缀 + 前 1..T-1 页全文 → 那一大段全部吃缓存，
    同时模型手里就是**整篇全文**（`CONTEXT_MAX_CHARS` = 200 万字符，等于不截断）。
    """
    try:
        from backend.services.paper import paper_service
        from backend.services.translation.page_translate import (
            CONTEXT_MAX_CHARS, PageStore, build_outline_skeleton,
            build_stable_prefix, history_context, list_pages,
        )

        paths = paper_service.paper_paths(paper_id)
        store = PageStore(paper_id, paths["path_a"])
        total = len(list_pages(paper_id))
        skeleton = build_outline_skeleton(paper_id, store)
        # 术语表是翻译侧（B 路）的东西，A 路不注入 —— 页级转录的稳定前缀里也没有它，
        # 注入了反而对不上前缀、白丢缓存。
        glossary = ""
        prefix = build_stable_prefix(generate_brief(paper_id), glossary, skeleton=skeleton)
        history = history_context(store, paper_id, total + 1, max_chars=CONTEXT_MAX_CHARS)
        return f"{prefix}{chr(10) + chr(10) + history if history else ''}"
    except Exception as exc:  # noqa: BLE001 —— 对齐失败只是不享缓存，不能让理解失败
        log.warning("%s 路页级上下文对齐失败（本次不享前缀缓存）：%s", path, exc)
        return ""


def _generate(provider, prompt: str, *, system: str, path: str,
              min_tokens: int = MAX_TOKENS):
    """调模型产出理解（**同预算重试**，最多 `_ATTEMPTS` 次）。

    预算基线就是厂商上限（`MAX_OUTPUT_TOKENS` = 384K，用户口径"不应该有 token 生成限制"），
    所以那些"正文为空"的错误**没有更高预算可升**（`_escalate_max_tokens` 恒返回 None）——
    但也不能一次就放弃：任务级失败**不会被重新排队**
    （`task_service._execute` 直接 `finish(FAILED)`，只有页级翻译那条路有逐项重试的调度器）。
    实测 turboquant 的 A 路精读就这么永久失败过两次：模型只吐思维链、`finish=stop`
    （reasoning 5445 / 6954 tokens），而同一份任务在相邻的两次运行里都成功 —— 纯偶发。

    所以：可重试的错误（`ProviderError.retryable`，含"只出思维链"、429、5xx、超时）
    **同预算再要一次**；不可重试的（密钥错、400）立刻抛。
    """
    from backend.services.model.usage_stats import usage_scope
    from backend.services.translation.translation_service import _escalate_max_tokens

    budget = max(MAX_TOKENS, int(min_tokens))
    for attempt in range(1, _ATTEMPTS + 1):
        try:
            # 用量口径在这里标一次（理解两路共用这一个调用点），重试的每一次都各算各的
            with usage_scope("understand"):
                return provider.generate(prompt, system=system, temperature=0.3,
                                         max_tokens=budget)
        except Exception as exc:  # noqa: BLE001
            escalated = _escalate_max_tokens(exc, budget)
            if escalated is not None:                      # 将来基线还能抬时走这条
                log.warning("%s 路理解正文为空（思考占满预算），max_tokens %d → %d 重试",
                            path, budget, escalated)
                budget = escalated
                continue
            if not getattr(exc, "retryable", False) or attempt >= _ATTEMPTS:
                log.error("%s 路理解失败（第 %d/%d 次，不再重试）：%s",
                          path, attempt, _ATTEMPTS, str(exc)[:160])
                raise
            log.warning("%s 路理解第 %d/%d 次失败（%s），同预算再试一次",
                        path, attempt, _ATTEMPTS, str(exc)[:120])
            time.sleep(_ATTEMPT_SLEEP * attempt)
    raise RuntimeError(f"{path} 路理解重试 {_ATTEMPTS} 次仍失败（max_tokens={budget}）")


def build_understanding(paper_id: str, path: str, *, force: bool = False) -> dict:
    """跑一次全篇理解（两路各一次调用），落库并返回结果。

    幂等：已存在且 `force=False` 时直接返回库里那份（与翻译的 resume 口径一致）——
    理解是一次性全篇调用，没有"续跑页"的概念。
    """
    path = normalize_path(path)
    with session_scope() as sess:
        row = sess.get(Understanding, f"{paper_id}:{path}")
        if row is not None and not force:
            return to_dict(row)
        paper = sess.get(Paper, paper_id)
        title = (paper.title if paper is not None else "") or paper_id

    inventory = chunk_inventory(paper_id, path)
    if not inventory:
        # 这句会原样显示给用户（InsightDoc 的 error 分支），所以不写 chunk 这个字
        raise ValueError(f"{paper_id} 还没有可用的段落索引（先建索引）")
    brief = _brief(paper_id)
    # system 用**页级那一套**（逐字节相同）→ 解读调用能吃该路的前缀缓存；
    # 解读的任务说明与材料跟在「稳定前缀 + 完整前文」之后（都在 user 里）。
    from backend.services.translation.page_translate import path_system_prompt

    head = _page_context_head(path, paper_id)
    prompt = prompts.render_understanding_prompt(
        path=path, title=title, inventory=inventory, brief=brief,
        head=head, task_block=prompts.system_for(path))
    provider, model_id = _provider(path)
    system = path_system_prompt(path)
    res = _generate(provider, prompt, system=system, path=path)
    # 缓存命中要能看见（用户口径："确保它们有完整的记忆"——命中率就是这条链路的体检指标）。
    # ⚠️ 缓存数字在 `raw["usage"]` 里：`provider.generate` 返回的是裸 `GenerationResult`，
    # 没有 page_translate.PageCallResult 那两个字段（第一版日志因此打出误导性的 0/0）。
    usage_raw = (getattr(res, "raw", None) or {}).get("usage") or {}
    log.info("%s 路理解：prompt %d token（缓存命中 %d / 未命中 %d；页级上下文 %d 字）",
             path, int(getattr(res, "prompt_tokens", 0) or 0),
             int(usage_raw.get("prompt_cache_hit_tokens") or 0),
             int(usage_raw.get("prompt_cache_miss_tokens") or 0), len(head))
    parsed = parse_result(res.text or "")
    truncated = not parsed["had_json"]
    if truncated:
        # 没有收尾 JSON：可能是被输出长度截断（实测第一次只写了 4 节就停），
        # 也可能只是模型收尾格式飘了。重试**一次**（有界）：预算已经是厂商上限，
        # 所以这次重试是"同预算再要一次"，不再放大了。
        log.warning("%s 路理解没给出收尾 JSON（疑似被截断），同预算重试一次", path)
        res = _generate(provider, prompt, system=system, path=path,
                        min_tokens=MAX_TOKENS)
        parsed = parse_result(res.text or "")
    valid = code_map(inventory)
    check = validate_refs(parsed["refs"], valid, path=path)
    payload = {
        "paper_id": paper_id, "path": path,
        "markdown": parsed["markdown"],
        "refs_json": json_dumps({"codes": parsed["refs"], **check}),
        "model": getattr(res, "model", "") or provider.model_name or model_id or "",
        "prompt_tokens": int(getattr(res, "prompt_tokens", 0) or 0),
        "completion_tokens": int(getattr(res, "completion_tokens", 0) or 0),
        "inventory": json_dumps([
            {"code": it["code"], "chunk_id": it["chunk_id"],
             "chunk_type": it["chunk_type"], "page": it["page"]}
            for it in inventory]),
        "refs_ok": check["ok"],
        "updated_at": now_iso(),
    }
    _save(paper_id, path, payload)
    log.info("理解完成（%s，%s 路）：Markdown %d 字，引用 %d 条（有效 %d / 无效 %d / 跨路 %d）",
             paper_id, path, len(payload["markdown"]), len(parsed["refs"]),
             len(check["resolved"]), len(check["invalid"]), len(check["cross_road"]))
    return get_understanding(paper_id, path)


def _brief(paper_id: str) -> str:
    """文档简报（领域 + 术语表）：复用翻译阶段的简报，理解阶段同样受益。

    取不到就返回空串 —— 简报只是锦上添花，不该让理解失败。
    """
    try:
        brief = generate_brief(paper_id)
        if brief is None or not getattr(brief, "enabled", False):
            return ""
        terms = "、".join(str(t.get("zh") or t.get("term") or "")
                        for t in (brief.glossary or [])[:20])
        return f"{brief.domain}｜术语：{terms}"
    except Exception as exc:  # noqa: BLE001
        log.debug("取文档简报失败（忽略）：%s", exc)
        return ""


def _save(paper_id: str, path: str, payload: dict) -> None:
    with session_scope() as sess:
        row = sess.get(Understanding, f"{paper_id}:{path}")
        created = row.created_at if row is not None else payload["updated_at"]
        if row is None:
            row = Understanding(id=f"{paper_id}:{path}", created_at=created)
            sess.add(row)
        for key in ("paper_id", "path", "markdown", "refs_json", "model",
                    "prompt_tokens", "completion_tokens", "inventory", "refs_ok",
                    "updated_at"):
            setattr(row, key, payload[key])


def _chunk_anchors(ids: list[str]) -> dict[str, dict]:
    """这些 chunk id → 它落在原文的哪个位置（已不存在的 id 不会出现在返回值里）。

    两个锚点都带出来，因为它们回答的是两个不同问题：

    - block_id：解析块 / 正文行的原始 id。文本块的 block_id 往往就是正文行 id
      （p006-03），但公式块有 row-<行 id> 这种写法，还有 …-x01 这种**伪块 id**
      （版面模型给的公式块，正文里根本没有这一行）。
    - row_id：chunk 元数据里记下的**真正落在哪一正文行**（page_row / formula_block
      两种锚定方式都写它）。它才是"跳到那一段"的可靠答案 —— 上面那两种 block_id
      阅读器都翻不成行，用户点标号就只能整页闪（2026-12-03 报的「标号 23 / 22
      高亮整页」：恰好都是公式 chunk）。

    值里的键可能缺：那是"还在、但落不到具体段落"，与"不在了"（key 缺失）必须分开 ——
    前者照样算有效引用，只是只能跳到页。
    """
    if not ids:
        return {}
    from backend.repositories.mysql.models import Chunk

    with session_scope() as sess:
        rows = (sess.query(Chunk.id, Chunk.block_id, Chunk.metadata_json)
                .filter(Chunk.id.in_(ids)).all())
    out: dict[str, dict] = {}
    for cid, block_id, meta_json in rows:
        meta = json_loads(meta_json, {}) or {}
        row_id = meta.get("row_id") or (meta.get("row_ids") or [None])[0]
        out[cid] = {"block_id": block_id, "row_id": row_id,
                    #: 公式条目带上 LaTeX：前端据此在**同一行里**认出是哪一枚公式（见下）
                    "latex": meta.get("latex")}
    return out


def _live_chunk_ids(ids: list[str]) -> set[str]:
    """这些 chunk id 现在还存在于库里吗（重新翻译 + 重建索引后 id 会变）。"""
    if not ids:
        return set()
    from backend.repositories.mysql.models import Chunk

    with session_scope() as sess:
        rows = sess.query(Chunk.id).filter(Chunk.id.in_(ids)).all()
    return {r[0] for r in rows}


def split_live_refs(resolved: dict[str, str], by_code: dict, *,
                    exists=_live_chunk_ids,
                    anchors: dict[str, dict] | None = None
                    ) -> tuple[list[dict], list[dict]]:
    """引用分两类：**还能跳到原文**的 与 **已过期**的（chunk 已不存在）。

    为什么保留过期的、而不是丢掉：解读内容本身仍然有效（概念解释、阅读顺序、方法链条），
    只是"指向的那一段"在重新翻译/重建索引后换了 id。丢掉它等于**重翻译一次就把解读打回原形** ——
    用户明确要求"重新翻译不影响已有的解读"。过期引用仍**保留页码**（页码来自生成时的 inventory），
    前端据此照样能跳到那一页，只是标一句"引用已过期（论文已重新翻译）"。
    """
    live: list[dict] = []
    stale: list[dict] = []
    ids = [cid for cid in resolved.values() if cid]
    alive = exists(ids) if exists is not None else set()
    for code, chunk_id in resolved.items():
        meta = by_code.get(code) or {}
        item = {"code": code, "chunk_id": chunk_id,
                "chunk_type": meta.get("chunk_type"), "page": meta.get("page")}
        # 落点：前端点标号要落到"原文那一段"，只给页码只能跳到整页（见 _chunk_anchors）。
        anchor = (anchors or {}).get(chunk_id) or {}
        if anchor.get("block_id"):
            item["block_id"] = anchor["block_id"]
        if anchor.get("row_id"):
            item["row_id"] = anchor["row_id"]
        # 公式的 LaTeX：一条公式与它所在的**同一个正文行**本来是两条引用（用户 2026-12-03 的
        # 「56、57 为什么是同一块地方」）—— 行级落点必然重合，只有精确到公式元素才分得开，
        # 所以正文行 id 之外再带上这条公式本身。
        if anchor.get("latex"):
            item["latex"] = anchor["latex"]
        (live if chunk_id in alive else stale).append(item)
    return live, stale


def to_dict(row: Understanding) -> dict:
    refs = json_loads(row.refs_json, {}) or {}
    inventory = json_loads(row.inventory, []) or []
    by_code = {it["code"]: it for it in inventory if isinstance(it, dict)}
    resolved = refs.get("resolved") or {}
    # 一次 in(...) 查询同时给出"还在不在"与"落在哪个块"：分两次查就是两次 RTT。
    anchors = _chunk_anchors([cid for cid in resolved.values() if cid])
    live_refs, stale_refs = split_live_refs(
        resolved, by_code, exists=lambda ids: set(anchors) & set(ids), anchors=anchors)
    return {
        "paper_id": row.paper_id, "path": row.path,
        "label": PATH_LABELS.get(row.path, row.path),
        "markdown": row.markdown or "",
        "refs": refs.get("codes") or [],
        "refs_resolved": live_refs,
        # 重新翻译 + 重建索引后旧 chunk id 会失效：**保留**这些引用（页码还在，照样能跳），
        # 只是另列一份让前端标"引用已过期"，绝不因为重翻译就把解读清空（用户口径）。
        "stale_refs": stale_refs,
        "invalid_refs": refs.get("invalid") or [],
        "cross_road_refs": refs.get("cross_road") or [],
        "refs_ok": bool(row.refs_ok),
        "model": row.model, "updated_at": row.updated_at,
        "prompt_tokens": int(row.prompt_tokens or 0),
        "completion_tokens": int(row.completion_tokens or 0),
        "inventory_size": len(inventory),
        "inventory": inventory,
    }


def get_understanding(paper_id: str, path: str | None = None) -> dict:
    """取解读结果；``path=None`` 给打包形状（`a` + `done`），前端一次取全。"""
    with session_scope() as sess:
        if path is not None:
            row = sess.get(Understanding, f"{paper_id}:{normalize_path(path)}")
            return to_dict(row) if row is not None else {}
        rows = (sess.query(Understanding)
                .filter(Understanding.paper_id == paper_id).all())
    by_path = {r.path: to_dict(r) for r in rows}
    return {"paper_id": paper_id, "a": by_path.get("a") or {},
            # 老库里可能还留着 `b` 行（阅读地图已删）：只报还在用的这一路，
            # 免得前端拿 `done` 去判断"两路都到齐了"。
            "done": [p for p in sorted(by_path) if p in PATH_LABELS]}


def understanding_status(paper_id: str) -> dict:
    """解读的状态（前端按钮与进度用）。"""
    with session_scope() as sess:
        rows = (sess.query(Understanding)
                .filter(Understanding.paper_id == paper_id).all())
    have = {r.path: r for r in rows}
    return {
        "paper_id": paper_id,
        "a": {"done": "a" in have, "updated_at": have["a"].updated_at if "a" in have else None},
        "chunk_ready": bool(chunk_inventory(paper_id, "a")),
    }


# --------------------------------------------------------------------------
# 任务入口（与页级翻译同构：一个任务，幂等/取消/面板都分得清）
# --------------------------------------------------------------------------


def run_understand_task(ctx, path: str) -> dict:
    paper_id = ctx.paper_id
    if not paper_id:
        raise ValueError("UNDERSTAND 任务缺少 paper_id")
    path = normalize_path(path)
    ctx.progress(0, 1, force=True,
                 message=f"待理解（{PATH_LABELS[path]}）")
    result = build_understanding(paper_id, path, force=True)
    ok_refs = result.get("refs_ok")
    ctx.progress(1, 1, force=True,
                 message=(f"{PATH_LABELS[path]}完成：{len(result['markdown'])} 字，"
                          f"引用 {len(result['refs'])} 条"
                          f"{'' if ok_refs else '（**有无效引用**）'}"))
    return {"message": f"{PATH_LABELS[path]}完成",
            "_done_items": 1, "_total_items": 1,
            "refs_ok": ok_refs, "refs": len(result.get("refs") or []),
            "invalid_refs": result.get("invalid_refs") or []}


def start_understanding(paper_id: str, *, paths: list[str] | None = None,
                        force: bool = False) -> dict:
    """为解读建任务（与 `start_translation` 同一套路：幂等 get_or_create）。"""
    from backend.services.paper import paper_service
    from backend.services.task.task_service import task_service

    paper_service.get_paper(paper_id)
    want = [normalize_path(p) for p in (paths or ["a"])]
    inventory = {p: len(chunk_inventory(paper_id, p)) for p in want}
    out = []
    for path in want:
        if inventory[path] == 0:
            log.warning("%s 路没有可理解的 chunk，跳过", path)
            continue
        task, created = task_service.get_or_create(
            PATH_TASK_TYPES[path], "knowledge", paper_id,
            {"path": path, "force": bool(force)},
            total_items=1)
        out.append({"path": path, "task_id": task["id"], "created": created,
                    "reused": not created, "inventory": inventory[path]})
    return {"paper_id": paper_id, "tasks": out,
            "task_ids": [t["task_id"] for t in out]}
