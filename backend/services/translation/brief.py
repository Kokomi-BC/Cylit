"""文档简报（Document Brief）：每篇论文**只调用一次**，产出全文脉络。

**为什么需要它**：块级/页级翻译的模型视角都很局部 —— 单次调用只看得到当前页
（或当前段）与前后邻段。缺的是"这篇论文在讲什么、属于哪个领域、全文该怎么统一
术语"。把这些上下文**固化成一个短简报**并在每次调用时逐字复用，效果是：

- 术语从"每块独立决策"变成"全文统一决策"，直接压住 R4（术语漂移）风险；
- 因为简报在同一篇论文内**字节级不变**，它可以放在 prompt 最前面，
  命中 provider 的 **prompt prefix cache**（DeepSeek 命中部分约 0.1× 计费）；
- 简报落盘为 ``translation_brief.json``，可人工编辑后重跑（人工术语优先于模型）。

禁用与降级：``--no-brief`` 时不生成，模板自动退化为"仅章节 + 术语表"。
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path

from backend.core.errors import ProviderError
from backend.core.logging import get_logger
from backend.core.utils import json_dumps, read_json, sha256_text, write_json
from backend.services.paper import paper_service, reader_service

log = get_logger(__name__)

BRIEF_FILENAME = "translation_brief.json"
BRIEF_VERSION = 1
# 简报输入抽样上限（字符）：标题 + 摘要 + 各章节开头
SAMPLE_LIMIT = 6000

BRIEF_SYSTEM = """你是学术论文翻译项目的术语负责人。请阅读给定论文的标题、摘要与章节开头，
产出一份**供后续逐页翻译使用的简报**，用简体中文填写。

要求：
1. domain：用一句话说明论文领域与核心方法（≤40 字）。
2. glossary：列出 8~20 个**必须在全文统一译法**的关键术语，给出英文原词与推荐中文译法。
   - 已是通用缩写、业界惯例不译的（如 Transformer、LoRA、QJL、BERT）请在 keep_as_is 中列出；
   - 只列这篇论文里反复出现、且译法确实需要统一的词，不要罗列普通词汇。
3. style：语体说明（如"学术论文正文，简洁书面语，公式与符号保留"）。
4. notes：2~4 条翻译注意点（符号约定、专有名词、易错点）。
5. 只输出 JSON，不要输出解释或 Markdown 代码块。"""

BRIEF_SCHEMA = {
    "type": "object",
    "properties": {
        "domain": {"type": "string", "description": "论文领域与核心方法（≤40 字）"},
        "glossary": {
            "type": "array",
            "description": "必须统一译法的术语",
            "items": {
                "type": "object",
                "properties": {
                    "en": {"type": "string"},
                    "zh": {"type": "string"},
                },
                "required": ["en", "zh"],
            },
        },
        "keep_as_is": {
            "type": "array",
            "description": "保持英文原样不译的词/缩写",
            "items": {"type": "string"},
        },
        "style": {"type": "string", "description": "语体说明"},
        "notes": {
            "type": "array",
            "description": "翻译注意点",
            "items": {"type": "string"},
        },
    },
    "required": ["domain"],
}


@dataclass
class DocumentBrief:
    paper_id: str
    title: str = ""
    domain: str = ""
    glossary: dict[str, str] = field(default_factory=dict)
    keep_as_is: list[str] = field(default_factory=list)
    style: str = ""
    notes: list[str] = field(default_factory=list)
    source: str = "model"          # model | manual | cached | none
    model_id: str | None = None
    prompt_hash: str = ""
    cache_hit_tokens: int = 0

    @property
    def enabled(self) -> bool:
        return bool(self.domain or self.glossary)

    def prompt_block(self) -> str:
        """渲染成注入每个翻译 prompt 的稳定文本块（**必须字节级稳定**）。

        刻意不使用时间戳、随机数或统计数字，以保证 provider 前缀缓存命中。
        """
        if not self.enabled:
            return ""
        lines = ["【论文背景（全文统一口径，翻译时必须遵守）】"]
        if self.domain:
            lines.append(f"领域与核心方法：{self.domain}")
        lines.append(f"标题：{self.title}")
        if self.glossary:
            items = "；".join(f"{en}→{zh}" for en, zh in self.glossary.items())
            lines.append(f"统一译法：{items}")
        if self.keep_as_is:
            lines.append(f"保持英文不译：{'、'.join(self.keep_as_is)}")
        if self.style:
            lines.append(f"语体：{self.style}")
        for n in self.notes:
            lines.append(f"注意：{n}")
        return "\n".join(lines)

    def to_dict(self, include_prompt: bool = False) -> dict:
        d = {
            "paper_id": self.paper_id, "title": self.title, "domain": self.domain,
            "glossary": self.glossary, "keep_as_is": self.keep_as_is,
            "style": self.style, "notes": self.notes, "source": self.source,
            "model_id": self.model_id, "version": BRIEF_VERSION,
        }
        if include_prompt:
            d["prompt_block"] = self.prompt_block()
        return d

    @staticmethod
    def from_dict(d: dict) -> "DocumentBrief":
        return DocumentBrief(
            paper_id=d.get("paper_id", ""), title=d.get("title", ""),
            domain=d.get("domain", ""),
            glossary=dict(d.get("glossary") or {}),
            keep_as_is=list(d.get("keep_as_is") or []),
            style=d.get("style", ""), notes=list(d.get("notes") or []),
            source=d.get("source", "cached"), model_id=d.get("model_id"),
        )


# --------------------------------------------------------------------------
# 落盘 / 读取
# --------------------------------------------------------------------------


def brief_path(paper_id: str) -> Path:
    return paper_service.paper_paths(paper_id)["root"] / "md" / BRIEF_FILENAME


def load_brief(paper_id: str) -> DocumentBrief | None:
    p = brief_path(paper_id)
    data = read_json(p, None) if p.is_file() else None
    if not isinstance(data, dict):
        return None
    return DocumentBrief.from_dict(data)


def save_brief(brief: DocumentBrief) -> Path:
    return write_json(brief_path(brief.paper_id), brief.to_dict())


# --------------------------------------------------------------------------
# 生成
# --------------------------------------------------------------------------


def sample_paper_text(paper_id: str, limit: int = SAMPLE_LIMIT) -> str:
    """抽样：标题 + 摘要 + 各章节标题 + 每节开头若干块（控制输入长度）。"""
    paper = paper_service.get_paper(paper_id)
    md_map = reader_service.load_markdown_map(paper_id)
    from backend.repositories.mysql.models import Block
    from backend.repositories.mysql.session import session_scope

    with session_scope() as sess:
        blocks = (sess.query(Block)
                  .filter(Block.paper_id == paper_id)
                  .order_by(Block.order_global.asc()).all())
        rows = [{"id": b.id, "type": b.type} for b in blocks]

    parts: list[str] = [f"标题：{paper.get('title') or ''}"]
    if paper.get("abstract"):
        parts.append(f"摘要：{paper['abstract']}")
    budget = limit
    section_head = ""
    for row in rows:
        text = (md_map.get(row["id"], {}) or {}).get("text", "")
        if not text:
            continue
        if row["type"] == "heading":
            section_head = text
            parts.append(f"\n## {text}")
            continue
        if row["type"] in ("ref", "figure", "table"):
            continue
        # 只取每个章节的开头若干内容，避免把整篇塞进简报调用
        chunk = text[:900]
        if section_head:
            parts.append(chunk)
            section_head = ""
        else:
            continue
        budget = limit - sum(len(p) for p in parts)
        if budget <= 0:
            break
    text = "\n".join(parts)
    return text[:limit]


def generate_brief(paper_id: str, *, provider=None, force: bool = False,
                   max_terms: int = 24) -> DocumentBrief:
    """生成简报（已有且非 force 时直接复用缓存）。"""
    if not force:
        cached = load_brief(paper_id)
        if cached is not None and cached.enabled:
            cached.source = "cached"
            return cached

    from backend.services.model.model_manager import model_manager
    from backend.services.translation.translation_service import load_glossary

    paper = paper_service.get_paper(paper_id)
    provider = provider or model_manager.get_generation()
    sample = sample_paper_text(paper_id)
    # 已有人工/IR 术语表优先并入（人工术语优先于模型）
    existing = load_glossary(paper_id, limit=max_terms)

    prompt = (
        f"以下是论文《{paper.get('title') or paper_id}》的抽样内容，请产出翻译简报。\n\n"
        f"{sample}\n\n"
        + ("已有的术语表（优先采用这些译法）：\n"
           + "\n".join(f"- {t['source_term']} → {t['target_term']}" for t in existing)
           if existing else "")
    )
    data: dict = {}
    try:
        if hasattr(provider, "structured"):
            data = provider.structured(prompt, BRIEF_SCHEMA, system=BRIEF_SYSTEM) or {}
        else:  # pragma: no cover
            data = {}
    except ProviderError as exc:
        log.warning("简报生成失败（降级为无简报模式）：%s", exc)
        data = {}

    glossary: dict[str, str] = {}
    for item in (data.get("glossary") or [])[:max_terms]:
        if not isinstance(item, dict):
            continue
        en = str(item.get("en") or "").strip()
        zh = str(item.get("zh") or "").strip()
        if en and zh:
            glossary[en] = zh
    for t in existing:                     # 人工/IR 术语覆盖模型
        glossary.setdefault(t["source_term"], t["target_term"])
    glossary = dict(list(glossary.items())[:max_terms])

    brief = DocumentBrief(
        paper_id=paper_id, title=paper.get("title") or "",
        domain=str(data.get("domain") or "").strip(),
        glossary=glossary,
        keep_as_is=[str(x) for x in (data.get("keep_as_is") or [])][:20],
        style=str(data.get("style") or "").strip(),
        notes=[str(x) for x in (data.get("notes") or [])][:6],
        source="model" if data else "none",
        model_id=_model_id(),
        prompt_hash=sha256_text(prompt)[:16],
    )
    if brief.enabled:
        save_brief(brief)
    return brief


def _model_id() -> str | None:
    from backend.services.model.model_manager import model_manager

    try:
        return model_manager.get_config(None, "generation").get("id")
    except Exception:  # noqa: BLE001
        return None


def dumps(brief: DocumentBrief) -> str:
    return json_dumps(brief.to_dict(include_prompt=True), indent=2)


def as_json(brief: DocumentBrief) -> str:  # pragma: no cover - 调试辅助
    return json.dumps(brief.to_dict(include_prompt=True), ensure_ascii=False, indent=2)
