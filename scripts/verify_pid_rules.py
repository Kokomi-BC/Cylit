"""行身份（pid）规则的一键验证：一条命令跑完"规则是否真有效"，全程只读。

为什么要它：``row_identity``（pid 注册表）、chunk / 解读里的 ``pids``、读路径
``understanding_service.split_refs_by_pids`` 都已上线，但"它们是否真的让引用在重建后认回
同一行"此前没有一处能拿**真实数据**给出证据 —— ``scripts/index_audit.py`` 判级按"存量数据
是常态"（老索引只 warn），单测喂的是构造数据。本脚本补的就是那份证据：11 条检查，每条打印
``[PASS]/[FAIL] 名称 —— 证据``，任意 FAIL → 退出码 1。

用法::

    python scripts/verify_pid_rules.py                 # 库里所有有解读的论文
    python scripts/verify_pid_rules.py <paper_id> ...   # 只验这些论文
    python scripts/verify_pid_rules.py --json           # 机器可读（stdout 只出 JSON）

退出码：有 FAIL → 1；全 PASS → 0；读不到论文列表（库连不上）→ 2。

## 11 条检查（FAIL 条件写在括号里）

A. 不变量（真实数据）

- **A1 行 pid 覆盖率与唯一性** —— 每行都有 ``pid``、全局无重复（读不到任何行 = FAIL）。
- **A2 注册表与行一一对应** —— 注册表 ``rows`` 的每条都能在它 (页, 序位) 上找到行
  （找不到 = FAIL）；``retired`` 里的 pid 不出现在活行上（出现 = FAIL，那是身份被重绑）；
  行上的 pid 也必须在注册表里（孤儿 = FAIL）。
- **A3 chunk 的 metadata.pids 同序等长** —— 带该字段的 chunk 必须
  ``len(pids) + pids_missing == len(row_ids)``（不等 = FAIL），每条 pid 都要在注册表里
  （找不到 = FAIL）。整个字段缺失只按老索引记进证据，不算 FAIL（重建索引后才有）。
- **A4 解读短码的行身份覆盖率** —— 正文里出现的每个短码都要有行身份
  （``refs_json.pids``，或所在 chunk 的 ``metadata.pids``），覆盖率不足 100% = FAIL；
  那些身份 ``resolve_pids`` 必须全部 ``ok``（text-changed / gone = FAIL）。

B. 故障注入（**在内存里伪造，不改库**）—— 规则真有效的核心证据

- **B5 单条 chunk id 打坏仍精确定位** —— 逐条把 ``resolved[code]`` 换成
  ``bogus:chunk:<code>``：那一条必须 ``healed`` 且 ``row_id/page`` 与打坏前一致
  （不一致 / 不见了 / 未自愈 = FAIL），同路其它引用不得被推成过期（变了 = FAIL）。
  一条带行身份的引用都没有 = FAIL（无从证明）。
- **B6 整篇 chunk id 全打坏仍精确** —— 一次性全部替换，"still precise" 比例必须 100%
  （< 100% 或样本 0 = FAIL）。
- **B7 pid 失效时降级不乱跳** —— 把某条 code 的 pids 换成 ``r-deadbeefdeadbeef``：必须进
  ``stale_refs`` 或 ``pid_state == gone``（标成 healed / 未降级 = FAIL），且落点不得漂移
  （给了与打坏前不同的 ``row_id`` = FAIL：宁可不跳，也不乱跳）。
- **B8 换成别人的 pid 就落到那一行** —— 把某条 code 的 pids 换成另一行真实存在的 pid：
  结果 ``row_id/page`` 必须等于那个 pid 真正对应的行（不等 = FAIL），证据里写出两个行 id。

C. 确定性 / 幂等

- **C9 连读两次的确定性与注册表不变** —— 两次 ``page_products`` 的 (行 id → pid) 映射必须
  相同（不同 = FAIL），注册表文件 ``mtime`` / 字节数 / sha256 必须没被动过（变了 = FAIL）。
- **C10 backfill dry-run 第二次可补 0** —— 导入 ``scripts/backfill_ref_pids.py`` 的**纯函数**
  规划两次（第一次的计划在内存里套进 refs，不写库）：第二次"可补"必须为 0（> 0 = FAIL）。

D. 与改造前行为的等价性

- **D11 与 split_live_refs 逐字段等价** —— 对"chunk 还在、pid 也对（healed）"的条目，
  ``split_live_refs`` 给出的**每个字段**都必须被 healed 项原样保留（少一个 / 值不同 = FAIL，
  例：丢了 ``chunk_id`` / ``block_id`` / ``latex``）；healed 项新增的键（``pids`` /
  ``healed`` / ``pid_state``）不算差异。对比条数为 0 = FAIL（没有样本就谈不上等价）。

## 边界

- **只读**：不写 DB、不改 ``data/`` 的产物。唯一允许的写是 ``page_products`` 自己的幂等
  注册表写入（本脚本只调现成的读入口，从不调 ``row_identity.assign`` / ``save``）；
  不跑解析 / 翻译 / 建索引 / embedding。
- 判定逻辑全是模块级**纯函数**（入参是普通 dict 列表），IO 只在 ``_load_*`` 里 ——
  单测喂构造数据即可（``tests/test_verify_pid_rules.py``）。
- 口径不另写一套：页产物走 ``reader_service.page_products``，行摊平走
  ``understanding_service._page_rows``，pid 判定走 ``row_identity.resolve_registry``，
  身份取值走 ``understanding_service._ref_pids``，每条 code 的 chunk 锚点走
  ``understanding_service._chunk_anchors``（读路径自己那一个查询）。
"""

from __future__ import annotations

import argparse
import hashlib
import json
import logging
import re
import sys
from collections import Counter
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

#: 正文里的短码（与 ``backfill_ref_pids._CODE_RE`` / ``index_audit._CODE_RE`` 同一口径）
_CODE_RE = re.compile(r"\b(A\d{2,4})\b")
#: 注入用的死 pid：格式合法（``r-<16 位十六进制>``）、注册表里必然没有
DEAD_PID = "r-deadbeefdeadbeef"
#: 打坏 chunk id 用的前缀（库里不可能存在这种 id）
BOGUS_PREFIX = "bogus:chunk:"
#: 一条证据里最多列几项（其余只给数量）
SAMPLE_MAX = 3
#: D11 的等价性对比里**允许**的差异：新增字段（healed 项多出来的那份）
_IGNORED_FIELDS = ("healed", "pid_state")

#: 检查 id → 名称（输出与 --json 共用；顺序即打印顺序）
CHECK_NAMES = {
    "A1": "行 pid 覆盖率与唯一性",
    "A2": "注册表与行一一对应",
    "A3": "chunk 的 metadata.pids 同序等长",
    "A4": "解读短码的行身份覆盖率",
    "B5": "单条 chunk id 打坏仍精确定位",
    "B6": "整篇 chunk id 全打坏仍精确",
    "B7": "pid 失效时降级不乱跳",
    "B8": "换成别人的 pid 就落到那一行",
    "C9": "连读两次的确定性与注册表不变",
    "C10": "backfill dry-run 第二次可补 0",
    "D11": "与 split_live_refs 逐字段等价",
}


# --------------------------------------------------------------------------
# 小工具（纯函数）
# --------------------------------------------------------------------------


def _clip(text: object, limit: int = 400) -> str:
    """压成一行（证据里不放换行与整段内容）。"""
    return " ".join(str(text if text is not None else "").split())[:limit]


def _sample(items, limit: int = SAMPLE_MAX) -> str:
    """列表 → 摘要串：前 ``limit`` 项 + 省略数量。"""
    seq = [str(x) for x in list(items)]
    shown = seq[:limit]
    rest = len(seq) - len(shown)
    return ", ".join(shown) + (f" …(+{rest})" if rest > 0 else "")


def result(check: str, ok: bool, evidence: str) -> dict:
    """一条检查的结果：``{"check","name","ok","evidence"}``（FAIL 时证据必须说清是什么坏了）。"""
    return {"check": check, "name": CHECK_NAMES.get(check, check),
            "ok": bool(ok), "evidence": _clip(evidence)}


def _index(items) -> dict[str, dict]:
    """引用项列表 → ``{code: 项}``（同一条 code 只可能有一条）。"""
    return {str(it.get("code")): it for it in items or []
            if isinstance(it, dict) and it.get("code")}


def _int_or(value, default: int = 0) -> int:
    """尽量转 int（``pids_missing`` 坏掉时不猜，当 0）。"""
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def pid_map(rows: list[dict]) -> dict[str, str]:
    """行列表 → ``{行 id: pid}``（只收有 pid 的行）—— C9 比较的对象。"""
    return {str(r.get("id")): str(r.get("pid")) for r in rows or []
            if isinstance(r, dict) and r.get("id")
            and str(r.get("pid") or "").strip()}


def identities_of(resolved: dict, by_code: dict, anchors: dict,
                  refs_pids: dict) -> dict[str, list[str]]:
    """每条 code 的**行身份**：``{code: pids}``。

    取值口径**复用读路径** ``understanding_service._ref_pids``（``refs_json.pids`` 优先，
    退现存 chunk 的 ``metadata.pids``）—— 注入检查要坏的就是这一份，别处再写一遍就会
    验到一个读路径根本不用的身份。
    """
    from backend.services.understanding import understanding_service as us

    out: dict[str, list[str]] = {}
    for code, chunk_id in (resolved or {}).items():
        pids = us._ref_pids(refs_pids or {}, code, chunk_id, anchors or {})
        if pids:
            out[str(code)] = pids
    return out


def chunk_pid_map(chunks: list[dict]) -> dict[str, list[str]]:
    """``{chunk id: metadata.pids}``（没有 pids 的 chunk 不进这张表）。"""
    out: dict[str, list[str]] = {}
    for chunk in chunks or []:
        if not isinstance(chunk, dict):
            continue
        pids = [str(p) for p in ((chunk.get("metadata") or {}).get("pids") or []) if p]
        if pids:
            out[str(chunk.get("id") or "")] = pids
    return out


def file_stat(path) -> dict:
    """文件的三样指纹（``mtime_ns`` / 字节数 / sha256）；读不到就给空 dict。"""
    try:
        data = Path(path).read_bytes()
    except OSError:
        return {}
    try:
        mtime = Path(path).stat().st_mtime_ns
    except OSError:
        mtime = 0
    return {"mtime_ns": mtime, "size": len(data),
            "sha256": hashlib.sha256(data).hexdigest()}


# --------------------------------------------------------------------------
# A 不变量（真实数据，纯函数）
# --------------------------------------------------------------------------


def check_pid_coverage(rows: list[dict]) -> dict:
    """A1 —— 每行都有 pid，且 pid 全局唯一。

    读不到任何行 = FAIL（覆盖率无从谈起）；有行没 pid = FAIL；同一个 pid 落在多条行上
    = FAIL（那是"两条行共用一个身份"，引用必然指错）。
    """
    body = [r for r in (rows or []) if isinstance(r, dict)]
    if not body:
        return result("A1", False, "读不到任何正文行（未翻译？），pid 覆盖率无从校验")
    with_pid = [str(r.get("pid")) for r in body if str(r.get("pid") or "").strip()]
    missing = [str(r.get("id") or "∅") for r in body if not str(r.get("pid") or "").strip()]
    dup = {pid: n for pid, n in Counter(with_pid).items() if n > 1}
    if missing or dup:
        parts = []
        if missing:
            parts.append(f"{len(missing)}/{len(body)} 个行没有 pid: {_sample(missing)}")
        if dup:
            parts.append(f"{len(dup)} 个 pid 重复: {_sample(sorted(dup))}")
        return result("A1", False, "；".join(parts))
    return result("A1", True,
                  f"行 {len(with_pid)}/{len(body)} 都有 pid，唯一 pid {len(set(with_pid))} 个"
                  f"（重复 0、缺 0）")


def check_registry_rows(rows: list[dict], registry: dict) -> dict:
    """A2 —— 注册表与行一一对应。

    FAIL 的三种情形：注册表 ``rows`` 里某条的身份在它的 (页, 序位) 上找不到行
    （slot 消失，state=gone）；``retired`` 里的 pid 又出现在活行上（身份被重绑，最严重的错）；
    活行上的 pid 不在注册表里（孤儿）。``text-changed`` 不算错 —— 位子还在、文本变了，
    那正是 pid 存在的意义。
    """
    from backend.services.paper import row_identity

    reg = registry if isinstance(registry, dict) else {}
    entries = [e for e in (reg.get("rows") or []) if isinstance(e, dict) and e.get("pid")]
    retired = [str(e.get("pid")) for e in (reg.get("retired") or [])
               if isinstance(e, dict) and e.get("pid")]
    on_rows = {str(r.get("pid")) for r in rows or []
               if isinstance(r, dict) and str(r.get("pid") or "").strip()}
    verdict = row_identity.resolve_registry(reg, [str(e["pid"]) for e in entries], rows)
    no_slot = sorted(pid for pid, hit in verdict.items() if hit.get("state") == "gone")
    revived = sorted(set(retired) & on_rows)
    known = {str(e["pid"]) for e in entries}
    orphan = sorted(on_rows - known - set(retired))
    if no_slot or revived or orphan:
        parts = []
        if no_slot:
            parts.append(f"{len(no_slot)} 个注册表身份在当前页产物里找不到行: {_sample(no_slot)}")
        if revived:
            parts.append(f"{len(revived)} 个已退休的 pid 又出现在活行上: {_sample(revived)}")
        if orphan:
            parts.append(f"{len(orphan)} 个活行的 pid 不在注册表里: {_sample(orphan)}")
        return result("A2", False, "；".join(parts))
    if not entries:
        return result("A2", True, f"注册表为空、活行 {len(on_rows)} 个也都没有 pid —— 无事可判")
    changed = sum(1 for hit in verdict.values() if hit.get("state") == "text-changed")
    return result("A2", True,
                  f"注册表 {len(entries)} 条 rows 条目逐条落在 (页, 序位) 对应的行上"
                  f"（其中 text-changed {changed} 条：位子还在、文本变了，不算错）；"
                  f"retired {len(retired)} 条无一在活行上；活行 {len(on_rows)} 个都在注册表里")


def check_chunk_pids(chunks: list[dict], registry: dict) -> dict:
    """A3 —— chunk 的 ``metadata.pids`` 与 ``row_ids`` 同序等长（``pids_missing`` 计入），
    且每条都在注册表里。

    整个字段缺失（老索引，建块时还没有行身份）不算 FAIL，只记进证据 —— 判级与
    ``index_audit.check_chunk_pids`` 同口径（那边是 warn），重建索引后字段才会带上。
    长度不等 = FAIL；pid 在注册表里找不到 = FAIL（chunk 引用了不存在的身份）。
    注册表为空时"找不到"没有基准（还没生成过），那时只做长度检查 —— 否则老库会整片假红。
    """
    reg = registry if isinstance(registry, dict) else {}
    known = {str(e.get("pid")) for e in (reg.get("rows") or [])
             if isinstance(e, dict) and e.get("pid")}
    items = [c for c in (chunks or []) if isinstance(c, dict)]
    with_field = 0
    length_bad: list[str] = []
    unknown: list[tuple] = []
    partial: list[str] = []
    for chunk in items:
        cid = str(chunk.get("id") or "∅")
        meta = chunk.get("metadata") or {}
        pids = meta.get("pids")
        if pids is None:
            continue
        with_field += 1
        if not isinstance(pids, list):
            length_bad.append(f"{cid}(pids 不是列表)")
            continue
        pids = [str(x) for x in pids]
        row_ids = [str(x) for x in (meta.get("row_ids") or [])]
        missing = max(_int_or(meta.get("pids_missing"), 0), 0)
        if len(pids) + missing != len(row_ids):
            length_bad.append(f"{cid}(pids {len(pids)} + 缺 {missing} ≠ row_ids {len(row_ids)})")
        if known:
            unknown += [(pid, cid) for pid in pids if pid not in known]
        if missing:
            partial.append(cid)
    if length_bad or unknown:
        parts = []
        if length_bad:
            parts.append(f"{len(length_bad)} 条 chunk 的 pids 与 row_ids 不是同序等长: "
                         f"{_sample(length_bad)}")
        if unknown:
            parts.append(f"{len(unknown)} 个 chunk pid 在注册表里找不到: "
                         f"{_sample(f'{pid}(@{cid})' for pid, cid in unknown)}")
        return result("A3", False, "；".join(parts))
    if not items:
        return result("A3", True, "该论文没有 chunk（未建索引）—— 无事可判")
    spare = len(items) - with_field
    return result("A3", True,
                  f"带 metadata.pids 的 chunk {with_field}/{len(items)} 条：同序等长全部成立"
                  f"（pids_missing>0 的 {len(partial)} 条已按口径计入），每条 pid 都在注册表里"
                  + (f"；其余 {spare} 条没有该字段（老索引，重建后才有；长度与注册表这两条规则"
                     f"在它们身上无从检验）" if spare else ""))


def _identity_pids(code: str, resolved: dict, by_code: dict,
                   chunk_pids: dict, refs_pids: dict) -> list[str]:
    """一条 code 能拿到的行身份：``refs_json.pids`` 优先 → ``resolved`` 那条 chunk →
    ``inventory`` 那条 chunk（与 ``index_audit.ref_pid_coverage`` 同口径）。"""
    stored = (refs_pids or {}).get(code)
    pids = [str(p) for p in stored if p] if isinstance(stored, list) else []
    if pids:
        return pids
    for cid in ((resolved or {}).get(code), (by_code.get(code) or {}).get("chunk_id")):
        pids = list((chunk_pids or {}).get(str(cid or "").strip()) or [])
        if pids:
            return pids
    return []


def check_ref_coverage(rows: list[dict], registry: dict, chunk_pids: dict,
                       paths: list[dict]) -> dict:
    """A4 —— 正文里出现的每个短码都有行身份，且那些身份全部 ``ok``。

    覆盖率不足 100% = FAIL（缺的那条引用只能按旧 chunk id 认，重建索引后即失效；
    补法是 ``scripts/backfill_ref_pids.py``，不重建索引）。有身份但
    ``resolve_registry`` 给出 ``text-changed`` / ``gone`` = FAIL（身份认不回原行）。
    """
    from backend.services.paper import row_identity

    total = 0
    gaps: list[str] = []
    unregistered: list[str] = []
    bad_state: list[str] = []
    for payload in paths or []:
        resolved = payload.get("resolved") or {}
        by_code = payload.get("by_code") or {}
        saved = payload.get("refs_pids") or {}
        mark = f"[{payload.get('path')}]"
        for code in dict.fromkeys(_CODE_RE.findall(payload.get("markdown") or "")):
            total += 1
            pids = _identity_pids(code, resolved, by_code, chunk_pids, saved)
            if not pids:
                (gaps if (code in resolved or code in by_code) else unregistered).append(
                    f"{mark}{code}")
                continue
            verdict = row_identity.resolve_registry(
                registry if isinstance(registry, dict) else {}, pids, rows)
            for pid in pids:
                state = str((verdict.get(pid) or {}).get("state") or "gone")
                if state != "ok":
                    bad_state.append(f"{mark}{code}/{pid}:{state}")
    if not total:
        return result("A4", True, "正文里没有任何短码（无从引用）—— 无事可判")
    if gaps or unregistered or bad_state:
        parts = []
        if gaps or unregistered:
            miss = len(gaps) + len(unregistered)
            parts.append(f"行身份覆盖 {total - miss}/{total} 条短码（缺 {miss}）: "
                         f"{_sample(gaps + unregistered)}"
                         f"（补法：python scripts/backfill_ref_pids.py <paper_id>）")
        if bad_state:
            parts.append(f"{len(bad_state)} 条身份认不回原行: {_sample(bad_state)}")
        return result("A4", False, "；".join(parts))
    return result("A4", True,
                  f"正文短码 {total}/{total} 条都有行身份，且 {total} 条的身份解析全部 ok"
                  f"（{len(paths or [])} 路）")


# --------------------------------------------------------------------------
# B 故障注入（在内存里伪造 resolved / refs_pids，不碰库）
# --------------------------------------------------------------------------


def check_heal_single(split, paths: list[dict]) -> dict:
    """B5 —— 逐条把 chunk id 打坏：那一条必须靠 pid 认回**同一行**（healed）。

    ``split(payload, refs_pids)`` 是读路径的注入点（默认实现见 ``make_splitter``）。
    FAIL：这条引用消失 / 没标 healed / ``row_id`` 或 ``page`` 变了 / 打坏这一条把同路
    别的引用推成了过期（说明自愈是连带副作用而不是精确定位）。样本 0 = FAIL。
    """
    samples = 0
    bad: list[str] = []
    for payload in paths or []:
        mark = f"[{payload.get('path')}]"
        base_live, base_stale = split(payload, payload.get("refs_pids"))
        base = _index(base_live)
        want_stale = {it.get("code") for it in base_stale or []}
        idents = identities_of(payload.get("resolved") or {}, payload.get("by_code") or {},
                               payload.get("anchors") or {}, payload.get("refs_pids") or {})
        for code in idents:
            broken = dict(payload.get("resolved") or {})
            broken[code] = BOGUS_PREFIX + code
            live, stale = split({**payload, "resolved": broken}, payload.get("refs_pids"))
            got = _index(live).get(code)
            before = base.get(code) or {}
            samples += 1
            if got is None:
                bad.append(f"{mark}{code}: 打坏 chunk id 后这条引用不见了")
            elif not got.get("healed"):
                bad.append(f"{mark}{code}: 未自愈（healed 缺失，仍按 chunk id 落地）")
            elif (got.get("row_id"), got.get("page")) != (before.get("row_id"),
                                                          before.get("page")):
                bad.append(f"{mark}{code}: 落点变了 {before.get('row_id')}/p{before.get('page')}"
                           f" → {got.get('row_id')}/p{got.get('page')}")
            elif {it.get("code") for it in stale or []} != want_stale:
                bad.append(f"{mark}{code}: 打坏这一条把别的引用推成了过期"
                           f"（stale {len(stale or [])} 条 ≠ 原有 {len(want_stale)} 条）")
    if samples == 0:
        return result("B5", False,
                      "样本 0：没有任何带行身份的解读引用，自愈无从证明"
                      "（先跑 python scripts/backfill_ref_pids.py <paper_id>）")
    if bad:
        return result("B5", False, f"{len(bad)}/{samples} 条未自愈: {_sample(bad)}")
    return result("B5", True,
                  f"逐条打坏 {samples} 条带身份引用的 chunk id：每条仍落在同一 row_id/page"
                  f"（healed），同路其它引用一条都没被推成过期")


def check_heal_all(split, paths: list[dict]) -> dict:
    """B6 —— 整篇 chunk id 一次性全打坏：still precise 比例必须 100%。

    "精确" = 那一条仍 ``healed`` 且 ``row_id`` / ``page`` 与打坏前一致（不是"没报错"）。
    样本 0 = FAIL（没有带身份的引用就没有可证明的东西）。
    """
    total = precise = 0
    stale_total = 0
    bad: list[str] = []
    for payload in paths or []:
        mark = f"[{payload.get('path')}]"
        idents = identities_of(payload.get("resolved") or {}, payload.get("by_code") or {},
                               payload.get("anchors") or {}, payload.get("refs_pids") or {})
        if not idents:
            continue
        base = _index(split(payload, payload.get("refs_pids"))[0])
        broken = {code: BOGUS_PREFIX + code for code in (payload.get("resolved") or {})}
        live, stale = split({**payload, "resolved": broken}, payload.get("refs_pids"))
        got = _index(live)
        stale_total += len(stale or [])
        for code in idents:
            total += 1
            before = base.get(code) or {}
            item = got.get(code) or {}
            if item.get("healed") and (item.get("row_id"), item.get("page")) == (
                    before.get("row_id"), before.get("page")):
                precise += 1
            else:
                bad.append(f"{mark}{code}: {before.get('row_id')}/p{before.get('page')}"
                           f" → {item.get('row_id')}/p{item.get('page')}"
                           f"{'（healed 缺失）' if not item.get('healed') else ''}")
    if total == 0:
        return result("B6", False,
                      "样本 0：没有任何带行身份的解读引用，still precise 比例无从统计")
    ratio = precise / total
    evidence = (f"整篇 chunk id 全部打坏后 {precise}/{total} 条带身份引用仍精确"
                f"（{ratio:.0%}）；另有 {stale_total} 条进了 stale_refs（本来就没有行身份的 code）")
    if bad:
        return result("B6", False, f"{evidence}；不精确 {len(bad)} 条: {_sample(bad)}")
    return result("B6", True, evidence)


def check_dead_pid(split, paths: list[dict]) -> dict:
    """B7 —— pid 指向"这段真的被删了"：必须降级（stale / ``pid_state == gone``），且不乱跳。

    FAIL：这条引用消失 / 标成 healed（拿一个死身份认了某一行）/ 该降级却没降级 /
    落点漂移（给了与打坏前不同的 ``row_id`` —— "宁可不跳，也不乱跳"）。样本 0 = FAIL。
    """
    samples = 0
    bad: list[str] = []
    for payload in paths or []:
        mark = f"[{payload.get('path')}]"
        base = _index(split(payload, payload.get("refs_pids"))[0])
        idents = identities_of(payload.get("resolved") or {}, payload.get("by_code") or {},
                               payload.get("anchors") or {}, payload.get("refs_pids") or {})
        for code in idents:
            dead = dict(payload.get("refs_pids") or {})
            dead[code] = [DEAD_PID]
            live, stale = split(payload, dead)
            item_live = _index(live).get(code)
            item_stale = _index(stale).get(code)
            before = base.get(code) or {}
            samples += 1
            item = item_live or item_stale
            if item is None:
                bad.append(f"{mark}{code}: 换成死 pid 后这条引用不见了")
            elif item.get("healed"):
                bad.append(f"{mark}{code}: 死 pid 也标成 healed（会乱跳）")
            elif item_live is not None and item_live.get("pid_state") != "gone":
                bad.append(f"{mark}{code}: 未降级（pid_state={item_live.get('pid_state')!r}，"
                           f"应进 stale_refs 或标 gone）")
            elif item_live is not None and item_live.get("row_id") and \
                    before.get("row_id") and item_live["row_id"] != before["row_id"]:
                bad.append(f"{mark}{code}: 落点漂移 {before['row_id']} → {item_live['row_id']}")
    if samples == 0:
        return result("B7", False, "样本 0：没有任何带行身份的解读引用，降级行为无从注入")
    if bad:
        return result("B7", False, f"{len(bad)}/{samples} 条未按预期降级: {_sample(bad)}")
    return result("B7", True,
                  f"{samples} 条逐条换成死 pid（{DEAD_PID}）：全部进 stale_refs 或标"
                  f" pid_state=gone、无一标 healed、落点无一漂移")


def check_foreign_pid(split, rows: list[dict], registry: dict,
                      paths: list[dict]) -> dict:
    """B8 —— 把某条 code 的 pids 换成**另一行**真实存在的 pid：落点必须跟着那个 pid 走。

    "pid 指向哪就落哪"是可预测的：结果 ``row_id`` / ``page`` 必须等于该 pid 在注册表里
    真正对应的行（不是原来那一条的行）。FAIL：落到了别的行 / healed 缺失 / 没有可变样本。
    """
    from backend.services.paper import row_identity

    samples = 0
    bad: list[str] = []
    shown: list[str] = []
    for payload in paths or []:
        mark = f"[{payload.get('path')}]"
        base = _index(split(payload, payload.get("refs_pids"))[0])
        idents = identities_of(payload.get("resolved") or {}, payload.get("by_code") or {},
                               payload.get("anchors") or {}, payload.get("refs_pids") or {})
        if not idents:
            continue
        all_pids = sorted({pid for pids in idents.values() for pid in pids})
        verdict = row_identity.resolve_registry(registry if isinstance(registry, dict) else {},
                                                all_pids, rows)
        for code in idents:
            own = str((base.get(code) or {}).get("row_id") or "")
            target = next((pid for pid in all_pids
                           if str((verdict.get(pid) or {}).get("row_id") or "")
                           and str((verdict.get(pid) or {}).get("row_id")) != own), "")
            if not target:
                continue            # 这篇只有一行可落，换不出"另一行"
            want = verdict.get(target) or {}
            swapped = dict(payload.get("refs_pids") or {})
            swapped[code] = [target]
            item = _index(split(payload, swapped)[0]).get(code) or {}
            samples += 1
            if (item.get("row_id"), item.get("page")) != (want.get("row_id"),
                                                           want.get("page")):
                bad.append(f"{mark}{code}: 换成 {target} 后落在 {item.get('row_id')}"
                           f"/p{item.get('page')}，而该 pid 对应 {want.get('row_id')}"
                           f"/p{want.get('page')}")
            elif len(shown) < 2:
                shown.append(f"{mark}{code}: {own} → {want.get('row_id')}（pid {target}）")
    if samples == 0:
        return result("B8", False,
                      "样本 0：没有可换成'另一行的 pid'的引用（带身份的引用不足两条，"
                      "或都落在同一行）")
    if bad:
        return result("B8", False, f"{len(bad)}/{samples} 条没跟着 pid 走: {_sample(bad)}")
    return result("B8", True,
                  f"{samples} 条逐条换成另一行的 pid：落点全部跟着 pid 走（可预测）；"
                  f"样例 {' | '.join(shown)}")


# --------------------------------------------------------------------------
# C 确定性 / 幂等
# --------------------------------------------------------------------------


def compare_pid_maps(first: dict, second: dict) -> dict:
    """两次 (行 id → pid) 映射比较：``{"rows","changed"}``（changed 为空 = 完全一致）。"""
    keys = set(first or {}) | set(second or {})
    changed = sorted(k for k in keys if (first or {}).get(k) != (second or {}).get(k))
    return {"rows": len(first or {}), "changed": changed}


def check_read_stability(first: dict, second: dict, stat_a: dict, stat_b: dict) -> dict:
    """C9 —— 连读两次 ``page_products``：(行 id → pid) 不变，注册表文件没被动过。

    FAIL：映射有任何一条变了（行 id 或 pid 抖动 = 引用会断）；注册表 ``mtime`` / 字节数 /
    sha256 变了（说明这次读重写了注册表 —— 幂等被破坏）。
    """
    diff = compare_pid_maps(first, second)
    moved = [key for key in ("mtime_ns", "size", "sha256")
             if (stat_a or {}).get(key) != (stat_b or {}).get(key)]
    if not first and not second:
        return result("C9", False, "两次读都没拿到任何行（未翻译？），确定性无从校验")
    if diff["changed"] or moved:
        parts = []
        if diff["changed"]:
            parts.append(f"{len(diff['changed'])} 个行的 pid 变了: {_sample(diff['changed'])}")
        if moved:
            parts.append(f"注册表文件被改动（{', '.join(moved)} 变了）")
        return result("C9", False, "；".join(parts))
    return result("C9", True,
                  f"两次读 (行→pid) 映射完全一致（{diff['rows']} 行）；注册表未改动"
                  f"（{stat_a.get('size')} 字节，sha256 {str(stat_a.get('sha256') or '')[:12]}…）")


def check_backfill_idempotent(paper_id: str, payloads: list[dict], chunk_pids: dict,
                              capture_doc: dict | None) -> dict:
    """C10 —— 连跑两次 dry-run 的**规划**：第二次"可补"必须为 0。

    只导入 ``scripts/backfill_ref_pids.py`` 的纯函数（``plan_backfill`` / ``backfilled_refs``）：
    第一次的计划在内存里套进 ``refs``（等价 ``apply_plan_to_rows`` 的纯函数部分，不写库），
    再规划一次。FAIL：第二次仍有可补的 code（幂等破了，跑一次补不完）。
    """
    from scripts import backfill_ref_pids as bf

    first = bf.plan_backfill(paper_id, payloads or [], chunk_pids or {}, capture_doc)
    updated: list[dict] = []
    for payload in payloads or []:
        summary = (first.get("paths") or {}).get(str(payload.get("path") or "")) or {}
        refs, _filled = bf.backfilled_refs(payload.get("refs") or {},
                                           summary.get("items") or [])
        updated.append({**payload, "refs": refs})
    second = bf.plan_backfill(paper_id, updated, chunk_pids or {}, capture_doc)
    left = [f"[{path}]{it['code']}"
            for path, summary in sorted((second.get("paths") or {}).items())
            for it in summary.get("items") or [] if it.get("action") == "fill"]
    evidence = (f"第一次可补 {first.get('fillable', 0)} 条 / 已有 {first.get('existing', 0)} 条 / "
                f"跳过 {len(first.get('skipped') or [])} 条；把计划套进内存后第二次可补 "
                f"{len(left)} 条")
    if left:
        return result("C10", False, f"{evidence}（不幂等）: {_sample(left)}")
    return result("C10", True, evidence)


# --------------------------------------------------------------------------
# D 与改造前行为的等价性
# --------------------------------------------------------------------------


def check_legacy_equivalence(split, paths: list[dict], note: str = "") -> dict:
    """D11 —— chunk 还在、pid 也对时，healed 项必须保留旧逻辑（``split_live_refs``）
    给出的**每个字段**。

    比较口径：对每条 healed 引用，旧项里出现的每个键在 healed 项里必须同值
    （``healed`` / ``pid_state`` 是新增的，比较时跳过；healed 项**多出来**的键如 ``pids``
    不算差异 —— 那是这次改造的目的）。FAIL：丢了字段或值不同（典型是 ``chunk_id`` /
    ``block_id`` / ``latex`` 被清空，前端就丢精度）。对比条数 0 = FAIL。
    """
    from backend.services.understanding import understanding_service as us

    compared = 0
    lost: dict[str, dict] = {}
    added: set[str] = set()
    for payload in paths or []:
        healed_live, _healed_stale = split(payload, payload.get("refs_pids"))
        anchors = payload.get("anchors") or {}
        old_live, _old_stale = us.split_live_refs(
            payload.get("resolved") or {}, payload.get("by_code") or {},
            exists=lambda ids, live=anchors: set(live) & set(ids), anchors=anchors)
        old = _index(old_live)
        for code, item in _index(healed_live).items():
            legacy = old.get(code)
            if legacy is None or not item.get("healed"):
                continue              # 只比"chunk 还在 + pid 也对"的条目（spec 的口径）
            compared += 1
            diff = {key: (value, item.get(key)) for key, value in legacy.items()
                    if key not in _IGNORED_FIELDS and item.get(key) != value}
            if diff:
                lost[f"[{payload.get('path')}]{code}"] = diff
            added |= {key for key in item
                      if key not in legacy and key not in _IGNORED_FIELDS}
    if compared == 0:
        return result("D11", False,
                      "对比条数 0：没有'chunk 还在 + pid 也对'的引用，等价性无从检验")
    extra = f"；{note}" if note else ""
    if lost:
        per_key = Counter(key for diff in lost.values() for key in diff)
        sample = next(iter(lost.items()))
        detail = "，".join(f"{key} {old_value!r} → {new_value!r}"
                          for key, (old_value, new_value) in list(sample[1].items())[:3])
        return result("D11", False,
                      f"对比 {compared} 条，{len(lost)} 条没保留旧逻辑的字段"
                      f"（{dict(per_key)}）；样例 {sample[0]}: {detail}"
                      + (f"；新增字段 {sorted(added)}" if added else "") + extra)
    return result("D11", True,
                  f"对比 {compared} 条（chunk 还在 + pid 全部 ok）：旧逻辑的每个字段都被原样保留"
                  + (f"；healed 项只新增 {sorted(added)}" if added else ""))


# --------------------------------------------------------------------------
# 装配：一篇论文的 11 条检查
# --------------------------------------------------------------------------


def make_splitter(paper_id: str, rows: list[dict] | None, registry: dict):
    """读路径的注入点：``split(payload, refs_pids) -> (live, stale)``。

    ``rows`` / ``registry`` 直接注入（本篇只读一次；单测也免得碰真实 ``data/``），
    落的实现仍是 ``understanding_service.split_refs_by_pids`` —— 注入检查验的就是它。
    """
    from backend.services.understanding import understanding_service as us

    def split(payload: dict, refs_pids: dict | None):
        return us.split_refs_by_pids(
            paper_id, payload.get("resolved") or {}, payload.get("by_code") or {},
            payload.get("anchors") or {}, refs_pids=refs_pids or {},
            rows_of=lambda _paper: rows, registry_of=lambda _paper: registry)

    return split


def _chunk_pid_note(chunks: list[dict]) -> str:
    """D11 失败时的诊断线索：chunk 表里一条 ``metadata.pids`` 都没有时说明原因。"""
    items = [c for c in chunks or [] if isinstance(c, dict)]
    with_pids = sum(1 for c in items if (c.get("metadata") or {}).get("pids"))
    if with_pids or not items:
        return ""
    return (f"诊断：chunk 表 {with_pids}/{len(items)} 条带 metadata.pids"
            f"（_best_chunk_by_pids 无从匹配，重建索引后才会带上）")


def verify_paper(paper_id: str, ctx: dict) -> list[dict]:
    """跑完 11 条检查（每条独立 try/except：一条炸了只记 crashed，其余照跑）。"""
    rows = ctx.get("rows") or []
    registry = ctx.get("registry") or {}
    chunks = ctx.get("chunks") or []
    paths = ctx.get("paths") or []
    chunk_pids = ctx.get("chunk_pids") or {}
    split = make_splitter(paper_id, rows, registry)
    runners = {
        "A1": lambda: check_pid_coverage(rows),
        "A2": lambda: check_registry_rows(rows, registry),
        "A3": lambda: check_chunk_pids(chunks, registry),
        "A4": lambda: check_ref_coverage(rows, registry, chunk_pids, paths),
        "B5": lambda: check_heal_single(split, paths),
        "B6": lambda: check_heal_all(split, paths),
        "B7": lambda: check_dead_pid(split, paths),
        "B8": lambda: check_foreign_pid(split, rows, registry, paths),
        "C9": lambda: check_read_stability(pid_map(ctx.get("rows") or []),
                                           pid_map(ctx.get("rows_again") or []),
                                           ctx.get("registry_stat") or {},
                                           ctx.get("registry_stat_again") or {}),
        "C10": lambda: check_backfill_idempotent(paper_id, ctx.get("payloads") or [],
                                                 chunk_pids, ctx.get("capture")),
        "D11": lambda: check_legacy_equivalence(split, paths, _chunk_pid_note(chunks)),
    }
    out: list[dict] = []
    for check in CHECK_NAMES:
        try:
            out.append(runners[check]())
        except Exception as exc:  # noqa: BLE001 —— 一条检查炸了不影响其它检查
            out.append(result(check, False,
                              f"check crashed: {exc.__class__.__name__}: {exc}"))
    return out


# --------------------------------------------------------------------------
# IO（全部只读；每个 loader 独立 try/except）
# --------------------------------------------------------------------------


def _load_paper_ids() -> list[str]:
    """库里所有有解读的论文（默认范围：B/D 两组要用解读引用；排序稳定，输出可比）。"""
    from backend.repositories.mysql.models import Understanding
    from backend.repositories.mysql.session import session_scope

    with session_scope() as sess:
        rows = (sess.query(Understanding.paper_id).distinct()
                .order_by(Understanding.paper_id).all())
    return [str(r[0]) for r in rows if r[0]]


def _load_chunks(paper_id: str) -> list[dict]:
    """chunk 表（``metadata`` 已解析）：A3 只读它，C10 的 ``chunk_pids`` 也从这里来。"""
    from backend.core.utils import json_loads
    from backend.repositories.mysql.models import Chunk
    from backend.repositories.mysql.session import session_scope

    with session_scope() as sess:
        rows = (sess.query(Chunk).filter(Chunk.paper_id == paper_id)
                .order_by(Chunk.order_global.asc()).all())
        return [{"id": str(r.id), "chunk_type": str(r.chunk_type or ""),
                 "metadata": json_loads(r.metadata_json, {}) or {}} for r in rows]


def _load_paths(paper_id: str) -> list[dict]:
    """解读（每路一条）+ 那一路的 chunk 锚点与身份取值。

    ``resolved`` / ``inventory`` / ``pids`` 都保持落库原样（**不**像 ``to_dict`` 那样
    从 markdown 就地补齐 ``resolved``）—— A4 要看的正是"落库的这一份够不够用"。
    """
    from backend.core.utils import json_loads
    from backend.repositories.mysql.models import Understanding
    from backend.repositories.mysql.session import session_scope
    from backend.services.understanding import understanding_service as us

    with session_scope() as sess:
        rows = (sess.query(Understanding)
                .filter(Understanding.paper_id == paper_id).all())
        items = [{"path": str(r.path or ""), "markdown": r.markdown or "",
                  "refs": json_loads(r.refs_json, {}) or {},
                  "inventory": json_loads(r.inventory, []) or []} for r in rows]

    out: list[dict] = []
    for item in sorted(items, key=lambda it: it["path"]):
        refs = item["refs"]
        resolved = dict(refs.get("resolved") or {})
        by_code = {str(it["code"]): it for it in item["inventory"]
                   if isinstance(it, dict) and it.get("code")}
        anchors = us._chunk_anchors([cid for cid in resolved.values() if cid])
        saved = refs.get("pids")
        out.append({**item, "resolved": resolved, "by_code": by_code,
                    "anchors": anchors,
                    "refs_pids": saved if isinstance(saved, dict) else {}})
    return out


def _load_context(paper_id: str) -> dict:
    """一篇论文的全部只读输入（页产物读**两次**：第二次就是 C9 的幂等性证据）。"""
    from backend.services.paper import reader_service, row_identity
    from backend.services.understanding import understanding_service as us
    from scripts.backfill_ref_pids import load_capture_doc

    pages, shape = reader_service.page_products(paper_id)
    rows = us._page_rows(pages)
    path = row_identity.registry_path(paper_id)
    stat_a = file_stat(path)
    registry = row_identity.load(paper_id)
    pages_again, _shape = reader_service.page_products(paper_id)   # 第二次读（C9）
    rows_again = us._page_rows(pages_again)
    stat_b = file_stat(path)
    chunks = _load_chunks(paper_id)
    paths = _load_paths(paper_id)
    return {"shape": shape, "rows": rows, "rows_again": rows_again,
            "registry": registry, "registry_path": str(path),
            "registry_stat": stat_a, "registry_stat_again": stat_b,
            "chunks": chunks, "chunk_pids": chunk_pid_map(chunks),
            "paths": paths, "capture": load_capture_doc(paper_id),
            "payloads": [{"path": p["path"], "refs": p["refs"],
                          "inventory": p["inventory"], "markdown": p["markdown"]}
                         for p in paths]}


# --------------------------------------------------------------------------
# 输出
# --------------------------------------------------------------------------


def header_line(paper_id: str, ctx: dict) -> str:
    """一篇论文的表头：一眼看清这次判的是哪份数据。"""
    registry = ctx.get("registry") or {}
    entries = [e for e in (registry.get("rows") or []) if isinstance(e, dict)]
    return (f"== {paper_id}（shape={ctx.get('shape') or '?'} 行 {len(ctx.get('rows') or [])}"
            f" / 注册表 {len(entries)} / retired {len(registry.get('retired') or [])}"
            f" / chunk {len(ctx.get('chunks') or [])}"
            f" / 解读 {len(ctx.get('paths') or [])} 路）==")


def print_human(results: list[dict]) -> None:
    for paper in results:
        print(header_line(paper["paper_id"], paper.get("context") or {}))
        for item in paper["checks"]:
            mark = "[PASS]" if item["ok"] else "[FAIL]"
            print(f"{mark} {item['check']} {item['name']} —— {item['evidence']}")
        print()


def build_payload(results: list[dict]) -> dict:
    flat = [{"paper_id": p["paper_id"], **c} for p in results for c in p["checks"]]
    failed = [f"{c['paper_id']} {c['check']}: {c['evidence']}"
              for c in flat if not c["ok"]]
    return {
        "papers": [{"paper_id": p["paper_id"],
                    "shape": (p.get("context") or {}).get("shape"),
                    "rows": len((p.get("context") or {}).get("rows") or []),
                    "chunks": len((p.get("context") or {}).get("chunks") or []),
                    "paths": len((p.get("context") or {}).get("paths") or []),
                    "checks": p["checks"]} for p in results],
        "summary": {"papers": len(results), "checks": len(flat),
                    "pass": sum(1 for c in flat if c["ok"]),
                    "fail": len(failed), "failed": failed},
    }


def _build_parser() -> argparse.ArgumentParser:
    return argparse.ArgumentParser(
        prog="verify_pid_rules.py",
        description="验证行身份（pid）规则是否有效（只读）：A 不变量 / B 故障注入 / "
                    "C 确定性 / D 与旧逻辑等价。任意 FAIL → 退出码 1。")


def main(argv: list | None = None) -> int:
    parser = _build_parser()
    parser.add_argument("paper_ids", nargs="*",
                        help="只验这些论文（默认：库里所有有解读的论文）")
    parser.add_argument("--json", action="store_true",
                        help="机器可读输出（stdout 只出 JSON）")
    args = parser.parse_args(argv)

    logging.basicConfig(stream=sys.stderr, level=logging.WARNING,
                        format="%(levelname)s %(name)s: %(message)s")

    try:
        paper_ids = args.paper_ids or _load_paper_ids()
    except Exception as exc:  # noqa: BLE001 —— 连不上库是运行环境问题，不是规则问题
        message = f"读不到论文列表（数据库连不上？）: {exc.__class__.__name__}: {exc}"
        if args.json:
            print(json.dumps({"error": message}, ensure_ascii=False))
        print(message, file=sys.stderr)
        return 2

    results: list[dict] = []
    for paper_id in paper_ids:
        try:
            ctx = _load_context(paper_id)
        except Exception as exc:  # noqa: BLE001 —— 这读取不到就别拖垮其它论文
            checks = [result(check, False,
                             f"load crashed: {exc.__class__.__name__}: {exc}")
                      for check in CHECK_NAMES]
            results.append({"paper_id": paper_id, "context": {}, "checks": checks})
            continue
        results.append({"paper_id": paper_id, "context": ctx,
                        "checks": verify_paper(paper_id, ctx)})

    payload = build_payload(results)
    if args.json:
        print(json.dumps(payload, ensure_ascii=False))
    else:
        print_human(results)
        summary = payload["summary"]
        print(f"== 汇总：papers={summary['papers']} 检查={summary['checks']} "
              f"PASS={summary['pass']} FAIL={summary['fail']} ==")
    return 1 if payload["summary"]["fail"] else 0


if __name__ == "__main__":
    raise SystemExit(main())
