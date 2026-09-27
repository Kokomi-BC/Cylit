"""block_id 稳定重绑定（规划 §9.2）。

策略：``page + bbox 近似 + 文本指纹``。重跑解析时先把新块与旧 IR 的块做匹配，
匹配上的沿用旧 ID，未匹配的分配新 ID，**保证重跑 ID 稳定**（阶段 03 验收项）。
"""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass, field

from backend.core.utils import json_dumps, sha256_text, write_json
from backend.parsing.models import Block

# bbox 近似容差（pt）
BBOX_TOL = 8.0
# 文本指纹：去空白与标点后的前缀
_FP_STRIP = re.compile(r"[\s\W_]+", re.UNICODE)


@dataclass
class BlockFingerprint:
    id: str
    page_no: int
    x0: float
    y0: float
    x1: float
    y1: float
    type: str
    text_fp: str
    content_hash: str = ""

    @staticmethod
    def from_block(b: Block) -> "BlockFingerprint":
        return BlockFingerprint(
            id=b.id, page_no=b.page_no, x0=b.rect.x0, y0=b.rect.y0,
            x1=b.rect.x1, y1=b.rect.y1, type=b.type,
            text_fp=text_fingerprint(b.text_clean or b.text),
            content_hash=b.content_hash,
        )

    @staticmethod
    def from_row(row: dict) -> "BlockFingerprint":
        return BlockFingerprint(
            id=row["id"], page_no=int(row.get("page_no") or 0),
            x0=float(row.get("x0") or 0), y0=float(row.get("y0") or 0),
            x1=float(row.get("x1") or 0), y1=float(row.get("y1") or 0),
            type=row.get("type") or "para",
            text_fp=text_fingerprint(row.get("text_clean") or row.get("text") or ""),
            content_hash=row.get("content_hash") or "",
        )


def text_fingerprint(text: str, length: int = 64) -> str:
    return _FP_STRIP.sub("", text or "").lower()[:length]


def _bbox_close(a: BlockFingerprint, b: BlockFingerprint) -> bool:
    return (abs(a.x0 - b.x0) <= BBOX_TOL and abs(a.y0 - b.y0) <= BBOX_TOL
            and abs(a.x1 - b.x1) <= BBOX_TOL and abs(a.y1 - b.y1) <= BBOX_TOL)


def _similarity(a: BlockFingerprint, b: BlockFingerprint) -> float:
    """文本指纹相似度（前缀重合 + 长度比）。"""
    if not a.text_fp or not b.text_fp:
        return 0.0
    n = min(len(a.text_fp), len(b.text_fp), 48)
    same = sum(1 for i in range(n) if a.text_fp[i] == b.text_fp[i])
    prefix = same / n
    len_ratio = min(len(a.text_fp), len(b.text_fp)) / max(len(a.text_fp), len(b.text_fp))
    return 0.7 * prefix + 0.3 * len_ratio


@dataclass
class MatchReport:
    matched: int = 0
    new_ids: int = 0
    renamed: list[tuple[str, str]] = field(default_factory=list)
    reused_ids: dict[str, str] = field(default_factory=dict)

    def to_dict(self) -> dict:
        return {"matched": self.matched, "new_ids": self.new_ids,
                "renamed": self.renamed[:200], "renamed_count": len(self.renamed)}


def rebind_block_ids(
    new_blocks: list[Block],
    old_fingerprints: list[BlockFingerprint],
    similarity_threshold: float = 0.72,
) -> MatchReport:
    """把新块与旧 IR 匹配，匹配成功的沿用旧 ID（就地修改 ``new_blocks``）。

    匹配优先级：
    1. content_hash 完全相同 → 直接复用（最快最稳）
    2. 同页 + bbox 近似 + 文本指纹相似度 ≥ 阈值
    """
    report = MatchReport()
    if not old_fingerprints:
        report.new_ids = len(new_blocks)
        return report

    # 先把所有新块改成临时 ID，避免"新块 A 占用了旧块 B 的 ID"导致主键冲突
    for i, b in enumerate(new_blocks):
        b.id = f"__new{i:05d}"

    by_hash: dict[str, list[BlockFingerprint]] = {}
    by_page: dict[int, list[BlockFingerprint]] = {}
    for fp in old_fingerprints:
        if fp.content_hash:
            by_hash.setdefault(fp.content_hash, []).append(fp)
        by_page.setdefault(fp.page_no, []).append(fp)

    used: set[str] = set()
    pending: list[Block] = []

    for b in new_blocks:
        cands = by_hash.get(b.content_hash or "", [])
        pick = next((c for c in cands if c.id not in used
                     and c.type == b.type), None)
        if pick is not None:
            used.add(pick.id)
            _rename(b, pick.id, report)
            report.matched += 1
            continue
        pending.append(b)

    for b in pending:
        best: tuple[float, BlockFingerprint] | None = None
        for fp in by_page.get(b.page_no, []):
            if fp.id in used or fp.type != b.type:
                continue
            if not _bbox_close(BlockFingerprint.from_block(b), fp):
                continue
            sim = _similarity(BlockFingerprint.from_block(b), fp)
            if sim >= similarity_threshold and (best is None or sim > best[0]):
                best = (sim, fp)
        if best is None:
            continue
        used.add(best[1].id)
        _rename(b, best[1].id, report)
        report.matched += 1

    remaining = [b for b in new_blocks if b.id.startswith("__new")]
    if remaining:
        # 新增块：分配该页下一个可用的顺序 ID（跳过已沿用的旧 ID）
        prefix = id_prefix(_infer_paper_id(new_blocks))
        assign_ids_in_order(remaining, reserve=used, prefix=prefix)
    report.new_ids = len(remaining)
    return report


def _infer_paper_id(blocks: list[Block]) -> str:
    for b in blocks:
        if b.paper_id:
            return b.paper_id
    return "p"


def _rename(block: Block, new_id: str, report: MatchReport) -> None:
    if block.id == new_id:
        report.reused_ids[block.id] = new_id
        return
    report.renamed.append((block.id, new_id))
    block.id = new_id
    block.md_anchor = f"blk-{new_id}"
    report.reused_ids[new_id] = new_id


def id_prefix(paper_id: str, taken: set[str] | None = None) -> str:
    """块 ID 的论文前缀（保证跨论文在共享业务库中唯一）。

    ``paper_id`` 已是 slug（小写字母/数字/连字符），这里去掉连字符并截断到 12 字符，
    得到 ``turboquantic`` 之类的短前缀 → 块 ID 形如 ``turboquantic-p07-b12``。

    ⚠️ **截断会撞车**：实测 `12382-beyond-speedup-utilizing` 与 `12382-beyond-speedup-utilizing-2`
    截断后都是 ``12382beyonds`` —— `block.id` 是共享业务库的全局主键，于是后导入的那篇
    写库时报 `Duplicate entry '12382beyonds-p01-b01'`、IR 只写了一半（用户 2026-09-27 遇到）。
    ``taken`` 传"**其他**论文已用的前缀"时，本函数会逐步加长到不冲突为止（必要时再补一个
    由论文 id 定出来的 4 位哈希）。**只对冲突的那篇加长** —— 存量论文前缀不变，
    已写入的块 ID / 批注 / 高亮都不会被打乱。
    """
    compact = "".join(ch for ch in (paper_id or "p") if ch.isalnum()).lower()
    if not compact:
        return "p"
    if not taken:
        return compact[:12]
    for n in (12, 16, 20, 24, 32, len(compact)):
        cand = compact[:n]
        if cand not in taken:
            return cand
    digest = hashlib.sha1(compact.encode("utf-8")).hexdigest()[:4]
    cand = f"{compact[:12]}x{digest}"
    n = 0
    while cand in taken:                       # 极端情况：再按序加长，保证一定能拿到
        n += 1
        cand = f"{compact[:12]}x{digest}{n}"
    return cand


def make_block_id(prefix: str, page_no: int, index: int) -> str:
    return f"{prefix}-p{page_no:02d}-b{index:02d}"


def assign_ids_in_order(blocks: list[Block], reserve: set[str] | None = None,
                        prefix: str = "p") -> None:
    """按阅读顺序编号：``{prefix}-p{page:02d}-b{order:02d}``。

    要求 ``blocks`` 已按 ``order_global`` 排好（旧 ID 被忽略，仅用于占位）。
    传入 ``reserve`` 可保留其它已占用的 ID（如孤儿块 ``...-p07-x03``），避免冲突。
    """
    used: set[str] = set(reserve or ())
    per_page: dict[int, int] = {}
    for b in sorted(blocks, key=lambda x: (x.page_no, x.order_global)):
        idx = per_page.get(b.page_no, 0) + 1
        candidate = make_block_id(prefix, b.page_no, idx)
        while candidate in used:
            idx += 1
            candidate = make_block_id(prefix, b.page_no, idx)
        per_page[b.page_no] = idx
        used.add(candidate)
        b.id = candidate
        b.order_in_page = idx
        b.md_anchor = f"blk-{b.id}"


def write_rebind_report(path: str, report: MatchReport, paper_id: str) -> None:
    write_json(path, {"paper_id": paper_id, "report": report.to_dict(),
                      "content_hash": sha256_text(json_dumps(report.to_dict()))})
