"""页级产物（A 路英文转录 / B 路中文翻译）的**手工纠错**。

用于"OCR 认错了字 / 译文译错了"这一类修改：agent 或前端给出 `find`（现有文本）与
`replace`（改成什么），只改**这一处**。

## 为什么改的是页级 Markdown，而不是行、也不是块

`path_a.json` / `path_b.json`（存量论文是 `pages.json`）是**事实源**：阅读器的行、检索的
chunk、双语导出的 Markdown **全部**由它派生 —— 其中行是**读时**由 `pair_rows` 从两侧
Markdown 现算的（`reader_service._merge_page`）。所以改 Markdown 就是改了一切，
不存在"改了 Markdown 还要同步第二份"的问题；反过来，只改行不改 Markdown 会在下次读取时
被现算覆盖掉。

## 为什么是字面替换，而不是整页覆盖

与 DSH harness 的编辑口径一致：**先读、再最小改动**。整页覆盖是最容易造成不可见损坏的
写法 —— 模型没看全的其余段落会被它"顺手重写"一遍，而且看不出来。所以这里的契约是：

1. `find` 必须在**这一路这一页**的 Markdown 里**恰好出现一次**（零次/多次都拒绝，
   并回一段"最像的几行"帮它改对）；`replace` 为空串就是删除；
2. **先读后写**：工具层要求这一轮真读过这一页才允许改（见 `agent/tools.py`）；
3. 改完**行结构必须不变**：行 id 是**位置编号**（`p{页}-{序号}`），行数一变，用户已有的
   高亮 / 笔记与历史引用会整体错位，而且从界面上看不出来。行数变了就**回滚**并报错
   （要改段落结构得重转录那一页）；
4. 落盘后**必须重建索引**（chunks + 向量），否则问答还在引用旧文本 ——
   写完不重建比不改更糟。这里走增量（只重嵌内容变了的 chunk），见
   `knowledge_service.reindex_changed`；
5. 改动前那一份整页文本进 `md/edit_log.jsonl`，`undo` 原样写回。

## 并发

- 同一篇论文的编辑用**进程内锁**串行（`PageStore.flush()` 是整文件原子重写，
  两个写者各持一份内存副本时后写者会把前者的页抹掉）；
- 论文上有**活着的任务**（转录 / 翻译 / 索引…）时直接拒绝：那些任务也在写同一个文件。
"""

from __future__ import annotations

import difflib
import threading

from backend.core.errors import ValidationError
from backend.core.events import EVENT_PAGE_TRANSCRIBED, EVENT_PAGE_TRANSLATED, bus
from backend.core.logging import get_logger
from backend.core.utils import json_dumps, json_loads, now_iso, sha256_text

log = get_logger(__name__)

#: 一次 `find` / `replace` 的长度上限（防"整页覆盖"从后门溜进来）
MAX_FIND = 4000
#: `view` 一次最多回多少字符的正文（超了截断并提示读小一点）
MAX_VIEW = 24000
#: 撤销日志保留多少条（每个条目存一份整页文本，几十 KB 量级）
LOG_KEEP = 200

#: 两侧：`side` → (路号, 页产物里的字段名)
SIDES: dict[str, tuple[str, str]] = {
    "original": ("a", "original_md"),
    "translation": ("b", "translation_md"),
}
#: 允许的写法（模型有时候会写 "en"/"ocr"/"译文"，别让它因为叫法不同被拒）
_ALIASES = {
    "original": "original", "en": "original", "transcript": "original",
    "transcription": "original", "ocr": "original",
    "原文": "original", "转录": "original", "英文": "original",
    "translation": "translation", "zh": "translation", "译文": "translation",
    "翻译": "translation", "中文": "translation",
}

_LOCKS: dict[str, threading.RLock] = {}
_LOCKS_GUARD = threading.Lock()


#: 编辑留下的元信息（撤回时要连它一起还原，否则"撤回"会留下"改过"的痕迹）
EDIT_META = ("edited_at", "edited_by", "edit_count", "edit_reason")


def _restore_meta(payload: dict, prev_meta: dict) -> dict:
    for key in EDIT_META:
        if key in prev_meta:
            payload[key] = prev_meta[key]
        else:
            payload.pop(key, None)
    return payload


def _lock_for(paper_id: str) -> threading.RLock:
    with _LOCKS_GUARD:
        lock = _LOCKS.get(paper_id)
        if lock is None:
            lock = _LOCKS[paper_id] = threading.RLock()
        return lock


def normalize_side(side: str) -> str:
    """``en`` / ``ocr`` / ``译文`` → ``original`` / ``translation``。"""
    key = str(side or "").strip().lower()
    if key in _ALIASES:
        return _ALIASES[key]
    raise ValidationError(f"side 只能是 original（英文转录/OCR）或 translation（中文译文），"
                          f"收到 {side!r}")


def _side_name(key: str) -> str:
    """过程行上的叫法：``original`` → 英文转录，``translation`` → 中文译文。"""
    return "英文转录" if key == "original" else "中文译文"


def _refuse(error: str, **extra) -> dict:
    """拒绝是**结果**不是异常：工具层把它原样回给模型，模型据此换一种改法。"""
    return {"ok": False, "error": error, **extra}


def _resolve_side(side: str) -> tuple[str, dict | None]:
    """侧别解析成 ``(key, 拒绝结果)`` —— 叫法不对也算"拒绝"，不抛栈（见 `_refuse`）。"""
    try:
        return normalize_side(side), None
    except ValidationError as exc:
        return "", _refuse(str(exc), side=str(side))


# --------------------------------------------------------------------------
# 纯函数：定位与替换（单测直接打这里）
# --------------------------------------------------------------------------


def count_occurrences(text: str, find: str) -> int:
    return (text or "").count(find) if find else 0


def line_of(text: str, find: str) -> int:
    """`find` 在文本里的**行号**（1 起，给模型与用户看的"第几行"）。"""
    idx = (text or "").find(find)
    return (text[:idx].count("\n") + 1) if idx >= 0 else 0


def replace_once(text: str, find: str, replace: str) -> str:
    """字面替换一处（调用方必须先保证 `find` 恰好出现一次）。"""
    return text.replace(find, replace, 1)


def near_misses(text: str, find: str, limit: int = 3) -> list[dict]:
    """`find` 没命中时，回几行"最像的" —— OCR 的错字常常只差一两个字符。

    打分用**最长公共片段占 `find` 的比例**（而不是整行相似度）：`find` 通常只是长行里的
    一小段，整行比相似度会把"只差一个下标"的行算成毫不相干（实测 `1/sqrt(dx)` 对
    `The scale factor is 1/sqrt(dk) here.` 的整行比只有 0.36，而最长公共片段占了 find 的 80%）。
    模型照着这几行改一下 `find` 就能命中，省掉它"再读一遍整页"的那一轮。
    """
    probe = (find or "").strip().split("\n")[0].strip()
    if not probe:
        return []
    scored: list[tuple[float, int, str]] = []
    for i, line in enumerate((text or "").split("\n"), start=1):
        stripped = line.strip()
        if not stripped:
            continue
        matcher = difflib.SequenceMatcher(None, probe, stripped, autojunk=False)
        common = matcher.find_longest_match(0, len(probe), 0, len(stripped)).size
        ratio = 0.7 * (common / len(probe)) + 0.3 * matcher.ratio()
        if ratio >= 0.45:
            scored.append((ratio, i, stripped))
    scored.sort(key=lambda x: (-x[0], x[1]))
    return [{"line": i, "text": line[:160], "similarity": round(r, 2)}
            for r, i, line in scored[:limit]]


def numbered(text: str, *, limit: int = MAX_VIEW) -> tuple[str, bool]:
    """给 `view` 用的带行号正文；返回 ``(文本, 是否截断)``。

    ⚠️ 行号只是**定位用的路标**：`find` 里必须写行内容本身，不要带 `|` 与行号
    （工具描述里明说了这一点）。
    """
    body = text or ""
    truncated = len(body) > limit
    if truncated:
        body = body[:limit]
    lines = body.split("\n")
    out = [f"{i:>4}| {line}" for i, line in enumerate(lines, start=1)]
    return "\n".join(out), truncated


# --------------------------------------------------------------------------
# 撤销日志
# --------------------------------------------------------------------------


def log_path(paper_id: str):
    from backend.services.paper import paper_service

    return paper_service.paper_paths(paper_id)["md"] / "edit_log.jsonl"


def _read_log(paper_id: str) -> list[dict]:
    path = log_path(paper_id)
    if not path.is_file():
        return []
    entries: list[dict] = []
    for line in path.read_text(encoding="utf-8", errors="ignore").splitlines():
        line = line.strip()
        if not line:
            continue
        item = json_loads(line)
        if isinstance(item, dict):
            entries.append(item)
    return entries


def _write_log(paper_id: str, entries: list[dict]) -> None:
    path = log_path(paper_id)
    kept = entries[-LOG_KEEP:]
    path.write_text("".join(json_dumps(e, indent=None) + "\n" for e in kept),
                    encoding="utf-8")


def _append_log(paper_id: str, entry: dict) -> None:
    path = log_path(paper_id)
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as fh:
        fh.write(json_dumps(entry, indent=None) + "\n")


# --------------------------------------------------------------------------
# 找"这一路这一页"的落点
# --------------------------------------------------------------------------


def _target(paper_id: str, page: int, side: str):
    """返回 ``(store, field, source)``：这一路这一页的正文存在哪个文件里。

    形态与 `reader_service.page_products` 一致：拆过的论文每路一个文件；存量论文只有
    `md/pages.json`（两侧都在里面）→ 就在那个文件里改，**不**顺手把它拆开。
    """
    from backend.services.paper import reader_service

    field = SIDES[side][1]
    a_store, b_store = reader_service.load_path_stores(paper_id)
    own = a_store if side == "original" else b_store
    own_page = own.page(page)
    if own_page is not None and field in own_page:
        return own, field, "path"
    legacy = reader_service.load_page_store(paper_id)
    legacy_page = legacy.page(page)
    if legacy_page is not None and str(legacy_page.get(field) or "").strip():
        return legacy, field, "legacy"
    if own_page is not None:
        return own, field, "path"
    return None, field, ""


def _blocking_tasks(paper_id: str) -> list[str]:
    """论文上活着的任务（任何类型）：它们也在写产物文件，这时改会被覆盖。"""
    from backend.services.task import task_service as ts

    try:
        rows = ts.task_service.list(paper_id=paper_id, limit=50)
    except Exception as exc:  # noqa: BLE001 —— 查不到任务表不该让编辑整个失败
        log.debug("查活动任务失败（按无任务处理）：%s", exc)
        return []
    active = {ts.PENDING, ts.PROCESSING, ts.PAUSED}
    return sorted({f"{r.get('type')}({r.get('status')})" for r in rows
                   if r.get("status") in active})


def _rows_of(paper_id: str, page: int) -> list[dict] | None:
    from backend.services.paper import reader_service

    merged = reader_service.merged_page(paper_id, page)
    return None if merged is None else list(merged.get("rows") or [])


def _blocks_of(payload: dict) -> list[str] | None:
    """块模式产物（A 定块、B 照译）的**逐块译文**；不是块模式返回 None。

    为什么要单独认它：块模式页的读模型取译文行文本时用的是 `blocks[i]` 而不是
    `translation_md`（`reader_service._merge_page` → `pair_rows_by_blocks`，位置即锚点）。
    只重写 `translation_md` 会出现"工具返回 ok、阅读器那一行还带着旧串"的半修状态
    （实测：`translation_md` 里已经没有 `\tag{3}`，`merged_page(3).rows[p003-03].zh` 还带着）。
    """
    blocks = payload.get("blocks")
    if not payload.get("blocks_from_a") or not isinstance(blocks, list) or not blocks:
        return None
    return [str(one or "") for one in blocks]


def _structure(store, page: int, field: str, paper_id: str) -> tuple[int, int]:
    """这一侧的**结构签名** ``(块数, 合并行数)``。

    块数来自 `split_blocks`（纯结构切分：空行分段、`$$` 围栏成块……），是"有没有增删段落"
    的直接判据；合并行数是"引用能不能跳准"的判据。**两个都要看**：实测只比行数不够 ——
    把 A 侧两段并成一段时，`pair_rows` 的结构对齐会用"单侧行"把行数补齐（7 行还是 7 行），
    但 A 侧少了一个块，行与块的对应已经漂了。
    """
    from backend.services.translation.page_translate import split_blocks

    payload = store.page(page) or {}
    blocks = len(split_blocks(str(payload.get(field) or "")))
    return blocks, len(_rows_of(paper_id, page) or [])


def _publish(paper_id: str, page: int, side: str) -> None:
    """让**已经打开这一页**的阅读器立刻看到修订结果（走 A/B 路各自的 SSE 事件）。"""
    from backend.services.paper import reader_service

    merged = reader_service.merged_page(paper_id, page) or {}
    rows = merged.get("rows") or []
    if not rows:
        return
    bus.publish("papers",
                EVENT_PAGE_TRANSCRIBED if side == "original" else EVENT_PAGE_TRANSLATED,
                {"paper_id": paper_id, "page": page,
                 "page_total": merged.get("page_total"),
                 "path": SIDES[side][0], "task_type": "manual_edit",
                 "rows": rows, "align": merged.get("align"),
                 "model": merged.get("model"), "cached": False, "task_id": None,
                 "updated_at": merged.get("updated_at"), "edited": True})


# --------------------------------------------------------------------------
# 读
# --------------------------------------------------------------------------


def view(paper_id: str, page: int, side: str) -> dict:
    """看**这一路这一页**的原始 Markdown（带行号）—— 编辑前必读的那一份。

    与 `page_markdown_get` 的区别：那个给的是**合并后的行**（原文 + 译文，已按块切好），
    适合读与引用；这里给的是**产物原文**，适合改 —— `find` 必须与它逐字一致。
    """
    from backend.services.paper import paper_service

    paper_service.get_paper(paper_id)
    key, bad = _resolve_side(side)
    if bad is not None:
        return {**bad, "page": int(page)}
    page = int(page)
    store, field, source = _target(paper_id, page, key)
    payload = store.page(page) if store is not None else None
    if payload is None:
        return _refuse(f"第 {page} 页没有产物（这篇论文这一页还没转录/翻译），无从修改。",
                       page=page, side=key)
    text = str(payload.get(field) or "")
    if not text.strip():
        return _refuse(f"第 {page} 页的"
                       f"{'英文转录' if key == 'original' else '中文译文'}是空的。",
                       page=page, side=key)
    lines, truncated = numbered(text)
    rows = _rows_of(paper_id, page) or []
    all_lines = text.split("\n")
    return {
        "page": page, "side": key, "source": source,
        "chars": len(text),
        #: `rows` = **读模型的行（块）数**，与 `replace` 回执里的「行数不变」同一口径；
        #: 它**不是** Markdown 的行数（段落之间有空行，两者必然不同）。2026-12-07 用户被这两个
        #: 数字搞混过（rows 15 / lines 29），所以把 Markdown 那一侧的计数单独给出来。
        "rows": len(rows),
        "lines_total": len(all_lines),
        "lines_non_empty": sum(1 for ln in all_lines if ln.strip()),
        "truncated": truncated,
        "lines": lines,
        #: `summary` = 过程行上那一句人话（`_tool_outcome` 优先取它）。`note` 是给**模型**的
        #: 长解释，用户不用看；两者分开，前端那一行才不会显示成半句说明书。
        "summary": f"第 {page} 页 {_side_name(key)} · {len(text)} 字",
        "note": ("这是这一页产物的原始 Markdown（行号只是路标，`find` 里写行内容本身，"
                 "不要带 `|` 和行号）。改写用 op=\"replace\"：find 必须**逐字**取自这里、"
                 "且在整页里只出现一次。"),
    }


# --------------------------------------------------------------------------
# 写
# --------------------------------------------------------------------------


def replace(paper_id: str, page: int, side: str, find: str, replace_text: str, *,
            reason: str = "", actor: str = "agent", reindex: bool = True) -> dict:
    """把这一路这一页里的 `find`（唯一命中）换成 `replace_text`。"""
    from backend.services.paper import paper_service, reader_service

    paper_service.get_paper(paper_id)
    key, bad = _resolve_side(side)
    if bad is not None:
        return {**bad, "page": int(page)}
    page = int(page)
    find = "" if find is None else str(find)
    replace_text = "" if replace_text is None else str(replace_text)
    if not find.strip():
        return _refuse("find 不能为空：它是「你要改掉的原文」，请逐字取自 view 的结果。")
    if len(find) > MAX_FIND or len(replace_text) > MAX_FIND:
        return _refuse(f"find / replace 都不得超过 {MAX_FIND} 字符 —— 这里只做**一处**"
                       f"小修正，不接受整页覆盖。")
    if find == replace_text:
        return _refuse("find 与 replace 一模一样，等于没改。")

    blocking = _blocking_tasks(paper_id)
    if blocking:
        return _refuse(f"这篇论文上还有活着的任务（{'、'.join(blocking)}），它们也在写产物文件，"
                       f"现在改会被覆盖。等任务结束再改。")

    with _lock_for(paper_id):
        store, field, source = _target(paper_id, page, key)
        payload = dict(store.page(page) or {}) if store is not None else {}
        if not payload:
            return _refuse(f"第 {page} 页没有产物，无从修改。", page=page, side=key)
        text = str(payload.get(field) or "")
        hits = count_occurrences(text, find)
        if hits == 0:
            return _refuse(
                f"find 在{'英文转录' if key == 'original' else '中文译文'}里没找到"
                f"（第 {page} 页）。用 op=\"view\" 看一眼原文，按**逐字**的内容重写 find。",
                page=page, side=key, near_miss=near_misses(text, find))
        if hits > 1:
            return _refuse(
                f"find 在这一页里出现了 {hits} 次，无法确定改哪一处。"
                f"把它**连同前后文**一起写进 find（比如带上上一行或下一行），使它在整页里唯一。",
                page=page, side=key, occurrences=hits)

        # 块模式页（A 定块、B 照译）：读模型取**译文行**来自 `blocks[]`，只改 *_md 等于
        # 改了一半 —— 阅读器那一行还是旧串，而工具已经回了 ok。这里同步改块，并且与整页
        # 同一套"唯一命中"口径（0 命中 = 这段文字不在块里，跳过；>1 命中 = 拒绝并要求加前后文）。
        blocks = _blocks_of(payload)
        new_blocks: list[str] | None = None
        block_index: int | None = None
        blocks_skipped = False
        if blocks is not None:
            per_block = [count_occurrences(one, find) for one in blocks]
            total = sum(per_block)
            if total > 1:
                return _refuse(
                    f"find 在这一页的**逐块译文**里出现了 {total} 次，无法确定改哪一处。"
                    f"把它连同前后文一起写进 find，使它在整页里唯一。",
                    page=page, side=key, occurrences=total)
            if total == 1:
                block_index = per_block.index(1)
                new_blocks = list(blocks)
                new_blocks[block_index] = replace_once(blocks[block_index], find, replace_text)
                # 结构校验照旧，只是这次连块一起看：块数与被改块的行数都不许变
                if (len(new_blocks) != len(blocks)
                        or new_blocks[block_index].count("\n") != blocks[block_index].count("\n")):
                    return _refuse(
                        f"这次修改会改变第 {page} 页的段落结构（逐块译文里多/少了换行），已拒绝。"
                        f"行与块都是**位置编号**：一旦增删段落，已有高亮 / 笔记与历史引用会整体错位。"
                        f"请改成**同一段内**改字 / 改词。",
                        page=page, side=key)
            else:
                blocks_skipped = True

        before_struct = _structure(store, page, field, paper_id)
        new_text = replace_once(text, find, replace_text)
        updated = {**payload, field: new_text, "edited_at": now_iso(),
                   "edited_by": actor, "edit_count": int(payload.get("edit_count") or 0) + 1}
        if new_blocks is not None:
            updated["blocks"] = new_blocks
        if reason:
            updated["edit_reason"] = str(reason)[:200]
        store.put(page, updated)

        after_struct = _structure(store, page, field, paper_id)
        if after_struct != before_struct:
            store.put(page, payload)                     # 回滚：行结构变了
            reader_service.invalidate_cache(paper_id)
            blocks_before, rows_before = before_struct
            blocks_after, rows_after = after_struct
            return _refuse(
                f"这次修改会改变第 {page} 页的结构（段落块 {blocks_before} → {blocks_after}、"
                f"行 {rows_before} → {rows_after}），已回滚。行与块都是**位置编号**：一旦增删段落，"
                f"这一页已有的高亮 / 笔记与历史引用会整体错位，而且从界面上看不出来。"
                f"请改成**不改变段落结构**的修法（同一段内改字 / 改词）；"
                f"确实要增删段落，得重转录这一页。",
                page=page, side=key, blocks_before=blocks_before,
                blocks_after=blocks_after, rows_before=rows_before,
                rows_after=rows_after)

        # 日志在**锁内**追加：并发编辑同一篇时，"读日志 → 改日志"这条链必须串行，
        # 否则两条记录会互相盖掉（撤回是照日志写回的，丢了记录就撤不回来了）。
        line_no = line_of(text, find)
        entry = {
            "id": sha256_text(f"{paper_id}:{page}:{key}:{now_iso()}:{find}")[:16],
            "at": now_iso(), "page": page, "side": key, "source": source, "actor": actor,
            "reason": str(reason or "")[:200], "line": line_no,
            "find": find, "replace": replace_text, "prev_md": text, "next_md": new_text,
            # 编辑前的元信息：撤回时原样还原（否则"撤回"会留下"改过"的痕迹）
            "prev_meta": {k: payload[k] for k in EDIT_META if k in payload},
        }
        if new_blocks is not None:
            # 块模式那一刀也必须进日志：否则 undo 只还原 *_md，又留下"磁盘对了、读模型还对
            # —— 或者反过来"的半修状态（这正是这次要修的 bug 的回滚版）
            entry["prev_blocks"] = blocks
            entry["next_blocks"] = new_blocks
            entry["block_index"] = block_index
        _append_log(paper_id, entry)

    reader_service.invalidate_cache(paper_id)
    _publish(paper_id, page, key)
    index = _reindex(paper_id, reindex)
    log.info("产物纠错：%s p%d %s 第 %d 行（%d → %d 字）", paper_id, page, key,
             line_no, len(find), len(replace_text))
    return {
        "ok": True, "page": page, "side": key, "source": source, "line": line_no,
        "before": find if len(find) <= 400 else find[:400] + "…",
        "after": replace_text if len(replace_text) <= 400 else replace_text[:400] + "…",
        "rows": before_struct[1], "chars": len(new_text),
        "index": index,
        "blocks": ({"index": block_index} if new_blocks is not None else None),
        "summary": (f"第 {page} 页 · 第 {line_no} 行 → "
                    + (f"「{replace_text}」" if len(replace_text) <= 24
                       else f"「{replace_text[:24]}…」") if replace_text else "（删除）"),
        "note": ("已落盘。这一页的阅读器内容与检索索引都已更新；"
                 "改动前的那一份留在 edit_log 里，op=\"undo\" 可以原样撤回。"
                 + ("\n⚠️ 这段文字只出现在整页 Markdown 里、不在逐块译文里："
                    "阅读器按块渲染的那一行可能仍是旧文本，必要时用 view 复核。"
                    if blocks_skipped else "")),
    }


def undo(paper_id: str, page: int, side: str, *, actor: str = "agent",
         reindex: bool = True) -> dict:
    """撤回这一路这一页**最后一次**编辑（把改动前的整页文本原样写回）。"""
    from backend.services.paper import paper_service, reader_service

    paper_service.get_paper(paper_id)
    key, bad = _resolve_side(side)
    if bad is not None:
        return {**bad, "page": int(page)}
    page = int(page)
    blocking = _blocking_tasks(paper_id)
    if blocking:
        return _refuse(f"论文上还有活着的任务（{'、'.join(blocking)}），现在撤回会被覆盖。")

    with _lock_for(paper_id):
        entries = _read_log(paper_id)
        idx = next((i for i in range(len(entries) - 1, -1, -1)
                    if int(entries[i].get("page") or 0) == page
                    and entries[i].get("side") == key), None)
        if idx is None:
            return _refuse(f"没有找到第 {page} 页"
                           f"{'英文转录' if key == 'original' else '中文译文'}的编辑记录，无从撤回。",
                           page=page, side=key)
        entry = entries[idx]
        store, field, source = _target(paper_id, page, key)
        payload = dict(store.page(page) or {}) if store is not None else {}
        if not payload:
            return _refuse(f"第 {page} 页的产物已经不在了（可能重转录过），无从撤回。")
        before_struct = _structure(store, page, field, paper_id)
        restored = str(entry.get("prev_md") or "")
        updated = _restore_meta({**payload, field: restored},
                               dict(entry.get("prev_meta") or {}))
        prev_blocks = entry.get("prev_blocks")
        if isinstance(prev_blocks, list) and isinstance(payload.get("blocks"), list):
            # 块模式那一刀一起回滚：只还原 *_md 的话，读模型（按 blocks 取译文行）还是改后的
            # 文本 —— "撤回成功"就又变成了半修状态
            updated["blocks"] = [str(one or "") for one in prev_blocks]
        store.put(page, updated)
        after_struct = _structure(store, page, field, paper_id)
        if after_struct != before_struct:
            store.put(page, payload)
            reader_service.invalidate_cache(paper_id)
            return _refuse("撤回会改变这一页的结构，已回滚（产物已被别处改过？）。")
        _write_log(paper_id, entries[:idx] + entries[idx + 1:])

    reader_service.invalidate_cache(paper_id)
    _publish(paper_id, page, key)
    index = _reindex(paper_id, reindex)
    log.info("产物纠错：撤回 %s p%d %s（%s，by %s）", paper_id, page, key,
             entry.get("at"), actor)
    return {"ok": True, "page": page, "side": key, "source": source,
            "summary": f"第 {page} 页 · 已撤回上一处",
            "reverted": {"find": entry.get("find"), "replace": entry.get("replace"),
                         "at": entry.get("at")},
            "rows": before_struct[1], "chars": len(restored), "index": index,
            "note": "已撤回上一次编辑，正文与索引都回到改动前。"}


def _reindex(paper_id: str, enabled: bool) -> dict:
    """重建这一篇的检索索引（增量）。失败**不算编辑失败** —— 但必须如实报给模型。"""
    if not enabled:
        return {"skipped": True, "reason": "调用方要求跳过（自检 / 批量改完再重建）"}
    try:
        from backend.services.knowledge import knowledge_service
        from backend.services.task.handlers import _set_paper_indexed

        data = knowledge_service.reindex_changed(paper_id)
        _set_paper_indexed(paper_id)
        return {"ok": True, **data}
    except Exception as exc:  # noqa: BLE001
        log.warning("编辑后重建索引失败（%s）：%s", paper_id, exc)
        return {"ok": False,
                "error": f"{type(exc).__name__}: {str(exc)[:200]}",
                "hint": "正文已经改好，但检索索引没跟上 —— 下次问答可能还引用旧文本，"
                        "请在设置页对这篇论文重建一次索引。"}
