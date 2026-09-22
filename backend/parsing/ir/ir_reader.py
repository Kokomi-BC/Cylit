"""IR 读取器：调试 / 审计 / 增量重跑用（**不是运行时依赖**）。"""

from __future__ import annotations

import sqlite3
from pathlib import Path

from backend.core.utils import json_loads


class IRReader:
    def __init__(self, path: str | Path) -> None:
        self.path = Path(path)

    @property
    def exists(self) -> bool:
        return self.path.is_file()

    def _rows(self, sql: str, args: tuple = ()) -> list[dict]:
        if not self.exists:
            return []
        conn = sqlite3.connect(str(self.path))
        conn.row_factory = sqlite3.Row
        try:
            rows = conn.execute(sql, args).fetchall()
        except sqlite3.Error:
            return []
        finally:
            conn.close()
        return [dict(r) for r in rows]

    # ------------------------------------------------------------------
    def paper(self) -> dict:
        rows = self._rows("SELECT * FROM paper LIMIT 1")
        return rows[0] if rows else {}

    def pages(self) -> list[dict]:
        return self._rows("SELECT * FROM page ORDER BY page_no")

    def blocks(self, page_no: int | None = None) -> list[dict]:
        if page_no is None:
            return self._rows("SELECT * FROM block ORDER BY order_global")
        return self._rows("SELECT * FROM block WHERE page_no = ? ORDER BY order_global",
                          (page_no,))

    def block(self, block_id: str) -> dict | None:
        rows = self._rows("SELECT * FROM block WHERE id = ?", (block_id,))
        return rows[0] if rows else None

    def formulas(self, grade: str | None = None) -> list[dict]:
        if grade:
            return self._rows("SELECT * FROM formula WHERE grade = ? ORDER BY page_number",
                              (grade,))
        return self._rows("SELECT * FROM formula ORDER BY page_number")

    def figures(self) -> list[dict]:
        return self._rows("SELECT * FROM figure ORDER BY page_number")

    def tables(self) -> list[dict]:
        return self._rows("SELECT * FROM tbl ORDER BY page_number")

    def terms(self) -> list[dict]:
        return self._rows("SELECT * FROM term ORDER BY scope, source_term")

    def rules(self) -> list[dict]:
        return self._rows("SELECT * FROM rule")

    def builds(self) -> list[dict]:
        return self._rows("SELECT * FROM build ORDER BY id DESC LIMIT 50")

    # ------------------------------------------------------------------
    def reader_snapshot(self) -> dict:
        """静态降级用的 reader.json 快照（规划 §6.3）。"""
        paper = self.paper()
        blocks = []
        for r in self.blocks():
            blocks.append({
                "id": r["id"], "page": r["page_no"], "type": r["type"],
                "col": r["col"], "order": r["order_global"],
                "bbox": [r["x0"], r["y0"], r["x1"], r["y1"]],
                "text": r["text_clean"] or r["text"], "anchor": r["md_anchor"],
                "meta": json_loads(r.get("meta_json"), {}) or {},
            })
        formulas = {}
        for f in self.formulas():
            formulas[f["block_id"]] = {
                "latex_candidate": json_loads(f["latex_candidate"], []),
                "latex_repaired": f["latex_repaired"],
                "latex_final": f["latex_final"],
                "grade": f["grade"], "confidence": f["confidence"],
                "crop": f["image_path"], "raw_text": f["raw_text"],
                "vector_audit": json_loads(f["vector_data_json"], {}) or {},
                "validation": json_loads(f["validation_json"], {}) or {},
                "status": f["status"],
            }
        figures = [{"block_id": f["block_id"], "page": f["page_number"],
                    "caption": f["caption"], "image": f["image_path"],
                    "bbox": [f["x0"], f["y0"], f["x1"], f["y1"]],
                    "status": f["status"]} for f in self.figures()]
        return {
            "paper": paper, "pages": self.pages(), "blocks": blocks,
            "formulas": formulas, "figures": figures,
        }

    def stats(self) -> dict:
        return {
            "pages": len(self.pages()), "blocks": len(self.blocks()),
            "formulas": len(self.formulas()), "figures": len(self.figures()),
            "tables": len(self.tables()),
        }
