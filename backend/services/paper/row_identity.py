"""正文行的稳定身份（pid）注册表 —— 只做身份这一层，不接线（见 docs/身份模型.md §2/§3/§4）。

为什么需要它：页级产物里的"行"在正常两路形态下**不落盘**（reader_service.page_products
每次读时当场重算），行 id 是**位置编号** p015-06。页内插一行、合一行、重切分，
后面所有行就换号，引用全断。

pid 的做法：**生成一次、重建时继承、永不重绑**。内容的指纹只用来**对齐**
（把旧 pid 续给新行），不参与身份；对不上的旧 pid 进 retired，
引用它的时候才能提示"内容已变/已失效"，而不是静默跳错。

本模块只提供三件事：
1. 指纹 —— 复用 rebind.text_fingerprint（行文本取"原文优先、退译文"）；
2. 继承/新建/退休 —— plan_page 是纯函数，可单测；
3. 注册表读写 —— registry_path / load / save / assign（唯一碰文件的地方）。

刻意不做的事：不接线给 reader_service / page_translate，不写数据库，不动前端。
接线是下一步（阶段 1b）。
"""

from __future__ import annotations

import logging
import threading
import uuid
from pathlib import Path

from backend.core.utils import now_iso, read_json, write_json
from backend.parsing.ir.rebind import text_fingerprint

# 注册表 schema 版本（写入即带；读的时候不校验，只原样保留未知字段）
VERSION = 1
# 注册表文件名，落在 data/papers/<id>/md/ 下（唯一在重建中不被删的目录）
REGISTRY_NAME = "row_ids.json"
# 相似度阈值：与 rebind.rebind_block_ids 同口径（0.72），低于它一律算"对不上"
MATCH_THRESHOLD = 0.72
# "同一槽位但文本大改"的下限：用于区分 retired 的 reason（text-changed / gone）
CHANGED_THRESHOLD = 0.5
# retired 上限（超出丢最旧）：退休名单只是审计线索，不能无限长
RETIRED_LIMIT = 5000

log = logging.getLogger(__name__)

_pid_lock = threading.Lock()
_paper_locks: dict[str, threading.Lock] = {}


# --------------------------------------------------------------------------
# 指纹与相似度（纯函数）
# --------------------------------------------------------------------------


def row_text(row: dict) -> str:
    """行的对齐用文本：原文优先、退译文（都没有就空串）。"""
    return str(row.get("original") or row.get("translation") or "")


def fingerprint(row: dict) -> str:
    """行的文本指纹（与解析块同一套归一化，便于跨层比对）。"""
    return text_fingerprint(row_text(row))


def similarity(a: str, b: str) -> float:
    """文本指纹相似度：前缀重合 0.7 + 长度比 0.3，只比前 48 字符。

    与 rebind._similarity **同一口径**（那边吃 BlockFingerprint，这里吃裸指纹串，
    公式逐行对齐；改任何一边都要同步）。
    """
    if not a or not b:
        return 0.0
    n = min(len(a), len(b), 48)
    same = sum(1 for i in range(n) if a[i] == b[i])
    prefix = same / n
    len_ratio = min(len(a), len(b)) / max(len(a), len(b))
    return 0.7 * prefix + 0.3 * len_ratio


def new_pid() -> str:
    """新身份：r-<16 位十六进制>（UUID，不是内容哈希 —— 内容变了 pid 也不变）。"""
    return "r-" + uuid.uuid4().hex[:16]


def empty_registry() -> dict:
    """空注册表（缺失/损坏时的返回值，也是新建注册表的初值）。"""
    return {"version": VERSION, "rows": [], "retired": []}


# --------------------------------------------------------------------------
# 继承 / 新建 / 退休（纯函数，不碰文件）
# --------------------------------------------------------------------------


def _retire_reason(entry: dict, fps: list[str]) -> str:
    """旧条目为什么退休：同一槽位文本大改 → text-changed；否则这段没了 → gone。"""
    try:
        idx = int(entry.get("order") or -1)
    except (TypeError, ValueError):
        return "gone"
    if 0 <= idx < len(fps) and similarity(str(entry.get("fp") or ""), fps[idx]) >= CHANGED_THRESHOLD:
        return "text-changed"
    return "gone"


def plan_page(rows: list[dict], existing: list[dict]) -> dict:
    """一页的行 → 每条该用哪个 pid。

    rows:     [{"id","original","translation"}...]（页内序）
    existing: 注册表里**本页**现存条目 [{"pid","order","fp"}...]

    返回 {"pids": [pid...],       # 与 rows 等长
          "created": [条目...],    # 新建的注册表条目（含 order/fp/created_at）
          "retired": [pid...],     # 这次没被认领的旧 pid
          "reused": int}           # 复用了多少条

    算法（按顺序，绝不把旧 pid 重绑到对不上的行）：
    ① 精确命中：fp 相同且未被本页认领 → 直接续用（重复 fp 按页内顺序一对一配）；
    ② 相似对齐：剩下的按页内顺序，相似度过 0.72 才认领（一对一）；
    ③ 其余新建；rows 没认领的旧 pid 进 retired。
    """
    fps = [fingerprint(r) for r in rows]
    pool = sorted(existing, key=lambda e: int(e.get("order") or 0))

    # ① 精确命中：同 fp 的条目按 order 排队，行按页内顺序一对一取用。
    buckets: dict[str, list[dict]] = {}
    for entry in pool:
        buckets.setdefault(str(entry.get("fp") or ""), []).append(entry)
    claimed: list[dict | None] = [None] * len(rows)
    for i, fp in enumerate(fps):
        queue = buckets.get(fp)
        if queue:
            claimed[i] = queue.pop(0)

    # ② 相似对齐：只在"两边都还有剩"的里面配，且必须过阈值。
    taken_pids = {str(e["pid"]) for e in claimed if e is not None}
    rest = [e for e in pool if str(e["pid"]) not in taken_pids]
    for i, fp in enumerate(fps):
        if claimed[i] is not None:
            continue
        best: dict | None = None
        best_sim = 0.0
        for entry in rest:
            sim = similarity(fp, str(entry.get("fp") or ""))
            if sim >= MATCH_THRESHOLD and sim > best_sim:
                best, best_sim = entry, sim
        if best is not None:
            claimed[i] = best
            rest.remove(best)

    # ③ 其余新建。
    pids: list[str] = []
    created: list[dict] = []
    used: set[str] = set()
    reused = 0
    for i, entry in enumerate(claimed):
        if entry is not None:
            pid = str(entry["pid"])
            pids.append(pid)
            used.add(pid)
            reused += 1
        else:
            item = {"pid": new_pid(), "order": i, "fp": fps[i], "created_at": now_iso()}
            created.append(item)
            used.add(item["pid"])
            pids.append(item["pid"])

    retired = [str(e["pid"]) for e in pool if str(e["pid"]) not in used]
    return {"pids": pids, "created": created, "retired": retired, "reused": reused}


# --------------------------------------------------------------------------
# 注册表读写（本模块唯一碰文件的地方）
# --------------------------------------------------------------------------


def registry_path(paper_id: str, *, root: Path | None = None) -> Path:
    """注册表路径：<论文目录>/md/row_ids.json。

    真实目录一律走现成的 paper_service.paper_paths(paper_id)["md"]
    （= Settings.paper_dir(paper_id) / "md"，见 paper_service.py:486/492），
    不在这里自己拼 data/papers/...。

    root 只给单测用：传了就当成"数据根"，按 root/<论文id>/md/ 落盘，
    免得测试往真实 data/ 里写。
    """
    if root is not None:
        md_dir = Path(root) / paper_id / "md"
    else:
        from backend.services.paper.paper_service import paper_paths  # 延迟导入：避免循环依赖

        md_dir = paper_paths(paper_id)["md"]
    return Path(md_dir) / REGISTRY_NAME


def load(paper_id: str, *, root: Path | None = None) -> dict:
    """读注册表。缺失 / 损坏 / JSON 坏 → 空注册表，**绝不抛**（pid 可以现建现用）。

    未知字段原样保留（forward-compat：别的版本写了什么，这里不删）。
    """
    data = read_json(registry_path(paper_id, root=root), default=None)
    if not isinstance(data, dict):
        return empty_registry()
    reg = dict(data)  # 浅拷贝，未知顶层字段跟着走
    if not isinstance(reg.get("rows"), list):
        reg["rows"] = []
    if not isinstance(reg.get("retired"), list):
        reg["retired"] = []
    reg.setdefault("version", VERSION)
    return reg


def save(paper_id: str, registry: dict, *, root: Path | None = None) -> None:
    """原子写注册表（临时文件 + os.replace，目录不存在就建）。

    复用 core.utils.write_json → write_text_atomic（Windows 上带重试与覆盖兜底）。
    """
    write_json(registry_path(paper_id, root=root), registry, indent=2)


def _lock_for(paper_id: str) -> threading.Lock:
    """同一篇论文的注册表读写在**进程内**串行（不要求跨进程）。"""
    with _pid_lock:
        lock = _paper_locks.get(paper_id)
        if lock is None:
            lock = _paper_locks[paper_id] = threading.Lock()
        return lock


def assign_pages(paper_id: str, pages: dict[int, list[dict]],
                *, root: Path | None = None) -> dict[int, list[str]]:
    """多页一起分配 pid：**一次 load** + 每页 plan_page + 合并 + **至多一次 save**。

    逐页语义与 `assign` **完全一致**（同一份注册表、同样的退休/新建规则、幂等、
    不变则不写）—— `assign` 就是它的薄包装。批量入口只为 `reader_service.page_products`
    存在：那边一次要出一整篇的页，逐页调 `assign` 会把注册表读/写"页数"那么多遍
    （每次读都要解析整份 JSON），而"整篇一次算完"才保证同一份注册表下的一致结果。

    空页（`rows` 为空）与 `assign` 同口径：不碰注册表，结果给空列表。
    """
    wanted: dict[int, list[dict]] = {int(p): list(rows or [])
                                     for p, rows in (pages or {}).items()}
    result: dict[int, list[str]] = {p: [] for p in wanted}
    todo = {p: rows for p, rows in wanted.items() if rows}
    if not todo:
        return result
    with _lock_for(paper_id):
        reg = load(paper_id, root=root)
        entries: dict[int, list[dict]] = {}                    # 每页原有的条目（按 order）
        plans: dict[int, tuple[dict, dict[str, dict]]] = {}    # 每页 (plan, pid→条目)
        changed = False
        # ① 逐页规划：只读内存里的 reg，页与页互不干扰（与单页算法逐行同构）
        for page in sorted(todo):
            rows = todo[page]
            page_rows = [e for e in reg["rows"] if _page_of(e) == page]
            page_rows.sort(key=lambda e: int(e.get("order") or 0))
            plan = plan_page(rows, page_rows)

            by_pid = {str(e.get("pid")): e for e in page_rows}
            if plan["created"] or plan["retired"]:
                changed = True
            for index, pid in enumerate(plan["pids"]):
                entry = by_pid.get(pid)
                if entry is None:
                    continue
                fp = fingerprint(rows[index])
                if entry.get("order") != index or entry.get("fp") != fp:
                    changed = True  # 行挪了位 / 文本小改（过阈值）→ 注册表要跟着更新
                entry["order"] = index
                entry["fp"] = fp
            entries[page] = page_rows
            plans[page] = (plan, by_pid)
            result[page] = list(plan["pids"])
        if not changed:
            return result

        # ② 合并：没碰的页原样带走；碰到的页只留"被认领的旧条目"+新建条目
        merged = [e for e in reg["rows"] if _page_of(e) not in todo]
        retired = list(reg["retired"])
        for page in sorted(todo):
            plan, by_pid = plans[page]
            live = {str(pid) for pid in plan["pids"]}
            merged += [by_pid[str(e["pid"])] for e in entries[page]
                       if str(e["pid"]) in live]
            merged += [{**item, "page": page} for item in plan["created"]]
            if plan["retired"]:
                fps = [fingerprint(r) for r in todo[page]]
                for pid in plan["retired"]:
                    old = by_pid.get(str(pid)) or {}
                    retired.append({
                        "pid": str(pid),
                        "page": page,
                        "fp": str(old.get("fp") or ""),
                        "retired_at": now_iso(),
                        "reason": _retire_reason(old, fps),
                    })
        if len(retired) > RETIRED_LIMIT:
            retired = retired[-RETIRED_LIMIT:]  # 超出丢最旧
        merged.sort(key=lambda e: (_page_of(e), int(e.get("order") or 0)))

        reg["version"] = reg.get("version") or VERSION
        reg["rows"] = merged
        reg["retired"] = retired
        # 写盘失败**不许炸读路径**：注册表是派生物，磁盘满 / 只读挂载时宁可这次身份不落盘
        # （下次读会重新分配，日志里有痕迹），也不能让"打开论文"失败。
        try:
            save(paper_id, reg, root=root)  # 整批**一次**写盘
        except Exception as exc:  # noqa: BLE001
            log.warning("行身份注册表写入失败（本次 pid 不落盘）：%s: %s",
                        paper_id, exc)
        return result


def assign(paper_id: str, page: int, rows: list[dict], *, root: Path | None = None) -> list[str]:
    """给一页的行分配 pid（`assign_pages` 的单页薄包装，语义与它完全一致）。

    每次读页面都会调用，所以必须幂等（同一批行连着调两次，第二次不写文件）、
    快（一次文件读，无 DB）、并发安全（同 paper 一把线程锁）—— 这三条都在
    `assign_pages` 里落实，本函数只是把这一页塞进批量入口、再把那一页的 pids 拿回来。

    空页（rows 为空）不动注册表：那是"这页这次没产出"，不是"这页的行都没了"。
    否则一次瞬时读失败就会把整页身份退休掉，再回来时全是新 pid。
    """
    return assign_pages(paper_id, {int(page): rows}, root=root).get(int(page), [])


def _page_of(entry: dict) -> int:
    try:
        return int(entry.get("page"))
    except (TypeError, ValueError):
        return -1
