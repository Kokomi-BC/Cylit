"""IR 写入器：把一次解析的结果落成每篇一个 SQLite。"""

from __future__ import annotations

import sqlite3
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator

from backend.core.logging import get_logger
from backend.core.utils import json_dumps, now_iso, sha256_file
from backend.parsing.ir.rebind import BlockFingerprint
from backend.parsing.models import FormulaBlock, ParseResult

log = get_logger(__name__)
SCHEMA_PATH = Path(__file__).resolve().parent / "schema.sql"
PARSER_VERSION = "0.1.0"


def load_schema_sql() -> str:
    return SCHEMA_PATH.read_text(encoding="utf-8")


class IRWriter:
    """IR SQLite 写入器（可重复构建：先清空同名论文数据再写）。"""

    def __init__(self, path: str | Path, paper_id: str | None = None) -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.paper_id = paper_id

    @contextmanager
    def connect(self) -> Iterator[sqlite3.Connection]:
        conn = sqlite3.connect(str(self.path))
        conn.row_factory = sqlite3.Row
        try:
            conn.executescript(load_schema_sql())
            conn.execute("PRAGMA foreign_keys = ON")
            yield conn
            conn.commit()
        finally:
            conn.close()

    # ------------------------------------------------------------------
    def read_existing_fingerprints(self) -> list[BlockFingerprint]:
        """读取已有 IR 的块指纹（用于 block_id 稳定重绑定）。"""
        if not self.path.is_file():
            return []
        try:
            conn = sqlite3.connect(str(self.path))
            conn.row_factory = sqlite3.Row
            rows = conn.execute(
                "SELECT id, page_no, x0, y0, x1, y1, type, text_clean, text, content_hash "
                "FROM block"
            ).fetchall()
            conn.close()
        except sqlite3.Error as exc:
            log.warning("读取旧 IR 失败（忽略重绑定）：%s", exc)
            return []
        return [BlockFingerprint.from_row(dict(r)) for r in rows]

    # ------------------------------------------------------------------
    def write(self, result: ParseResult, *, source_pdf: str | Path | None = None,
              reset: bool = True) -> dict:
        """写入完整解析结果，返回统计。"""
        pid = result.paper_id
        with self.connect() as conn:
            if reset:
                # 按依赖顺序清空（figure/tbl/formula 依赖 block 主键；build 无 paper_id 列）
                conn.execute("DELETE FROM formula WHERE paper_id = ?", (pid,))
                conn.execute("DELETE FROM figure WHERE paper_id = ?", (pid,))
                conn.execute("DELETE FROM tbl WHERE paper_id = ?", (pid,))
                conn.execute("DELETE FROM block WHERE paper_id = ?", (pid,))
                conn.execute("DELETE FROM page")
                conn.execute("DELETE FROM paper WHERE paper_id = ?", (pid,))
                conn.execute("DELETE FROM term WHERE paper_id = ?", (pid,))
                conn.execute("DELETE FROM rule WHERE paper_id = ?", (pid,))
                conn.execute("DELETE FROM build WHERE paper_id = ?", (pid,))

            # paper
            pages = result.pages
            conn.execute(
                """INSERT OR REPLACE INTO paper
                   (paper_id, title, authors, venue, year, page_count, source_path,
                    source_hash, page_width, page_height, is_two_column,
                    parser_version, schema_version, built_at, stats_json)
                   VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (pid, result.title, result.authors, result.stats.get("venue"),
                 result.stats.get("year"), len(pages),
                 str(source_pdf) if source_pdf else None,
                 sha256_file(source_pdf) if source_pdf and Path(source_pdf).is_file() else None,
                 pages[0].width if pages else None, pages[0].height if pages else None,
                 1 if any(p.columns > 1 for p in pages) else 0,
                 PARSER_VERSION, 1, now_iso(), json_dumps(result.stats)),
            )

            # page
            conn.executemany(
                """INSERT OR REPLACE INTO page
                   (page_no, width, height, rotation, columns, gutter_x, image_path,
                    text_chars, block_count, has_text_layer)
                   VALUES (?,?,?,?,?,?,?,?,?,?)""",
                [(p.page_no, p.width, p.height, p.rotation, p.columns, p.gutter_x,
                  p.image_path, p.text_chars, p.block_count,
                  1 if p.has_text_layer else 0) for p in pages],
            )

            # block
            conn.executemany(
                """INSERT OR REPLACE INTO block
                   (id, paper_id, page_no, order_in_page, order_global, type, col,
                    x0, y0, x1, y1, text, text_clean, source_text, source_kind,
                    font_names, font_size, line_count, char_count, content_hash,
                    md_anchor, section_id, chunk_id, is_inline, meta_json, created_at)
                   VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                [(b.to_ir_dict()["id"], pid, b.page_no, b.order_in_page, b.order_global,
                  b.type, b.col, b.rect.x0, b.rect.y0, b.rect.x1, b.rect.y1,
                  b.text, b.text_clean, b.source_text, b.source_kind,
                  json_dumps(b.font_names), b.font_size, b.line_count, b.char_count,
                  b.content_hash, b.md_anchor, b.section_id, b.chunk_id,
                  1 if b.is_inline else 0,
                  json_dumps(b.meta) if b.meta else None, now_iso())
                 for b in result.blocks],
            )

            # formula
            conn.executemany(
                """INSERT OR REPLACE INTO formula
                   (block_id, paper_id, page_number, x0, y0, x1, y1, raw_text,
                    image_path, vector_data_json, latex_candidate, latex_repaired,
                    latex_final, confidence, grade, source, status, validation_json,
                    review_note, is_inline)
                   VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                [tuple(f.to_ir_dict().values()) for f in result.formulas],
            )

            # figure
            conn.executemany(
                """INSERT OR REPLACE INTO figure
                   (block_id, paper_id, page_number, x0, y0, x1, y1, image_path,
                    caption_block_id, caption, width_px, height_px, effective_dpi,
                    edge_ink, subfigure_count, aspect_ratio, audit_json, status)
                   VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                [tuple(f.to_ir_dict().values()) for f in result.figures],
            )

            # table
            conn.executemany(
                """INSERT OR REPLACE INTO tbl
                   (block_id, paper_id, page_number, x0, y0, x1, y1, caption_block_id,
                    caption, image_path, row_count, col_count, has_borders, grid_json,
                    status)
                   VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                [tuple(t.to_ir_dict().values()) for t in result.tables],
            )

            # build 记录
            conn.execute(
                """INSERT INTO build (paper_id, stage, status, started_at, finished_at,
                                      duration_ms, counts_json, message)
                   VALUES (?,?,?,?,?,?,?,?)""",
                (pid, "PARSE", "completed", now_iso(), now_iso(), result.duration_ms,
                 json_dumps(result.counts()), "; ".join(result.warnings[:5]) or None),
            )

        return {"ir_path": str(self.path), "counts": result.counts()}

    # ------------------------------------------------------------------
    def write_terms(self, terms: list[dict]) -> int:
        if not terms:
            return 0
        with self.connect() as conn:
            conn.executemany(
                """INSERT OR REPLACE INTO term
                   (paper_id, scope, source_term, target_term, note, hit_count, created_at)
                   VALUES (?,?,?,?,?,?,?)""",
                [(t.get("paper_id"), t.get("scope", "paper"), t["source_term"],
                  t["target_term"], t.get("note"), t.get("hit_count", 0), now_iso())
                 for t in terms],
            )
        return len(terms)

    def write_rules(self, rules: list[dict]) -> int:
        """写一致性常量规则表（规划 §13.3 C2 的 rule 表）。"""
        if not rules:
            return 0
        with self.connect() as conn:
            conn.executemany(
                """INSERT OR REPLACE INTO rule
                   (paper_id, kind, symbol, value, value_float, tolerance,
                    source_block, note)
                   VALUES (?,?,?,?,?,?,?,?)""",
                [(r.get("paper_id"), r.get("kind", "constant"), r.get("symbol"),
                  str(r.get("value")) if r.get("value") is not None else None,
                  r.get("value_float"), r.get("tolerance"), r.get("source_block"),
                  r.get("note")) for r in rules],
            )
        return len(rules)


def formula_ir_tuple(f: FormulaBlock) -> tuple:
    d = f.to_ir_dict()
    return tuple(d.values())
