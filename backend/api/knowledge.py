"""知识库 API（规划 §20.5 + §20.4）。

范围：概览 / 纳入移出 / 触发 EMBED+INDEX / 索引状态 / chunk 浏览 / 相似检索 / RAG 问答。
检索与问答（§20.4）**已实现**：`/rag/similar`（向量 + 词法 → RRF → 重排）与
`/rag/query`（+ `/rag/query/stream`）；两路分块后 `chunks` 额外带 `road` 维度（见
`docs/论文理解系统-设计.md` §3）。
"""

from __future__ import annotations

import math

from fastapi import APIRouter, Body, Query, Request

from backend.api.response import envelope
from backend.services.knowledge import knowledge_service
from backend.services.task.task_service import task_service

router = APIRouter(tags=["knowledge"])


@router.get("/knowledge/overview")
async def overview(request: Request):
    return envelope(knowledge_service.overview(), request=request)


@router.post("/knowledge/papers")
async def set_membership(request: Request, payload: dict = Body(...)):
    """兼容保留：只翻"已纳入"标记（不动索引）。

    界面已经不用它了 —— 用户口径是**建索引即纳入**、要退出就用 ``/knowledge/remove``
    （那条会把索引一起清掉，两侧不会长期不一致）。
    """
    paper_id = payload.get("paper_id")
    include = bool(payload.get("include", True))
    return envelope(knowledge_service.set_kb_membership(paper_id, include),
                    request=request)


@router.post("/knowledge/remove")
async def remove_from_kb(request: Request, payload: dict = Body(...)):
    """**从知识库移除**：清向量 + 清 chunk 元数据 + 取消"已纳入"（论文与译文不动）。"""
    paper_id = payload.get("paper_id")
    if not paper_id:
        from backend.core.errors import ValidationError

        raise ValidationError("缺少 paper_id")
    result = knowledge_service.remove_from_kb(paper_id)
    return envelope(result, message=f"已从知识库移除（清 {result['removed_chunks']} 条 chunk）",
                    request=request)


@router.post("/knowledge/reconcile")
async def reconcile_knowledge(request: Request, payload: dict = Body(default={})):
    """对账：MySQL chunk 元数据 ↔ 向量库；``{fix: true}`` 时顺手清掉孤儿。"""
    return envelope(knowledge_service.reconcile(
        paper_id=payload.get("paper_id"), fix=bool(payload.get("fix"))), request=request)


@router.post("/knowledge/index")
async def trigger_index(request: Request, payload: dict = Body(default={})):
    paper_id = payload.get("paper_id")
    rebuild = bool(payload.get("rebuild"))
    task, created = task_service.get_or_create(
        "INDEX", "knowledge", paper_id,
        params={"rebuild": rebuild}, total_items=0,
        message="排队中（产线 C：chunk + embedding + 入库）",
    )
    return envelope(task, message=("已创建索引任务" if created else "已有进行中的索引任务"),
                    request=request)


@router.get("/knowledge/index/status")
async def index_status(request: Request, paper_id: str | None = Query(None)):
    return envelope(knowledge_service.index_status(paper_id), request=request)


@router.get("/knowledge/index/trend")
async def index_trend(request: Request, days: int = Query(30)):
    """索引增长趋势（设置页「索引管理」的趋势图）：每天新增 + 累计。

    **现存记录口径，不是历史流水** —— 对表现有的行的 ``created_at`` 分桶，没有趋势表；
    删掉的论文不进图、重建索引会把该篇 chunk 的创建时间重置成重建那一刻
    （详见 ``knowledge_service.index_trend`` 的 docstring）。

    ``days`` 夹取到 1..180，右端是服务器本地"今天"；每天一条、**连续不缺天**
    （没有数据的日子是 0，前端不用自己补洞）。只读：不建表、不写库、不触发任务。
    """
    return envelope(knowledge_service.index_trend(days), request=request)


# ---------------- 检索耗时趋势（设置页「检索耗时趋势」）----------------

#: 窗口内最多统计多少行（多出来的只取**最近**的）。**不能无上限**：一次问答 / 一次
#: 相似检索就是一行流水，180 天重度使用是几十万行 —— 全捞进内存算分位数会把这条只读
#: 接口拖成几秒。20000 行足够画出稳定的 p50/p95，被截断时 `note` 里明说。
LATENCY_ROW_CAP = 20000


def _percentile(ordered: list[float], q: float) -> float:
    """**nearest-rank** 分位数：升序第 `ceil(q·n)` 项（q ∈ (0,1]，n ≥ 1）。

    为什么是 nearest-rank 而不是线性插值：
    - 它是"真实存在的那一条的耗时" —— p95 = 190.0ms 可以直接去库里找回那条记录，
      插值出来的 190.3ms 谁都没经历过；
    - 天然满足 p50 ≤ p95、且必然落在 [min, max] 内（插值与分桶聚合混用时这两个性质
      不自动成立，而前端画的是同一条平均线）。
    代价是样本少时不够平滑（p50 会贴着某一条跳），趋势图看的是量级，够用。

    为什么在 Python 里算而不是 SQL：分位数没有可移植写法 —— MySQL 的
    `PERCENTILE_CONT` 要 8.0+（项目兼容更老的版本，而且 `DATE_FORMAT` 一类的语法
    已经吃过"SQLite 上直接报错"的亏，见 `index_trend`），SQLite 根本没有。
    """
    n = len(ordered)
    if n == 0:
        return 0.0
    index = min(n - 1, max(0, math.ceil(q * n) - 1))
    return ordered[index]


def latency_report(days: int = 30) -> dict:
    """检索耗时趋势：每（**本地**）日 count / avg / p50 / p95，外加全窗口 totals。

    数据源是 `retrieval_log` 流水（在 `retrieval.search` 这唯一出口埋的点，保留 90 天）。

    **series 只返回有数据的那几天**，不补 0 —— 跟 `index_trend` 的"每天一条、连续不缺天"
    刚好相反，因为两边问的不是同一件事：那边"那天没新增论文"是事实（0 是对的，累计曲线
    要连续）；这边"那天没人检索"不等于"那天耗时 0" —— 补 0 会把平均线拽到底，
    看上去像检索突然变快了，其实是根本没跑。日期升序，前端按 series 自己的点画。

    口径：
    - 窗口 = 最近 `days` 个本地日（含今天），`days` 夹到 1..180；
    - 库里 `ts` 是 **UTC naive**（全库约定），这里**转成本地时间再分桶**：直接
      `func.date(ts)` 分的是 UTC 日，东八区下 08:00 之前的检索会落到前一天的柱子上；
    - 分位数用 nearest-rank（见 `_percentile`）；avg 保留 1 位小数；
    - `totals`（count / avg_ms / p50_ms / p95_ms）是**整窗口一次算**的：分母是窗口内
      全部行，不是"各天数值的平均"（按天平均会被只有一两行的小日子带偏）；
    - 窗口内行数上限 `LATENCY_ROW_CAP`，超出只取**最近**的那些，并在 `note` 里说明；
    - 全窗口没有数据：`series = []`、`totals.count = 0`、`avg_ms / p50_ms / p95_ms = None`
      （不是 0：0 会被前端当成"非常快"画出来）。

    只读：不建表、不写库、不触发任务。
    """
    from datetime import date, datetime, time as dtime, timedelta, timezone

    from backend.repositories.mysql.models import RetrievalLog
    from backend.repositories.mysql.session import session_scope

    days = max(1, min(30 if days is None else int(days), 180))
    today = date.today()
    start = today - timedelta(days=days - 1)
    # 窗口左端 = 本地 start 日 00:00，换成库里那一列的口径（UTC naive）。
    # naive 的 astimezone() 会按本地时区解释，正是这里要的。
    lower = datetime.combine(start, dtime.min).astimezone(timezone.utc).replace(tzinfo=None)

    with session_scope() as sess:
        rows = (sess.query(RetrievalLog.ts, RetrievalLog.total_ms)
                .filter(RetrievalLog.ts >= lower)
                .order_by(RetrievalLog.ts.desc())
                .limit(LATENCY_ROW_CAP + 1)      # +1 只为判断"是否超上限"
                .all())

    truncated = len(rows) > LATENCY_ROW_CAP
    if truncated:
        rows = rows[:LATENCY_ROW_CAP]

    buckets: dict[str, list[float]] = {}
    for ts, total_ms in rows:
        if ts is None:
            continue
        day = ts.replace(tzinfo=timezone.utc).astimezone().date().isoformat()
        buckets.setdefault(day, []).append(float(total_ms or 0.0))

    series: list[dict] = []
    window: list[float] = []
    for day in sorted(buckets):
        values = sorted(buckets[day])
        window.extend(values)
        series.append({
            "date": day,
            "count": len(values),
            "avg_ms": round(sum(values) / len(values), 1),
            "p50_ms": round(_percentile(values, 0.50), 1),
            "p95_ms": round(_percentile(values, 0.95), 1),
        })

    window.sort()
    # totals 一律是**整窗口一次算**（分母 = 窗口内全部行），不是"各天数值的平均" ——
    # 后者会被只有一两行的小日子带偏：某天 1 条 4000ms、另一天 500 条 100ms，
    # 按天平均得 2050ms，而用户实际感受到的是后者。
    totals = {
        "count": len(window),
        "avg_ms": round(sum(window) / len(window), 1) if window else None,
        # p50 与 series[].p50_ms 同一算法、同一 nearest-rank 口径，只是分母换成整窗口行数
        "p50_ms": round(_percentile(window, 0.50), 1) if window else None,
        "p95_ms": round(_percentile(window, 0.95), 1) if window else None,
    }
    note = ("检索耗时流水（retrieval_log，保留 90 天）：按本地日分桶、只列出有检索的日子，"
            "分位数为 nearest-rank（升序第 ceil(q·n) 项），avg/p50/p95 都取总耗时 total_ms；"
            "totals 为整窗口一次计算（不是各天 avg/p50/p95 的平均）")
    if truncated:
        note += f"；窗口内超过 {LATENCY_ROW_CAP} 条，只统计最近 {LATENCY_ROW_CAP} 条"
    return {"days": days,
            "generated_at": datetime.now().isoformat(timespec="seconds"),
            "series": series, "totals": totals, "note": note}


@router.get("/knowledge/index/latency")
async def index_latency(request: Request, days: int = Query(30)):
    """检索耗时趋势（窗口夹到 1..180，右端是服务器本地"今天"）。

    **只返回有数据的日子**（不补 0，理由见 `latency_report`）；全窗口没有数据时
    `series = []` 且 `totals.count = 0` —— 前端据此显示空状态。
    """
    return envelope(latency_report(days), request=request)


@router.get("/knowledge/chunks")
async def list_chunks(request: Request, paper_id: str = Query(...),
                      chunk_type: str | None = Query(None),
                      lang: str | None = Query(None),
                      road: str | None = Query(None, pattern="^(a|b|shared)$"),
                      q: str | None = Query(None),
                      limit: int = Query(50, le=500),
                      offset: int = Query(0, ge=0)):
    """列出 chunk 元数据（**不返回全文**：全文在 LanceDB，规划 §8 三条禁止）。

    `image_url` 是插图/表格/公式的裁图（浏览器可直接取，用于知识库页的缩略图 / 图库）；
    正文条目为 `None`。

    **默认不列语义单元**（`chunk_type="unit"`）：这是"逐块读这篇论文"的浏览视图，
    单元是检索用的更细粒度（同一批行的另一层划分），默认混进来只会让列表翻倍。
    要看单元就显式传 `chunk_type=unit`。

    `road`：`a` 英文转录 / `b` 中文翻译 / `shared` 两路共用（图、表、公式）。
    路号在 id 与 `metadata_json` 里（不另加列），所以这里按 id 段过滤后再按元数据核对。

    `q`：大小写不敏感的子串，命中 `id` 或 `content_preview` 任一即可。
    MySQL 的排序规则本来就不敏感、SQLite 的 `LIKE` 也不敏感，但"各库默认"不是同一份
    约定 —— 两边显式套 `func.lower`，行为由这一行保证，不靠数据库的默认值。

    `offset` / `total`：`total` 是**过滤后、分页前**的条数（界面显示"共 N 条"）；
    `count` 仍是本次返回的条数，老调用方（只读 items / count）语义一字不变。

    ⚠️ `lang` / `road` 只能把行取回来按 `metadata_json` 核对，这条路径一次最多看
    5000 行（`total` 也受这个上限约束）；不传这两个参数时过滤全在 SQL 里，`total` 精确。
    """
    from sqlalchemy import func, or_

    from backend.core.utils import json_loads
    from backend.repositories.mysql.models import Chunk
    from backend.repositories.mysql.session import session_scope

    def _item(c) -> dict:
        meta = json_loads(c.metadata_json, {}) or {}
        return {"id": c.id, "paper_id": c.paper_id, "block_id": c.block_id,
                "chunk_type": c.chunk_type, "page": c.page_no,
                "lang": meta.get("lang") or "en", "road": meta.get("road") or "shared",
                "section": c.section, "preview": c.content_preview,
                "caption": meta.get("caption"),
                "hash": c.content_hash, "tokens": c.token_count,
                "index_version": c.index_version,
                "embedding_model_id": c.embedding_model_id,
                "image_url": knowledge_service.chunk_image_url(
                    c.paper_id, c.chunk_type, int(c.page_no or 0), meta)}

    keyword = (q or "").strip().lower()
    with session_scope() as sess:
        query = sess.query(Chunk).filter(Chunk.paper_id == paper_id)
        if chunk_type:
            query = query.filter(Chunk.chunk_type == chunk_type)
        else:
            query = query.filter(Chunk.chunk_type != "unit")
        if road:
            # id 里带路号段（`{paper}:{A|B}:{type}:…`）；共用块的 id 没有这一段。
            # 用 SQL 先缩小范围，最终仍按元数据核对（id 只是投影）。
            if road == "shared":
                query = query.filter(~Chunk.id.like("%:A:%"), ~Chunk.id.like("%:B:%"))
            else:
                query = query.filter(Chunk.id.like(f"%:{road.upper()}:%"))
        if keyword:
            pattern = f"%{keyword}%"
            query = query.filter(or_(func.lower(Chunk.id).like(pattern),
                                     func.lower(Chunk.content_preview).like(pattern)))
        query = query.order_by(Chunk.order_global.asc())
        if lang or road:
            # 语种 / 路号只活在 metadata_json 里：取回来再核对，分页也只能在内存里做。
            rows = query.limit(5000).all()
            matched = []
            for c in rows:
                meta = json_loads(c.metadata_json, {}) or {}
                row_lang = meta.get("lang") or "en"
                # `any` = 语言无关（公式原子行）：按中文、英文过滤时都要出现
                if lang and row_lang not in (lang, "any"):
                    continue
                if road and (meta.get("road") or "shared") != road:
                    continue
                matched.append(c)
            total = len(matched)
            items = [_item(c) for c in matched[offset:offset + limit]]
        else:
            total = int(query.count())
            items = [_item(c) for c in query.offset(offset).limit(limit).all()]
    return envelope({"items": items, "count": len(items), "total": total}, request=request)
@router.get("/knowledge/chunk")
async def chunk_detail(request: Request, chunk_id: str = Query(...)):
    """**单个 chunk 的完整正文**（知识库页「展开全文」）。

    列表接口只给 200 字 preview（规划 §8：全文只在向量库）；这条只读那一条向量行，
    不触发任务、不写状态。不做中英对照 —— 完整阅读回阅读器。
    """
    return envelope(knowledge_service.chunk_detail(chunk_id), request=request)


# ---------------- §20.4 相似检索与 RAG 问答 ----------------


@router.post("/rag/similar")
async def rag_similar(request: Request, payload: dict = Body(default={})):
    """跨文献 / 全库相似检索：向量 + 词法 → RRF → 重排（任务书功能⑥）。

    请求体：``{query?}`` 或 ``{chunk_id?}``；``scope`` = all | cross | paper；
    可选 ``paper_id`` / ``chunk_types`` / ``langs`` / ``top_k``。
    """
    return envelope(knowledge_service.similar(payload), request=request)


@router.post("/rag/query")
async def rag_query(request: Request, payload: dict = Body(...)):
    """RAG 问答（§20.4）：混合检索 → ContextBuilder → 生成 → AnswerValidator。

    请求体：``{question, scope: current_paper|knowledge_base|cross, paper_id?, top_k?}``；
    响应：``{implemented, answer, sources[], grounded, validation, retrieval_metadata, usage}``。
    """
    from backend.services.knowledge import rag_service

    return envelope(rag_service.answer(payload), request=request)


@router.post("/rag/query/stream")
def rag_query_stream(payload: dict = Body(...)):
    """RAG 流式问答（SSE）：``rag_meta`` → ``rag_token``* → ``rag_done`` / ``rag_error``。

    同步生成器 + ``StreamingResponse``：同步 SDK 的流式接口在线程池里迭代，
    不阻塞事件循环。
    """
    from fastapi.responses import StreamingResponse

    from backend.services.knowledge import rag_service

    return StreamingResponse(rag_service.stream_answer(payload),
                             media_type="text/event-stream",
                             headers={"Cache-Control": "no-cache",
                                      "X-Accel-Buffering": "no"})
