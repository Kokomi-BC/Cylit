"""术语的**检索变体**：同一个概念在这篇论文里的另一种写法（只服务检索，不改译法）。

为什么需要它（实测，线上 turboquant 篇）：

- `TURBOQUANT_mse` 正文写作 `TURBOQUANT$_{\text{mse}}$`，用户按 `X_mse` 写就搜不到；
- `SLB` 与 `Shannon lower bound` 各回 8 条、只重叠 4 条 —— 同一概念两组各看一半。

`expand` 那一层能从「全称 (缩写)」句式里自动学一部分，但**用户自造的写法**、**从没展开过的缩写**
它学不到。那部分由这张表补齐，而且随时可删：删掉只是少几条召回，产物与译文一个字都不动。

为什么变体与译法必须分家：见 `models.TermVariant` 的 docstring（检索可以宽松，译法必须一对一）。
"""

from __future__ import annotations

import re
import time
from typing import Any, Iterable

from backend.core.logging import get_logger

log = get_logger(__name__)

#: 一条术语 / 变体的长度上限（DB 列也是 160）
MAX_LEN = 160
#: 一次最多写多少组（批量上限；超了在回执里写明截断）
MAX_ITEMS = 40
#: 进程内缓存秒数：变体表是热路径（每次词法检索都要读），30s 足够新鲜，写操作会主动清。
ALIAS_TTL = 30.0

_CACHE: dict[str, tuple[float, dict[str, tuple[str, ...]]]] = {}


def normalize(text: Any) -> str:
    """压缩空白、去掉首尾标点。**不做大小写 / 单复数归一**：

    变体是「用户写的检索写法」，替它改写等于替用户决定「这两种写法是一个东西」——
    而这个决定本来就该由写的人来做（工具回执里会把最终写法原样回显）。
    """
    body = re.sub(r"\s+", " ", str(text or "")).strip()
    return body.strip(" \t,;，；。.:：\"\'")[:MAX_LEN].strip()


def _tokens(text: str) -> list[str]:
    """按检索侧的分词口径切词（延迟导入：retrieval 会反向依赖本模块）。"""
    from backend.services.knowledge.retrieval import tokenize

    return tokenize(text)


def alias_map(rows: Iterable[dict]) -> dict[str, tuple[str, ...]]:
    """行 → 别名表（**两个方向都存**，键是 token 串，与 `expand.expand_terms` 同口径）。"""
    out: dict[str, tuple[str, ...]] = {}
    for row in rows:
        term = normalize(row.get("term"))
        variant = normalize(row.get("variant"))
        if not term or not variant or term == variant:
            continue
        left, right = _tokens(term), _tokens(variant)
        if not left or not right:
            continue
        out.setdefault(" ".join(left), tuple(right))
        out.setdefault(" ".join(right), tuple(left))
    return out


def _load(paper_id: str) -> list[dict]:
    """读某篇的全部变体（读不到就当没有：检索不该因为这张表挂掉而失败）。"""
    from backend.repositories.mysql.models import TermVariant
    from backend.repositories.mysql.session import session_scope

    try:
        with session_scope() as sess:
            rows = (sess.query(TermVariant)
                    .filter(TermVariant.paper_id == paper_id)
                    .order_by(TermVariant.created_at).all())
            return [{
                "id": str(r.id), "paper_id": r.paper_id, "term": r.term,
                "variant": r.variant, "source": r.source or "agent",
                "created_at": r.created_at.isoformat(sep=" ", timespec="seconds") if r.created_at else None,
            } for r in rows]
    except Exception as exc:  # noqa: BLE001 —— 变体表是增强，不是事实源
        log.warning("读检索变体失败（当作空表）：%s", exc)
        return []


def list_variants(paper_id: str) -> list[dict]:
    return _load(paper_id)


def aliases(paper_id: str) -> dict[str, tuple[str, ...]]:
    if not paper_id:
        return {}
    hit = _CACHE.get(paper_id)
    stamp = time.monotonic()
    if hit and stamp - hit[0] < ALIAS_TTL:
        return hit[1]
    table = alias_map(_load(paper_id))
    _CACHE[paper_id] = (stamp, table)
    return table


def invalidate(paper_id: str | None = None) -> None:
    if paper_id:
        _CACHE.pop(paper_id, None)
    else:
        _CACHE.clear()


def pairs_from(items: Iterable[dict]) -> tuple[list[dict], list[dict]]:
    """批量入参 → (要写的组, 被跳过的组)。**纯函数**，不碰库，便于离线测试。

    `items` 形如 `[{"term": "TurboQuant_mse", "variants": ["TurboQuant MSE"]}]`；
    也接受 `variant` 单数写法（模型偶尔会这么给）。
    """
    out: list[dict] = []
    skipped: list[dict] = []
    for raw in list(items or [])[:MAX_ITEMS]:
        if not isinstance(raw, dict):
            skipped.append({"input": raw, "reason": "这一条不是对象（要 {term, variants}）"})
            continue
        term = normalize(raw.get("term"))
        raw_variants = raw.get("variants")
        if raw_variants is None:
            raw_variants = raw.get("variant")
        if isinstance(raw_variants, str):
            raw_variants = [raw_variants]
        variants = [normalize(v) for v in (raw_variants or [])]
        variants = [v for v in variants if v]
        if not term:
            skipped.append({"term": term, "reason": "term 是空的"})
            continue
        if not variants:
            skipped.append({"term": term, "reason": "没有给 variants"})
            continue
        for variant in variants:
            if variant == term:
                skipped.append({"term": term, "variant": variant, "reason": "与 term 相同"})
                continue
            out.append({"term": term, "variant": variant})
    return out, skipped


def add(paper_id: str, items: Iterable[dict], source: str = "agent") -> dict:
    """批量加变体；已存在的（同一 term+variant）不重复写，回执里单列。"""
    from backend.core.utils import new_id
    from backend.repositories.mysql.models import TermVariant
    from backend.repositories.mysql.session import session_scope

    wanted, skipped = pairs_from(items)
    have = {(r["term"], r["variant"]) for r in _load(paper_id)}
    added: list[dict] = []
    with session_scope() as sess:
        for pair in wanted:
            key = (pair["term"], pair["variant"])
            if key in have:
                skipped.append({"term": pair["term"], "variant": pair["variant"],
                                "reason": "已经有了"})
                continue
            row = TermVariant(id=new_id("tv"), paper_id=paper_id,
                              term=pair["term"], variant=pair["variant"], source=source)
            sess.add(row)
            have.add(key)
            added.append({"id": row.id, "term": row.term, "variant": row.variant})
    invalidate(paper_id)
    return {"added": added, "skipped": skipped}


def remove(paper_id: str, ids: Iterable[str] | None = None,
           terms: Iterable[str] | None = None) -> dict:
    """批量删变体：给 `ids` 删那几条，给 `terms` 删这些术语的全部变体。"""
    from backend.repositories.mysql.models import TermVariant
    from backend.repositories.mysql.session import session_scope

    id_list = [str(i).strip() for i in (ids or []) if str(i).strip()]
    term_list = [normalize(t) for t in (terms or []) if normalize(t)]
    gone: list[dict] = []
    if id_list or term_list:
        with session_scope() as sess:
            query = sess.query(TermVariant).filter(TermVariant.paper_id == paper_id)
            if id_list and term_list:
                from sqlalchemy import or_
                query = query.filter(or_(TermVariant.id.in_(id_list),
                                         TermVariant.term.in_(term_list)))
            elif id_list:
                query = query.filter(TermVariant.id.in_(id_list))
            else:
                query = query.filter(TermVariant.term.in_(term_list))
            for row in query.all():
                gone.append({"id": str(row.id), "term": row.term, "variant": row.variant})
                sess.delete(row)
        invalidate(paper_id)
    return {"removed": gone, "requested": {"ids": id_list, "terms": term_list}}