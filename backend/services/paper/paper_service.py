"""论文服务：导入 / 列表 / 详情 / 删除（规划 §20.1、§21.1、§21.12）。

删除严格遵循「**先删索引，最后删文件**」（规划 §21.12）：
MySQL 软删 → 清向量索引 → 取消任务 → 清 chunk 元数据 → 文件移入 trash → 对账。
"""

from __future__ import annotations

import shutil
from pathlib import Path

from backend.core.config import get_settings
from backend.core.errors import ConflictError, NotFoundError, ValidationError
from backend.core.logging import get_logger
from backend.core.utils import (
    iso_utc, json_dumps, json_loads, new_id, now, safe_filename, sha256_file, slugify,
    ts_compact,
)
from backend.repositories.mysql.models import Block, Paper, Task, Translation
from backend.repositories.mysql.session import session_scope
from backend.services.task.task_service import task_service

log = get_logger(__name__)


# --------------------------------------------------------------------------
# 导入（规划 §21.1）
# --------------------------------------------------------------------------


def import_pdf(temp_path: str | Path, original_name: str,
               *, title: str | None = None, authors: str | None = None,
               venue: str | None = None, year: int | None = None,
               tags: str | None = None, paper_id: str | None = None,
               auto_parse: bool = True, reuse_existing: bool = True,
               figure_caption: bool = False,
               deep_figure_search: bool = False,
               parse_engine: str | None = None,
               parse_params: dict | None = None) -> dict:
    """上传 → 校验 → 落盘 source.pdf + source_hash → 建 paper 行 → 建 IMPORT/PARSE 任务。

    ``reuse_existing=True`` 时，若同一 PDF（sha256 相同）已导入过且未删除，
    直接复用那篇论文（重新落盘 + 可选重新解析），不再新建一条记录。
    这对"重复演示/回归测试"很关键：否则会不断产生 ``xxx-2``、``xxx-3``。

    ``figure_caption`` / ``deep_figure_search``：导入弹窗的两个插图开关（均默认关），
    **导入即重解析**，所以它们只落进 PARSE 任务参数、在本次解析时生效（复用那篇论文时
    也按**新选的**值重发解析）。语义见 ``PaperBuilder``。

    ``parse_engine``：解析引擎（``local`` / ``mineru``）。``None`` = 用设置里的默认引擎
    （用户在导入弹窗里一定会显式选一个，``None`` 只留给 CLI / 脚本）。

    ``parse_params``：引擎自己的可选参数（目前只有在线解析的 ``mineru_*``：
    模型版本 / OCR 语言 / 强制 OCR / 公式 / 表格 / 页码范围），原样进 PARSE 任务参数。
    """
    from backend.services.paper import mineru_service

    opts = {"figure_caption": bool(figure_caption),
            "deep_figure_search": bool(deep_figure_search),
            "engine": (mineru_service.effective()["engine_default"]
                       if parse_engine is None else _check_engine(parse_engine))}
    if parse_params:
        opts.update(parse_params)
    s = get_settings()
    src = Path(temp_path)
    if not src.is_file():
        raise ValidationError(f"上传文件不存在：{src}")
    size = src.stat().st_size
    if size == 0:
        raise ValidationError("上传文件为空")
    if size > 300 * 1024 * 1024:
        raise ValidationError("文件过大（>300MB）")

    digest = sha256_file(src)
    if reuse_existing:
        existing = _find_by_hash(digest)
        if existing is not None:
            pdir = s.paper_dir(existing["id"])
            pdir.mkdir(parents=True, exist_ok=True)
            shutil.copy2(src, pdir / "source.pdf")
            # ⚠️ 复用时**显式传进来的 `paper_id` 会被忽略**（同一份 PDF 只有一篇论文）。
            # 调用方拿到的是别人那篇 —— 踩过一次：脚本里传 `paper_id="zz-purge-demo"`、
            # 下一步拿返回值去删论文，删掉的是**已存在的那篇**（真的发生在本项目上）。
            # 这里把不一致喊出来，别让调用方以为新建成功。
            if paper_id and str(paper_id) != str(existing["id"]):
                log.warning("同一 PDF 已存在（%s），忽略请求的 paper_id=%s —— 复用既有的那篇",
                            existing["id"], paper_id)
            log.info("同一 PDF 已导入过，复用论文 %s", existing["id"])
            task_service.create("IMPORT", "system", existing["id"], params={
                "original_name": original_name, "size": size, "sha256": digest,
                "reused": True,
            }, total_items=1, message="同一文件已导入过，复用现有论文")
            if auto_parse:
                create_parse_task(existing["id"], force=True, params=opts)
            return {**existing, "reused": True}

    # ⚠️ 上传接口会带一个 `title` 表单字段，而前端**预填的就是文件名** ——
    # 直接 `title or meta.title` 会让文件名压过解析层抽出来的真标题
    # （实测：DeepSeek-V3.2 的附录 PDF 抽得出 "DeepSeek-V3.2-Exp: …"，库里却叫
    # `12382-beyond-speedup-utilizing-2`）。所以"传进来的 title == 文件名"视为**没填**。
    given_title = (title or "").strip()
    if given_title and given_title == Path(original_name).stem.strip():
        given_title = ""

    pid = paper_id or slugify(given_title or Path(original_name).stem, fallback="paper")
    pid = _unique_paper_id(pid)

    page_count = 0
    detected_title = given_title
    detected_authors = authors or ""
    detected_venue = venue or ""
    detected_year = year
    detected_abstract = ""
    try:
        import pymupdf as fitz
        from backend.parsing.metadata import extract_paper_meta

        doc = fitz.open(str(src))
        page_count = doc.page_count
        # 元数据能自动抽的就别让用户手打：只有用户**没填**的字段才用抽出来的值
        # （见 `backend/parsing/metadata.py`）。
        meta = extract_paper_meta(doc, Path(original_name))
        detected_title = detected_title or meta.title or Path(original_name).stem
        detected_authors = detected_authors or meta.authors
        detected_venue = detected_venue or meta.venue
        detected_year = detected_year or meta.year
        detected_abstract = meta.abstract
        doc.close()
    except Exception as exc:  # noqa: BLE001
        raise ValidationError(
            f"PDF 无法解析（可能损坏或加密）：{exc}") from exc

    pdir = s.paper_dir(pid)
    pdir.mkdir(parents=True, exist_ok=True)
    target = pdir / "source.pdf"
    shutil.copy2(src, target)

    with session_scope() as sess:
        row = Paper(
            id=pid, title=detected_title or Path(original_name).stem,
            authors=detected_authors or None, venue=detected_venue or None,
            year=detected_year, abstract=detected_abstract[:2000] or None,
            page_count=page_count, source_hash=digest, status="imported",
            tags=tags,
        )
        sess.add(row)
        sess.flush()
        result = _to_dict(row)
    log.info("导入论文 %s（%d 页，%s）", pid, page_count, digest[:12])

    task_service.create("IMPORT", "system", pid, params={
        "original_name": original_name, "size": size, "sha256": digest,
    }, total_items=1, message="已落盘，等待解析")
    if auto_parse:
        create_parse_task(pid, params=opts)
    return result


def _find_by_hash(digest: str) -> dict | None:
    with session_scope() as sess:
        row = (sess.query(Paper)
               .filter(Paper.source_hash == digest, Paper.deleted_at.is_(None))
               .order_by(Paper.created_at.desc()).first())
        return _to_dict(row) if row is not None else None


def _unique_paper_id(base: str) -> str:
    with session_scope() as sess:
        if sess.get(Paper, base) is None:
            return base
        for i in range(2, 100):
            cand = f"{base}-{i}"
            if sess.get(Paper, cand) is None:
                return cand
    return f"{base}-{new_id('x')[-6:]}"


def create_parse_task(paper_id: str, *, force: bool = False,
                      params: dict | None = None) -> dict:
    """创建 PARSE 任务（幂等：已有进行中的 PARSE 则复用）。

    ``params`` 透传给任务体，支持：
    ``engine``（``local`` / ``mineru``，见 ``PaperBuilder`` / ``MineruBuilder``）、
    ``llm_repair``（公式 LLM 修复）、``vision`` / ``vision_model`` / ``vision_dpi``
    （扫描件读页）、``figure_caption`` / ``deep_figure_search``
    （两个插图开关）。

    这几个"解析口径"开关**没传就沿用该论文上一次解析用的值**：用户从论文库点「重新解析」时
    不会把他上次特意选的引擎或开关悄悄换掉（导入时则由弹窗显式给出，走的是"传了就用传的"）。
    """
    get_paper(paper_id)  # 存在性校验
    active = task_service.find_active("PARSE", paper_id, "system")
    if active and not force:
        return active
    payload = {"force": force, **(params or {}),
               **_fill_parse_options(paper_id, params or {})}
    # 引擎在**这里**归一：`_fill_parse_options` 只补"没给"的键，给了的值必须自己校验，
    # 否则一个拼错的 engine（比如脚本里写的 "cloud"）会被静默当成本地解析 ——
    # 用户看到的是"跑了、但结果不像 MinerU"。（`auto` = 用设置里的默认引擎。）
    if "engine" in payload:
        payload["engine"] = _check_engine(str(payload["engine"] or "auto"))
    return task_service.create("PARSE", "system", paper_id, params=payload,
                               total_items=9, message="排队中")


def _check_engine(value: str) -> str:
    """引擎取值校验：``local`` / ``mineru``，``auto`` = 用设置里的默认引擎。

    写库前拦住拼错的值（``ValidationError`` → HTTP 400，用户立刻知道参数不对）。
    """
    from backend.services.paper import mineru_service

    if str(value or "").strip().lower() in ("", "auto"):
        return mineru_service.effective()["engine_default"]
    return mineru_service.norm_engine(value)


def _fill_parse_options(paper_id: str, params: dict) -> dict:
    """给 PARSE 参数补齐"解析口径"开关（只补**没给**的那些）。

    取值优先级：调用方给的 > 该论文上一次解析固化的值（`figure_regions.parse_options`
    从 IR 里读）> 默认值（两个插图开关默认关、引擎取设置里的默认引擎）。
    读不到（从未解析过 / IR 缺失）时按默认值处理。
    """
    from backend.services.paper import figure_regions, mineru_service

    defaults = dict(figure_regions.DEFAULT_PARSE_OPTIONS)
    defaults["engine"] = mineru_service.effective()["engine_default"]
    missing = [k for k in defaults if k not in params]
    if not missing:
        return {}
    try:
        last = figure_regions.parse_options(paper_id)
    except Exception as exc:  # noqa: BLE001 —— 读不到就用默认值，不该拦住解析
        log.warning("读取论文 %s 的解析开关失败（按默认值处理）：%s", paper_id, exc)
        last = {}
    out: dict = {}
    for k in missing:
        if k == "engine":
            out[k] = mineru_service.norm_engine(last.get(k) or defaults[k])
        else:
            out[k] = bool(last.get(k, defaults[k]))
    return out


# --------------------------------------------------------------------------
# 查询
# --------------------------------------------------------------------------


def get_paper(paper_id: str, include_deleted: bool = False) -> dict:
    with session_scope() as sess:
        row = sess.get(Paper, paper_id)
        if row is None or (row.deleted_at is not None and not include_deleted):
            raise NotFoundError(f"论文不存在：{paper_id}")
        return _to_dict(row)


def list_papers(*, q: str | None = None, status: str | None = None,
                tag: str | None = None, include_deleted: bool = False,
                sort: str = "last_read_at", order: str = "desc",
                page: int = 1, page_size: int = 50) -> dict:
    with session_scope() as sess:
        query = sess.query(Paper)
        if not include_deleted:
            query = query.filter(Paper.deleted_at.is_(None))
        if status:
            query = query.filter(Paper.status == status)
        if tag:
            query = query.filter(Paper.tags.like(f"%{tag}%"))
        if q:
            like = f"%{q}%"
            query = query.filter((Paper.title.like(like)) | (Paper.authors.like(like)))
        col = getattr(Paper, sort, Paper.created_at)
        query = query.order_by(col.desc() if order == "desc" else col.asc())
        total = query.count()
        rows = query.offset(max(0, (page - 1) * page_size)).limit(page_size).all()
        items = [_to_dict(r) for r in rows]
    return {"items": items, "total": total, "page": page, "page_size": page_size}


#: 论文可以做结构化编辑的字段。
#: `contributions` / `prereq` 是**列表**（库里存 JSON），`tags` / `keywords_en` 也接受列表
#: （库里是逗号分隔的字符串）—— 统一在这里归一，免得每个调用方各转一遍。
_LIST_FIELDS = {"contributions": "contributions_json", "prereq": "prereq_json"}
_CSV_FIELDS = {"tags", "keywords_en"}
#: 元数据字段（写 `meta_sources_json` 时用；不含 status / in_knowledge_base 这类状态）
META_FIELDS = (
    "title", "authors", "venue", "year", "tags", "abstract", "abstract_zh",
    "keywords_en", "domain", "one_liner", "contributions", "prereq", "difficulty",
    "read_time_min", "paper_type", "doi", "code_url",
)


def _csv(value) -> str | None:
    if value is None:
        return None
    if isinstance(value, (list, tuple, set)):
        items = [str(x).strip() for x in value if str(x).strip()]
        return ",".join(dict.fromkeys(items))[:512] or None
    return str(value).strip()[:512] or None


def update_paper(paper_id: str, payload: dict, *, source: str | None = "manual") -> dict:
    """改论文行。

    ``source``：**这次的改动算谁做的**（`manual` 用户 / `pdf` / `model` / `agent`），
    写进 `meta_sources_json`。为什么要它：元数据以前没有来源标记，"补全"只能一律
    "只填空字段" —— 分不清"空是因为论文里没有"还是"用户故意清空"，也就没法重填某一项。
    传 ``source=None`` 表示不记来源（内部状态字段用它）。
    """
    allowed = {"title", "authors", "venue", "year", "tags", "status",
               "in_knowledge_base", "abstract",
               "abstract_zh", "keywords_en", "domain", "one_liner",
               "contributions", "prereq", "difficulty", "read_time_min",
               "paper_type", "doi", "code_url"}
    with session_scope() as sess:
        row = sess.get(Paper, paper_id)
        if row is None or row.deleted_at is not None:
            raise NotFoundError(f"论文不存在：{paper_id}")
        touched: list[str] = []
        for k, v in payload.items():
            if k not in allowed or v is None:
                continue
            key = _LIST_FIELDS.get(k)
            if key:
                items = [str(x).strip() for x in (v if isinstance(v, (list, tuple)) else [v])
                         if str(x).strip()]
                setattr(row, key, json_dumps(items) if items else None)
            elif k in _CSV_FIELDS:
                setattr(row, k, _csv(v))
            else:
                setattr(row, k, v)
            touched.append(k)
        if source and touched:
            sources = json_loads(row.meta_sources_json, {}) or {}
            for k in touched:
                if k in META_FIELDS:
                    sources[k] = source
            row.meta_sources_json = json_dumps(sources)
            row.meta_filled_at = now()
        sess.flush()
        return _to_dict(row)


def references(paper_id: str) -> list[dict]:
    """这篇论文的参考文献（按论文里的顺序）。

    `title` / `title_en` / `authors` 是**离线抽取时从翻译产物里对齐着拆出来的**
    （`metadata_fill.split_ref_text`）：中文标题、英文标题、作者段 —— 侧栏据此中英对照显示、
    并把作者那一段收起来（`raw` 永远是完整原文）。没翻译的论文这三个都是 NULL。
    `page_no` 是这一条所在的页码，侧栏卡片的「定位」按钮用它跳回正文。
    `code_url` 是这一条里的 GitHub 仓库地址（有就并排多给一枚代码图标）。
    """
    from backend.repositories.mysql.models import PaperReference

    with session_scope() as sess:
        rows = (sess.query(PaperReference)
                .filter(PaperReference.paper_id == paper_id)
                .order_by(PaperReference.order_no.asc()).all())
        return [{"id": r.id, "order_no": int(r.order_no or 0), "raw": r.raw or "",
                 "title": r.title, "title_en": r.title_en, "authors": r.authors,
                 "page_no": int(r.page_no) if r.page_no else None,
                 "venue": r.venue, "year": r.year, "doi": r.doi, "url": r.url,
                 "code_url": r.code_url, "source": r.source}
                for r in rows]


def touch_read(paper_id: str) -> None:
    with session_scope() as sess:
        row = sess.get(Paper, paper_id)
        if row is not None:
            row.last_read_at = now()


def set_translate_progress(paper_id: str, *, done: int, total: int) -> None:
    """逐页翻译**进行中**的轻量写回（只 UPDATE 两列，不读磁盘）。

    为什么必须实时写：论文库卡片上的进度条读的就是 `translate_done/translate_total`。
    以前只在任务**结束时**才回写，于是翻译跑到一半卡片还停在旧数字（用户报"已完成一半但未显示进度"）。
    """
    _set_page_progress(paper_id, done=done, total=total,
                       road="translate")


def set_transcribe_progress(paper_id: str, *, done: int, total: int) -> None:
    """A 路（英文转录）**进行中**的轻量写回；字段独立于 translate_*。"""
    _set_page_progress(paper_id, done=done, total=total, road="transcribe")


def _set_page_progress(paper_id: str, *, done: int, total: int, road: str) -> None:
    done_col = f"{road}_done"
    total_col = f"{road}_total"
    with session_scope() as sess:
        row = sess.get(Paper, paper_id)
        if row is None or row.deleted_at is not None:
            return
        setattr(row, done_col, max(0, int(done)))
        if total:
            setattr(row, total_col, int(total))
        if (road == "translate" and row.status != "failed"
                and row.translate_done < (row.translate_total or 0)):
            row.status = "translating"
        sess.flush()


def refresh_progress_counters(paper_id: str) -> dict:
    """按磁盘的真实状态回写论文进度字段（服务端权威）：两路各写自己的那对计数器。"""
    stats = translation_stats(paper_id)
    tr = transcription_stats(paper_id)
    with session_scope() as sess:
        row = sess.get(Paper, paper_id)
        if row is None:
            raise NotFoundError(f"论文不存在：{paper_id}")
        row.translate_total = stats["total"]
        row.translate_done = stats["done"]
        row.transcribe_total = tr["total"]
        row.transcribe_done = tr["done"]
        # 状态机只按"论文能不能读"来定：中文译文齐了才算 `translated`（A 路单独完成不算）
        if stats["total"] and stats["done"] >= stats["total"]:
            row.status = "translated"
        elif stats["done"] > 0:
            row.status = "translating"
        sess.flush()
        return _to_dict(row)


def translation_stats(paper_id: str) -> dict:
    """翻译进度：``{total, done, stale}`` —— **以页为单位**（页级翻译是唯一粒度）。

    页号清单以渲染出来的页图为准；``done`` 的口径是 **B 路（中文翻译）已产出的页数**：
    两路拆开之后，"翻译进度"就是中文那一侧的完成度（A 路英文转录的进度另有
    `Paper.transcribe_done`）。旧格式（`pages.json`，同一次调用产出两侧）两种口径等价 ——
    因为旧产物里"有行"必然意味着"有译文"（`call_page` 缺任一侧面都会报错重试）。
    """
    return _page_progress(paper_id, "path_b", "page_products")


def transcription_stats(paper_id: str) -> dict:
    """A 路（英文转录）进度：优先 `md/path_a.json`，没有就退回旧合并产物。

    与 `translation_stats` 分开是刻意的：两路各自落盘、各自续跑，进度也各算各的
    （否则"只跑完 A 路"会显示成"翻译完成"）。存量论文的英文内容在 `pages.json` 里，
    所以退回旧产物后是 `22/22` —— 那才是事实（阅读器现在读到的英文就是它）。
    """
    return _page_progress(paper_id, "path_a", "page_products")


def _page_progress(paper_id: str, primary: str, fallback: str | None) -> dict:
    from backend.services.translation.page_translate import PageStore, list_pages

    paths = paper_paths(paper_id)
    total = len(list_pages(paper_id))
    store = PageStore(paper_id, paths[primary])         # 只读，version 无关紧要
    done = len(store.done_pages())
    if fallback and not done:
        legacy = PageStore(paper_id, paths[fallback])
        done = len(legacy.done_pages())
        total = total or int(legacy.stats().get("page_total") or 0)
    else:
        total = total or int(store.stats().get("page_total") or 0)
    return {"total": int(total), "done": int(done), "stale": 0}


# --------------------------------------------------------------------------
# 文件路径
# --------------------------------------------------------------------------


def paper_paths(paper_id: str) -> dict:
    s = get_settings()
    d = s.paper_dir(paper_id)
    return {
        "root": d, "pdf": d / "source.pdf", "ir": d / "ir.sqlite",
        "pages": d / "pages", "figures": d / "figures",
        "crops": d / "formula_crops", "md": d / "md", "reports": d / "reports",
        "markdown": d / "md" / "full.md",
        "blocks_index": d / "md" / "blocks.json",
        # ---- 页级翻译产物（两路拆分后）----
        # A 路 = 英文转录、B 路 = 中文翻译，各自**一个文件**。
        # 为什么必须分开：`PageStore.flush()` 是整文件原子重写，两个并发写者共用一个文件
        # 会互相覆盖（后写者把人家的页抹掉）。见 docs/论文理解系统-设计.md §2.1。
        "path_a": d / "md" / "path_a.json",
        "path_b": d / "md" / "path_b.json",
        # 旧格式的**合并**产物（同一次调用产出两侧）：拆开后只读，不再写入。
        # 存量论文靠它零迁移继续可读（`reader_service.page_products` 会合成 A/B 视图）。
        "page_products": d / "md" / "pages.json",
    }


def get_markdown(paper_id: str) -> str | None:
    p = paper_paths(paper_id)["markdown"]
    if not p.is_file():
        return None
    return p.read_text(encoding="utf-8")


def resolve_pdf(paper_id: str) -> Path:
    p = paper_paths(paper_id)["pdf"]
    if not p.is_file():
        raise NotFoundError(f"PDF 文件缺失：{paper_id}")
    return p


def resolve_page_image(paper_id: str, page_no: int, dpi: int | None = None) -> Path:
    """页面图。

    ``dpi=None`` → 解析阶段已经渲染好的那一张（阅读器口径，``render_dpi``=150）。
    给了 dpi 且与它不同 → **按需渲染并落盘缓存**（文件名带 dpi，不覆盖阅读器那张）。

    为什么需要按需渲染：问答 agent 要看公式上下标，150dpi 常常不够；而把 150dpi 的图
    放大到 400dpi 只是插值，信息量一点没增加 —— 必须重新从 PDF 渲染。
    """
    pages = paper_paths(paper_id)["pages"]
    if dpi is None:
        p = pages / f"page-{page_no:04d}.png"
        if not p.is_file():
            raise NotFoundError(f"页面图不存在：{paper_id} p{page_no}")
        return p

    use = max(72, min(int(dpi), 600))
    target = pages / f"page-{page_no:04d}-{use}dpi.png"
    if target.is_file() and target.stat().st_size > 0:
        return target

    from backend.core.utils import write_bytes_atomic
    from backend.parsing.extract.render import render_page_png

    pdf = resolve_pdf(paper_id)
    import pymupdf as fitz

    pages.mkdir(parents=True, exist_ok=True)
    with fitz.open(str(pdf)) as doc:
        if page_no < 1 or page_no > doc.page_count:
            raise NotFoundError(f"页码超出范围：p{page_no}（共 {doc.page_count} 页）")
        data = render_page_png(doc, page_no, dpi=use)
    write_bytes_atomic(target, data)
    log.info("按需渲染页面图：%s p%d @%ddpi（%d bytes）", paper_id, page_no, use, len(data))
    return target


def save_upload(data: bytes, filename: str) -> Path:
    s = get_settings()
    s.uploads_dir.mkdir(parents=True, exist_ok=True)
    target = s.uploads_dir / f"{ts_compact()}-{safe_filename(filename, 'upload.pdf')}"
    target.write_bytes(data)
    return target


# --------------------------------------------------------------------------
# 删除（规划 §21.12：先删索引，最后删文件）
# --------------------------------------------------------------------------


#: `_purge_paper_content` 的字段 → 人话（写进删除步骤，用户看得见清了什么）
_PURGE_LABELS = {
    "conversations": "对话", "messages": "消息", "attachments": "附件图",
    "annotations": "划词解释", "highlights": "高亮", "notes": "笔记",
    "progress": "阅读进度", "understanding": "解读", "formulas": "公式",
}


def _purge_paper_content(paper_id: str) -> dict:
    """清掉这篇论文的**用户内容**：会话（含附件图）→ 划词解释 / 高亮 / 笔记 /
    阅读进度 / 解读 / 公式。

    会话那几张表归 agent 侧那个模块管（`conversations.purge_paper`：它的表、它的附件），
    这里只负责把结果汇总进删除步骤。**任何一段失败都不该让删论文半途而废** ——
    出错只记日志，磁盘与其它清理照常走。
    """
    from backend.repositories.mysql.models import (Annotation, Formula, Highlight, Note,
                                                   ReadingProgress, Understanding)
    from backend.services.agent import conversations as agent_conversations

    counts: dict[str, int] = {}
    try:
        counts.update(agent_conversations.purge_paper(paper_id))
    except Exception as exc:  # noqa: BLE001
        log.warning("清会话历史失败（继续清理其它内容）：%s", exc)
    with session_scope() as sess:
        for key, model in (("annotations", Annotation), ("highlights", Highlight),
                           ("notes", Note), ("progress", ReadingProgress),
                           ("understanding", Understanding), ("formulas", Formula)):
            counts[key] = int(sess.query(model).filter(model.paper_id == paper_id)
                              .delete(synchronize_session=False) or 0)
    return counts


def delete_paper(paper_id: str, *, hard: bool = False) -> dict:
    """软删：MySQL 标记 + 立即清索引/任务/chunk/**用户内容** + 文件移入 trash。

    ``hard=True`` 时先执行同样的清理，再物理删除 trash 与 MySQL 行；
    对已软删的论文再次调用 ``hard=True`` 也允许（幂等清理）。
    """
    from backend.repositories.mysql.models import Chunk

    s = get_settings()
    p = get_paper(paper_id, include_deleted=True)
    steps: list[str] = []

    # 1) MySQL 软删
    with session_scope() as sess:
        row = sess.get(Paper, paper_id)
        if row is not None:
            row.deleted_at = row.deleted_at or now()
            row.in_knowledge_base = False
        steps.append("mysql: 标记 deleted_at")

    # 2) 立即删向量索引（避免"检索到已删论文却跳 404"）
    removed = _drop_vector_index(paper_id)
    steps.append(f"lancedb: 删除 {removed} 条 chunk")

    # 3) 取消未完成任务 + 清 chunk 元数据
    cancelled = 0
    for t in task_service.list(paper_id=paper_id, limit=200):
        if t["status"] in ("pending", "processing", "paused"):
            task_service.request_cancel(t["id"])
            cancelled += 1
    with session_scope() as sess:
        n = sess.query(Chunk).filter(Chunk.paper_id == paper_id).delete()
        steps.append(f"mysql: 取消 {cancelled} 个任务，删除 {n} 条 chunk 元数据")

    # 3.5) **属于这篇论文的用户内容**（用户 2026-10-03 问的"删论文，历史记录能不能清干净"）：
    #      会话与消息（agent 侧的表，由那个模块自己清 + 顺带删附件图）、划词解释历史、
    #      高亮、笔记、阅读进度、理解产物、公式。
    #      为什么**软删就清**：软删时索引与 chunk 已经清了，`data/trash` 里只有 PDF 与页级
    #      产物 —— 留着这些行只会变成孤儿；而论文 id 是标题派生的 slug，
    #      **同一篇重新导入会拿到同一个 id**，旧对话会挂到新论文上"复活"。
    purged = _purge_paper_content(paper_id)
    steps.append("mysql: 清用户内容（" + "、".join(
        f"{_PURGE_LABELS[key]} {value}" for key, value in purged.items() if value) + "）")

    # 4) 文件移入 trash（延迟物理删除）
    root = s.paper_dir(paper_id)
    trash = None
    if root.exists():
        trash = s.trash_dir / f"{paper_id}-{ts_compact()}"
        s.trash_dir.mkdir(parents=True, exist_ok=True)
        shutil.move(str(root), str(trash))
        steps.append(f"fs: 移入 trash {trash.name}")

    if hard:
        # 彻底删除：无论文件是否已移入 trash 都要清 MySQL 行（支持对已软删论文再清理）
        if trash is not None:
            shutil.rmtree(trash, ignore_errors=True)
        with session_scope() as sess:
            sess.query(Block).filter(Block.paper_id == paper_id).delete()
            sess.query(Translation).filter(Translation.paper_id == paper_id).delete()
            sess.query(Task).filter(Task.paper_id == paper_id).delete()
            row = sess.get(Paper, paper_id)
            if row is not None:
                sess.delete(row)
        steps.append("彻底删除：trash + MySQL 行")

    log.info("删除论文 %s：%s", paper_id, "；".join(steps))
    return {"paper_id": paper_id, "hard": hard, "steps": steps, "trash": str(trash)
            if trash else None, "was": p["title"]}


def _drop_vector_index(paper_id: str) -> int:
    """删除 LanceDB 中该论文的 chunk（未启用向量库时返回 0）。"""
    try:
        from backend.repositories.vector.lancedb_store import vector_store

        return vector_store.delete_paper(paper_id)
    except Exception as exc:  # noqa: BLE001
        log.warning("删除向量索引失败（可忽略，索引为派生数据）：%s", exc)
        return 0


def reconcile(paper_id: str | None = None, *, include_deleted: bool = True) -> dict:
    """对账 CLI（规划 §8 一致性机制：只报告不自动删）。

    - 逐个论文核对 MySQL / 文件系统 / LanceDB 三处是否一致；
    - ``include_deleted=True``（默认）时也核对已软删的论文——
      "检索到已删论文却跳 404" 正是这里要发现的典型不一致。
    """
    from backend.repositories.mysql.models import Chunk

    s = get_settings()
    report: dict = {"papers": [], "issues": []}

    def check_one(pid: str, row: Paper | None) -> dict:
        paths = paper_paths(pid)
        md_exists = paths["markdown"].is_file()
        ir_exists = paths["ir"].is_file()
        pdf_exists = paths["pdf"].is_file()
        with session_scope() as sess:
            blocks = sess.query(Block).filter(Block.paper_id == pid).count()
            chunks = sess.query(Chunk).filter(Chunk.paper_id == pid).count()
            translations = (sess.query(Translation)
                            .filter(Translation.paper_id == pid).count())
        vector_count = None
        try:
            from backend.repositories.vector.lancedb_store import vector_store

            vector_count = vector_store.count(paper_id=pid)
        except Exception:  # noqa: BLE001
            vector_count = None
        entry = {
            "paper_id": pid, "pdf": pdf_exists, "ir": ir_exists, "markdown": md_exists,
            "mysql_blocks": blocks, "mysql_chunks": chunks,
            "mysql_translations": translations, "vector_chunks": vector_count,
            "deleted_at": iso_utc(row.deleted_at) if row else None,
        }
        issues = []
        if row is not None and row.deleted_at is None:
            if not pdf_exists:
                issues.append("文件缺失：source.pdf 不存在")
            if md_exists == (blocks == 0):
                issues.append(f"不一致：Markdown={'有' if md_exists else '无'}，"
                              f"block={blocks}")
            if chunks and vector_count is not None and vector_count < chunks:
                issues.append(f"索引缺失：MySQL chunk={chunks}，LanceDB={vector_count}")
        elif row is not None and row.deleted_at is not None:
            if pdf_exists:
                issues.append("已软删但文件仍在 data/papers/（应在 trash 中）")
            if vector_count:
                issues.append(f"已软删但向量库仍有 {vector_count} 条 chunk")
        entry["issues"] = issues
        report["issues"].extend([f"{pid}: {i}" for i in issues])
        return entry

    with session_scope() as sess:
        if paper_id:
            row = sess.get(Paper, paper_id)
            if row is None or (not include_deleted and row.deleted_at is not None):
                raise NotFoundError(f"论文不存在：{paper_id}")
            report["papers"].append(check_one(paper_id, row))
        else:
            query = sess.query(Paper)
            if not include_deleted:
                query = query.filter(Paper.deleted_at.is_(None))
            for row in query.all():
                report["papers"].append(check_one(row.id, row))
            # 目录里存在但库里没有的论文目录
            known = {p["paper_id"] for p in report["papers"]}
            for d in sorted(s.papers_dir.glob("*")):
                if d.is_dir() and d.name not in known:
                    report["issues"].append(f"{d.name}: 磁盘有目录但 MySQL 无记录")
    report["ok"] = not report["issues"]
    return report


# --------------------------------------------------------------------------
# 序列化
# --------------------------------------------------------------------------


def _to_dict(row: Paper) -> dict:
    data = {
        "id": row.id, "title": row.title, "authors": row.authors,
        "venue": row.venue, "year": row.year, "page_count": row.page_count,
        "status": row.status, "tags": [t for t in (row.tags or "").split(",") if t],
        "translate_done": row.translate_done, "translate_total": row.translate_total,
        # A 路（英文转录）的进度：与 translate_* 并列，两路并发时前端能各自显示
        "transcribe_done": row.transcribe_done,
        "transcribe_total": row.transcribe_total,
        "analyze_done": row.analyze_done, "analyze_total": row.analyze_total,
        "pdf_translate_done": row.pdf_translate_done,
        "pdf_translate_total": row.pdf_translate_total,
        "in_knowledge_base": bool(row.in_knowledge_base),
        "source_hash": row.source_hash,
        "abstract": row.abstract,
        # ---- 元数据扩展（2026-10-07）：列表字段一律给数组，空值给 None 让前端区分
        #      "还没有"（None/[]）与"读过、确实没有"（""）。----
        "abstract_zh": row.abstract_zh,
        "keywords_en": [t for t in (row.keywords_en or "").split(",") if t],
        "domain": row.domain,
        "one_liner": row.one_liner,
        "contributions": json_loads(row.contributions_json, []) or [],
        "prereq": json_loads(row.prereq_json, []) or [],
        "difficulty": row.difficulty,
        "read_time_min": row.read_time_min,
        "paper_type": row.paper_type,
        "doi": row.doi,
        "code_url": row.code_url,
        "meta_sources": json_loads(row.meta_sources_json, {}) or {},
        "meta_filled_at": iso_utc(row.meta_filled_at),
        "parse_summary": json_loads(row.parse_summary_json, {}) or {},
        # 时间一律走 `iso_utc`：库里是 UTC naive，直接 isoformat 前端会按本地时区解析（差 8 小时）
        "last_read_at": iso_utc(row.last_read_at),
        "created_at": iso_utc(row.created_at),
        "deleted_at": iso_utc(row.deleted_at),
    }
    return _align_with_disk(row, data)


# 正在跑逐页翻译的论文 id 缓存：{时刻, 集合}。查询很便宜但要被每篇论文的序列化调用，
# 所以缓存 3 秒，避免列表接口对同一个查询打 N 次。
_LIVE_CACHE: dict[str, object] = {"at": 0.0, "ids": set()}
_LIVE_TTL_S = 3.0


def _papers_being_translated() -> set[str]:
    """当前有 TRANSLATE_MD 任务在跑（排队/执行/暂停）的论文 id 集合。

    为什么必须看任务：任务在跑时**磁盘上还是上一轮的产物**（重新翻译时尤其明显），
    单纯"以磁盘为准"会把卡片改回「已翻译 22/22」，而任务其实刚开始 —— 正是用户反复
    反馈的"前后端状态对不上"。跑任务期间以库里/任务里的实时值为准。
    """
    import time

    now_ts = time.monotonic()
    if now_ts - float(_LIVE_CACHE["at"]) < _LIVE_TTL_S:  # type: ignore[arg-type]
        return _LIVE_CACHE["ids"]  # type: ignore[return-value]
    ids: set[str] = set()
    try:
        rows = task_service.list(type_="TRANSLATE_MD", limit=200)
        ids = {str(r["paper_id"]) for r in rows
               if r.get("paper_id") and str(r.get("status")) in ("pending", "processing", "paused")}
    except Exception as exc:  # noqa: BLE001 —— 查不到就退化成旧行为，别把列表接口打挂
        log.debug("查活跃翻译任务失败：%s", exc)
    _LIVE_CACHE["at"] = now_ts
    _LIVE_CACHE["ids"] = ids
    return ids


def _align_with_disk(row: Paper, data: dict) -> dict:
    """把 MySQL 里的统计**按磁盘真实情况**修正（只改返回值，不写库）。

    为什么需要：`data/papers/<id>/` 被手动删掉（测试/清理）后，MySQL 里的
    `page_count` / `translate_*` / `status` 仍是陈旧值 —— 论文卡片就会显示
    「继续翻译」+「11/22」这种与事实完全不符的状态（用户报"看的一头雾水"）。
    判据只有磁盘：解析产物 = `ir.sqlite` + `pages/`，翻译产物 = `md/pages.json`。

    例外：这篇论文**正在跑翻译任务**时不做翻译相关的修正（磁盘不是真相，任务才是）。
    """
    if row.deleted_at is not None:
        return data
    live = row.id in _papers_being_translated()
    try:
        paths = paper_paths(row.id)
        parsed = paths["ir"].is_file() and paths["pages"].is_dir()
        # 磁盘真相：有 B 路产物（中文翻译）就是"译过了"；只跑融合调用的老论文看旧格式文件
        translated = paths["path_b"].is_file() or paths["page_products"].is_file()
    except Exception:  # noqa: BLE001 —— 路径算不出来就按库里的值返回，不因统计口径把接口打挂
        return data
    if not parsed:
        data["page_count"] = 0
        data["translate_done"] = 0
        data["translate_total"] = 0
        if data.get("status") != "failed":
            data["status"] = "imported"
    elif not translated and not live:
        data["translate_done"] = 0
        data["translate_total"] = 0
        if data.get("status") in ("translating", "translated", "indexed"):
            data["status"] = "parsed"
    return data


def dumps(obj) -> str:
    return json_dumps(obj)
