"""论文元数据补全：把「离线从 PDF 读出来的」与「模型给的」元数据写回论文行。

用户口径（原话）：*论文元数据我想要么从论文获取自动填写，要么模型翻译后填写（和术语一同生成）*。
于是这里的两条来源分工明确：

- **离线**（``from_pdf``）：标题/作者/会议/年份/摘要都能从 PDF 里读，见
  ``backend/parsing/metadata.py``。导入与解析时**已经自动做过一次**，这个入口是给
  「论文库里那些历史数据 / 抽错了想重来一遍」用的；
- **模型**（``from_model``）：读不出来的部分（会议名没写在首页、扫描件没有文本层、
  缺中文摘要与标签）交给模型；常规翻译流程里**不额外多花一次调用** ——
  ``META_SCHEMA`` 被文档简报复用，模型生成术语表的同一次调用就把这些字段一起产出了。

写回策略：**只填空字段**（``overwrite=False``）。元数据没有"来源"标记，把用户手改过的值
覆盖掉是不可逆的损失；要强制重填就显式传 ``overwrite=True``（API 上的 `overwrite` 参数）。
"""

from __future__ import annotations

import re
from datetime import datetime

from backend.core.logging import get_logger
from backend.services.paper import paper_service

log = get_logger(__name__)

# 可补全的字段（标题单独处理：它更可能是用户手改过的）。
# 作者只在**离线**这一路出现 —— 模型看不到首页作者块，硬让它猜名字只会编造。
FILL_FIELDS = ("authors", "venue", "year", "tags", "abstract")
MISSING_FIELDS = ("venue", "year", "tags", "abstract")

# 模型这一次调用要产出的元数据。**与文档简报共用**（brief.BRIEF_SCHEMA 里嵌的就是它），
# 所以翻译流程里零额外成本地就把元数据补齐了。
META_SCHEMA = {
    "type": "object",
    "properties": {
        "venue": {"type": "string",
                  "description": "会议或期刊名（含年份，如 ICLR 2026）；文中没有就留空"},
        "year": {"type": "integer", "description": "发表年份；判断不出填 0"},
        "tags": {"type": "array", "description": "3~6 个简体中文关键词",
                 "items": {"type": "string"}},
        "abstract_zh": {"type": "string",
                        "description": "论文摘要的中文译文；抽样里没有摘要就留空"},
    },
}

META_SYSTEM = """你在整理一篇学术论文的元数据。只依据给定内容判断，**不要猜、不要编造**：
1. venue：会议或期刊名。只有文中明确出现时才填（如 "Published as a conference paper at ICLR 2026" 或页脚的 "Proceedings of ..."）；判断不出就留空字符串。
2. year：发表年份（整数）。文中能明确推出才填，否则填 0。
3. tags：3~6 个简体中文关键词，覆盖研究领域与核心方法。
4. abstract_zh：把论文摘要译成简体中文（≤400 字）。抽样内容里没有摘要就留空字符串 —— 不要用一句话总结冒充摘要。
只输出 JSON，不要输出解释或 Markdown 代码块。"""


# --------------------------------------------------------------------------
# 写回
# --------------------------------------------------------------------------


def _is_empty(value) -> bool:
    return value in (None, "", 0, [], ())


def apply_fills(paper_id: str, fills: dict, *, overwrite: bool = False) -> dict:
    """把 ``fills`` 写回论文行；默认**只填空字段**，返回真正写进去的字段。"""
    clean = {k: v for k, v in (fills or {}).items()
             if k in FILL_FIELDS and not _is_empty(v)}
    if not clean:
        return {}
    paper = paper_service.get_paper(paper_id)
    patch = {k: v for k, v in clean.items() if overwrite or _is_empty(paper.get(k))}
    if patch:
        paper_service.update_paper(paper_id, patch)
        log.info("元数据补全 %s：%s", paper_id,
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


def missing_fields(paper: dict) -> list[str]:
    return [k for k in MISSING_FIELDS if _is_empty(paper.get(k))]


# --------------------------------------------------------------------------
# 来源 ①：PDF（离线）
# --------------------------------------------------------------------------


def from_pdf(paper_id: str, *, overwrite: bool = False) -> dict:
    """读 ``source.pdf`` 补元数据（标题只在"明显不对"时才改）。"""
    import pymupdf as fitz

    from backend.parsing.metadata import extract_paper_meta

    path = paper_service.resolve_pdf(paper_id)
    with fitz.open(str(path)) as doc:
        meta = extract_paper_meta(doc, path)

    paper = paper_service.get_paper(paper_id)
    fills = {k: v for k, v in meta.fills().items() if k in FILL_FIELDS}
    applied = apply_fills(paper_id, fills, overwrite=overwrite)

    title = (meta.title or "").strip()
    if title and (overwrite or _title_needs_fix(paper.get("title") or "", paper_id)):
        paper_service.update_paper(paper_id, {"title": title[:512]})
        applied["title"] = title[:512]
    return {"fills": fills, "applied": applied,
            "sources": {k: f"pdf:{v}" for k, v in (meta.sources or {}).items()}}


# --------------------------------------------------------------------------
# 来源 ②：模型（与术语同一次调用）
# --------------------------------------------------------------------------


def normalize_meta(data: dict) -> dict:
    """模型返回的 meta 块 → 能直接写回论文行的字段（脏值一律丢掉）。"""
    out: dict = {}
    venue = str((data or {}).get("venue") or "").strip()
    if len(venue) >= 2:
        out["venue"] = venue[:128]
    try:
        year = int((data or {}).get("year") or 0)
    except (TypeError, ValueError):
        year = 0
    if 1950 <= year <= datetime.now().year + 1:
        out["year"] = year
    tags = [str(t).strip() for t in ((data or {}).get("tags") or []) if str(t).strip()]
    if tags:
        out["tags"] = ",".join(dict.fromkeys(tags))[:256]
    abstract = str((data or {}).get("abstract_zh") or "").strip()
    if len(abstract) >= 60:               # 太短的"摘要"多半是模型随手写的一句话，不如不填
        out["abstract"] = abstract[:2000]
    return out


def model_meta(paper_id: str, *, provider=None) -> dict:
    """让模型读一遍抽样内容，产出 venue/year/tags/abstract_zh（失败返回空 dict）。"""
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
    applied = apply_fills(paper_id, fills, overwrite=overwrite)
    return {"fills": fills, "applied": applied, "sources": {k: "model" for k in applied}}


# --------------------------------------------------------------------------
# 编排：API / UI 用的"一键补全"
# --------------------------------------------------------------------------


def auto(paper_id: str, *, model: bool = False, overwrite: bool = False,
         provider=None) -> dict:
    """先离线读 PDF，仍缺的字段（且 ``model=True``）再问一次模型。"""
    report: dict = {"paper_id": paper_id, "applied": {}, "sources": {}, "errors": []}
    try:
        pdf = from_pdf(paper_id, overwrite=overwrite)
        report["applied"].update(pdf["applied"])
        report["sources"].update(pdf["sources"])
    except Exception as exc:  # noqa: BLE001
        log.warning("从 PDF 补元数据失败 %s：%s", paper_id, exc)
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
