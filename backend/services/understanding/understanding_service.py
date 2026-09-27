"""第二阶段「论文理解」：两路各做一次全篇理解，结果是**自由 Markdown + 引用校验**。

## 在整条流水线里的位置

``PDF → 解析 → 两路并发（A 转录 / B 翻译）→ 两路各自分块 → 两路各自理解 → 前端统一展示``

理解是**第二阶段**：它读的是**已经落库的 chunk**（不是页图、也不是原始解析文本），
所以"理解结果引用了哪一段"与知识库检索用的是**同一套 chunk id** —— 这正是用户要的
"Chunk 是论文理解的引用基准单位"。

**两路各做什么**（用户 2026-09-26 看过真实结果后调整过）：
A 路（英文转录侧）= **论文精读**（基本信息 / 背景与问题 / 动机与 gap / **贡献与创新点** /
方法 / 实验 / 结论、局限与启发）；B 路（中文翻译侧）= **阅读地图 + 相关概念**。
原先 A 路的"技术解剖"被判定与阅读地图重复、已替换（见 `prompts.py` 顶部说明）。
**前端把两路拼成一篇展示，不暴露"两路"这个概念。**

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
- B 路的"相关概念"必须有引用：`concepts` 的小节里若一个短码都没有，记
  `missing_concept_refs=True`（用户 ⑧ 的硬要求）。
"""

from __future__ import annotations

import re

from backend.core.logging import get_logger
from backend.core.utils import json_dumps, json_loads, now_iso
from backend.repositories.mysql.models import Chunk, Paper, Understanding
from backend.repositories.mysql.session import session_scope
from backend.services.translation.brief import generate_brief
from backend.services.understanding import prompts

log = get_logger(__name__)

#: 哪一路看哪些路的 chunk：``a`` 看 A 路 + 共用；``b`` 看 B 路 + 共用
ROAD_OF_PATH = {"a": "a", "b": "b"}
#: 本路材料清单里的语言：A 路是英文行 + 语言无关行（公式），B 路是中文行 + 语言无关行
LANGS_OF_PATH = {"a": ("en", "any"), "b": ("zh", "any")}
#: 清单里要哪些类型：正文 text 不进清单（单元更细、且 unit/formula/figure/table 已经是检索单位）
TYPES = ("unit", "formula", "figure", "table")
PATH_TASK_TYPES = {"a": "UNDERSTAND_A", "b": "UNDERSTAND_B"}
#: 任务面板/日志里用的中文名。**前端不再展示这两路标签**（用户口径：前端只呈现一份解读），
#: 它们只出现在任务列表与后端日志里，所以写清"这一路负责什么"而不是"A 路 / B 路"。
PATH_LABELS = {"a": "论文精读（结构与创新点）", "b": "阅读地图与相关概念"}
MAX_ITEMS = 420
_REF_RE = re.compile(r"(?:来源\s*Chunk\s*[:：]\s*)?\b([AB]\d{2,4})\b")


def normalize_path(value) -> str:
    """路径归一：只接受 ``a`` / ``b``（大小写与空格容错）。"""
    if value is None:
        return "a"
    text = str(value).strip().lower()
    if text not in ROAD_OF_PATH:
        raise ValueError(f"未知的路径：{value!r}（只支持 'a' / 'b'）")
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


def path_of_code(code: str) -> str:
    """从短码前缀看出是哪一路（`B023` → ``b``）。"""
    return "a" if str(code).strip().upper().startswith("A") else "b"


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
    """从正文里扫 `来源 Chunk: B023` / 裸 `B023`，按出现顺序去重。"""
    out: list[str] = []
    for m in _REF_RE.finditer(text or ""):
        code = m.group(1).upper()
        if code not in out:
            out.append(code)
    return out


def validate_refs(refs: list[str], valid: dict[str, str], *, path: str) -> dict:
    """校验引用：短码必须在本路清单里，且不许跨路。

    返回 ``{"resolved": {code: chunk_id}, "invalid": [...], "cross_road": [...],
    "ok": bool}``。**不抛错**：调用方把结果落库并打标记，理解内容不丢。
    """
    resolved: dict[str, str] = {}
    invalid: list[str] = []
    cross: list[str] = []
    for code in refs:
        if code in valid:
            resolved[code] = valid[code]
            continue
        if code.startswith("A" if path == "b" else "B"):
            cross.append(code)
        else:
            invalid.append(code)
    return {"resolved": resolved, "invalid": invalid, "cross_road": cross,
            "ok": not invalid and not cross}


def concept_sections_missing_refs(markdown: str) -> bool:
    """B 路硬要求（用户 ⑧）："相关概念"那一节里必须有本路短码引用。

    判定很宽松：只要正文里出现过 `相关概念`/`核心概念` 之后到结尾的范围内存在短码，
    就算满足。找不到该小节时按"缺"处理 —— 那是 B 路提示词里点名必须有的一节。
    """
    text = markdown or ""
    i = max(text.find("相关概念"), text.find("核心概念"))
    if i < 0:
        return True
    return not _scan_refs(text[i:])


# --------------------------------------------------------------------------
# 生成
# --------------------------------------------------------------------------


def _provider(path: str):
    from backend.services.translation.translation_service import (
        _current_model_id, _path_provider,
    )

    return _path_provider(path), _current_model_id(path)


#: 理解是长输出（一份 3~5 千字的自由 Markdown），初始预算给足；不够再按同一套放大
MAX_TOKENS = 12000


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
        from backend.services.translation.translation_service import (
            glossary_text, load_glossary,
        )

        paths = paper_service.paper_paths(paper_id)
        store = PageStore(paper_id, paths["path_a" if path == "a" else "path_b"])
        total = len(list_pages(paper_id))
        skeleton = build_outline_skeleton(paper_id, store)
        glossary = "" if path == "a" else glossary_text(load_glossary(paper_id))
        prefix = build_stable_prefix(generate_brief(paper_id), glossary, skeleton=skeleton)
        history = history_context(store, paper_id, total + 1, max_chars=CONTEXT_MAX_CHARS)
        return f"{prefix}{chr(10) + chr(10) + history if history else ''}"
    except Exception as exc:  # noqa: BLE001 —— 对齐失败只是不享缓存，不能让理解失败
        log.warning("%s 路页级上下文对齐失败（本次不享前缀缓存）：%s", path, exc)
        return ""


def _generate(provider, prompt: str, *, system: str, path: str,
              min_tokens: int = MAX_TOKENS):
    """调模型产出理解，带"思考模式吃光预算"的**升档重试**。

    复用页级流水线那一套判据（`translation_service._escalate_max_tokens`）：
    实测 A 路（技术解剖）第一次就以 `finish=length`、reasoning 8000 tokens 而**正文为空**
    失败 —— B 路同时成功了（35s / 71 条引用），所以这不是"理解不可行"，是预算问题。
    """
    from backend.services.translation.translation_service import _escalate_max_tokens

    budget = max(MAX_TOKENS, int(min_tokens))
    for _ in range(3):
        try:
            return provider.generate(prompt, system=system, temperature=0.3,
                                     max_tokens=budget)
        except Exception as exc:  # noqa: BLE001
            escalated = _escalate_max_tokens(exc, budget)
            if escalated is None:
                raise
            log.warning("%s 路理解正文为空（思考占满预算），max_tokens %d → %d 重试",
                        path, budget, escalated)
            budget = escalated
    raise RuntimeError(f"{path} 路理解重试 3 次仍失败（max_tokens={budget}）")


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
        raise ValueError(f"{paper_id} 还没有可理解的 chunk（先建索引）")
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
        # 没有收尾 JSON 多半是**被输出长度截断**（实测 B 路第一次只写了 4 节就停，
        # 用户口径 ⑧ 要求的"相关概念"整节没写出来 —— 而那正是 B 路最重要的产出）。
        # 放大预算重来一次；这次重试是**有界的**（只一次），不会无限烧调用。
        log.warning("%s 路理解没给出收尾 JSON（疑似被截断），max_tokens ×2 重试", path)
        res = _generate(provider, prompt, system=system, path=path,
                        min_tokens=MAX_TOKENS * 2)
        parsed = parse_result(res.text or "")
    valid = code_map(inventory)
    check = validate_refs(parsed["refs"], valid, path=path)
    missing = concept_sections_missing_refs(parsed["markdown"]) if path == "b" else False
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
        "refs_ok": check["ok"], "missing_concept_refs": missing,
        "updated_at": now_iso(),
    }
    _save(paper_id, path, payload)
    log.info("理解完成（%s，%s 路）：Markdown %d 字，引用 %d 条（有效 %d / 无效 %d / 跨路 %d）",
             paper_id, path, len(payload["markdown"]), len(parsed["refs"]),
             len(check["resolved"]), len(check["invalid"]), len(check["cross_road"]))
    return get_understanding(paper_id, path)


def _stable_prefix(path: str, paper_id: str) -> str:
    """**与页级转录/翻译逐字节相同的稳定前缀**，放在理解 prompt 最前面以命中前缀缓存。

    页级流水线的稳定前缀 = 简报 + 术语表 + **全文标题骨架**（`page_translate.build_stable_prefix`），
    它是整篇字节级不变的，页与页之间靠它命中 provider 的前缀缓存（实测稳定态命中 80%+）。
    理解阶段以前**没有**复用这段：它的 system prompt 是另一套（1320 字 vs 页级 3426/4220 字），
    材料也是另一套（chunk 清单），于是那次调用**零缓存命中、全额计费**（用户 2026-09-27 问的）。

    ⚠️ 只对齐**能对齐的部分**：页级调用带的是**页图**（视觉 token），理解只有文本，
    所以能共享的只有 system + 简报/术语表/骨架这一段；材料本身不可能共享。
    """
    try:
        from backend.services.translation.page_translate import (
            build_outline_skeleton, build_stable_prefix,
        )
        from backend.services.translation.translation_service import (
            glossary_text, load_glossary,
        )

        skeleton = build_outline_skeleton(paper_id)
        glossary = "" if path == "a" else glossary_text(load_glossary(paper_id))
        return build_stable_prefix(generate_brief(paper_id), glossary, skeleton=skeleton)
    except Exception as exc:  # noqa: BLE001 —— 对齐失败只是少一段缓存，不能让理解失败
        log.warning("%s 路稳定前缀对齐失败（本次不享前缀缓存）：%s", path, exc)
        return ""


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
                    "missing_concept_refs", "updated_at"):
            setattr(row, key, payload[key])


def _live_chunk_ids(ids: list[str]) -> set[str]:
    """这些 chunk id 现在还存在于库里吗（重新翻译 + 重建索引后 id 会变）。"""
    if not ids:
        return set()
    from backend.repositories.mysql.models import Chunk

    with session_scope() as sess:
        rows = sess.query(Chunk.id).filter(Chunk.id.in_(ids)).all()
    return {r[0] for r in rows}


def split_live_refs(resolved: dict[str, str], by_code: dict, *,
                    exists=_live_chunk_ids) -> tuple[list[dict], list[dict]]:
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
        (live if chunk_id in alive else stale).append(item)
    return live, stale


_CONCEPT_HEAD_RE = re.compile(r"^#{2,4}\s+(.*)$", re.MULTILINE)
_CONCEPT_SECTION_RE = re.compile(r"相关概念|核心概念")


def parse_concepts(markdown: str, by_code: dict) -> list[dict]:
    """从 B 路（自由 Markdown）里解析出**结构化概念**，供阅读器左栏与"跟随阅读位置"用。

    为什么在**读时**算、而不是生成时落库：理解结果是自由 Markdown（用户口径⑦，模型可增删合并小节），
    所以"概念"这一层是**投影**而不是事实源 —— 读时投影的额外好处是**不用改表、不用重新生成**，
    已经存好的解读立刻就能出概念列表（前端不必等一轮重跑）。

    解析规则（宽松，认不出来就退化成空列表，由前端退回"整段 Markdown"）：
    - 先定位「相关概念 / 核心概念」那一节，只在这一节里找概念小节；
    - 概念小节 = `##`~`####` 标题（标题里可含中英文名，用括号形式时一并拆出来）；
    - 小节正文里出现的 `B023` 短码归到该概念，并通过 `by_code`（短码 → chunk/页码）解析成引用。
    """
    text = markdown or ""
    section = _CONCEPT_SECTION_RE.search(text)
    if not section:
        return []
    # 概念区**到下一个同级或更高级标题为止**：否则"相关概念"之后的 `## 推荐阅读顺序`
    # 也会被当成一个概念（实测就是这么漏进来的）。
    line_start = text.rfind("\n", 0, section.start()) + 1
    head_line = text[line_start:text.find("\n", section.start())]
    base_level = len(head_line) - len(head_line.lstrip("#"))
    body = text[text.find("\n", section.start()) + 1:]
    heads: list[re.Match] = []
    for m in _CONCEPT_HEAD_RE.finditer(body):
        level = len(m.group(0)) - len(m.group(0).lstrip("#"))
        if level <= base_level:
            break                       # 回到同级 → 概念区结束
        heads.append(m)
    if not heads:
        return []
    out: list[dict] = []
    for i, head in enumerate(heads):
        end = heads[i + 1].start() if i + 1 < len(heads) else len(body)
        title = head.group(1).strip()
        chunk = body[head.end():end].strip()
        if not title or not chunk:
            continue
        cn, en = _split_concept_name(title)
        refs: list[dict] = []
        for m in _REF_RE.finditer(chunk):
            code = m.group(1).upper()
            meta = by_code.get(code)
            if not meta:
                continue
            item = {"code": code, "chunk_id": meta.get("chunk_id"),
                    "page": meta.get("page"), "chunk_type": meta.get("chunk_type")}
            if item not in refs:
                refs.append(item)
        out.append({"name": cn, "name_en": en, "body_md": chunk, "refs": refs})
    return out


def _split_concept_name(title: str) -> tuple[str, str]:
    """概念标题 → （中文名, 英文名）。两种常见写法都认：

    ``向量量化（Vector Quantization, VQ）与位宽（bit-width）`` → 前半中文、括号里的英文；
    ``Vector Quantization（向量量化）`` → 反过来。
    """
    text = title.strip()
    m = re.match(r"^(.*?)[（(]([^）)]*)[）)](.*)$", text)
    if not m:
        return text, "" if re.search(r"[\u4e00-\u9fff]", text) else ""
    head, inner, rest = m.group(1).strip(" ：:，,、-—"), m.group(2).strip(), m.group(3).strip()
    if re.search(r"[\u4e00-\u9fff]", head):
        return (head + (" " + rest if rest else "")).strip(), inner
    if re.search(r"[\u4e00-\u9fff]", inner):
        return inner, head
    return text, ""


def related_concepts(paper_id: str, page: int, *, tolerance: int = 1) -> dict:
    """与某一页相关的概念（阅读器"跟随阅读位置"用）。

    命中判据：概念的引用里有 chunk 落在 [page-tolerance, page+tolerance] 内 ——
    引用的单元常常跨页，完全等值会漏掉大量"其实就在这一页"的概念。
    """
    data = get_understanding(paper_id, "b")
    concepts = (data or {}).get("concepts") or []
    lo, hi = int(page) - tolerance, int(page) + tolerance
    hit = [c for c in concepts
           if any(lo <= int(r.get("page") or 0) <= hi for r in (c.get("refs") or []))]
    return {"paper_id": paper_id, "page": int(page), "concepts": hit,
            "total_concepts": len(concepts)}


def to_dict(row: Understanding) -> dict:
    refs = json_loads(row.refs_json, {}) or {}
    inventory = json_loads(row.inventory, []) or []
    by_code = {it["code"]: it for it in inventory if isinstance(it, dict)}
    resolved = refs.get("resolved") or {}
    live_refs, stale_refs = split_live_refs(resolved, by_code)
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
        "missing_concept_refs": bool(row.missing_concept_refs),
        "model": row.model, "updated_at": row.updated_at,
        "prompt_tokens": int(row.prompt_tokens or 0),
        "completion_tokens": int(row.completion_tokens or 0),
        "inventory_size": len(inventory),
        "inventory": inventory,
        # 概念是**读时投影**（见 `parse_concepts`）：已存好的解读立刻可用，不用改表/重生成
        "concepts": parse_concepts(row.markdown or "", by_code),
    }


def get_understanding(paper_id: str, path: str | None = None) -> dict:
    """取某一路的理解结果；``path=None`` 取两路（前端一次要两条）。"""
    with session_scope() as sess:
        if path is not None:
            row = sess.get(Understanding, f"{paper_id}:{normalize_path(path)}")
            return to_dict(row) if row is not None else {}
        rows = (sess.query(Understanding)
                .filter(Understanding.paper_id == paper_id).all())
    by_path = {r.path: to_dict(r) for r in rows}
    return {"paper_id": paper_id, "a": by_path.get("a") or {},
            "b": by_path.get("b") or {}, "done": sorted(by_path)}


def understanding_status(paper_id: str) -> dict:
    """两路各自的状态（前端按钮与进度用）。"""
    with session_scope() as sess:
        rows = (sess.query(Understanding)
                .filter(Understanding.paper_id == paper_id).all())
    have = {r.path: r for r in rows}
    return {
        "paper_id": paper_id,
        "a": {"done": "a" in have, "updated_at": have["a"].updated_at if "a" in have else None},
        "b": {"done": "b" in have, "updated_at": have["b"].updated_at if "b" in have else None},
        "chunk_ready": bool(chunk_inventory(paper_id, "a")),
    }


# --------------------------------------------------------------------------
# 任务入口（与页级翻译同构：一个任务一路，幂等/取消/面板都分得清）
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
    """为两路各建一个理解任务（真并发，与 `start_translation` 同一套路）。"""
    from backend.services.paper import paper_service
    from backend.services.task.task_service import task_service

    paper_service.get_paper(paper_id)
    want = [normalize_path(p) for p in (paths or ["a", "b"])]
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
