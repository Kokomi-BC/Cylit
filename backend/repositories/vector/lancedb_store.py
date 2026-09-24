"""向量库（LanceDB 嵌入式，规划 §11、§7）。

**LanceDB 是可丢的**（规划 §6.2 第 4 条）：任何时刻可全量重建，语义不变。
因此本模块只做「写入 / 删除 / 计数 / 检索」，不含任何业务状态。
未安装 lancedb 时自动降级为不可用（``available=False``），不影响主链路。
"""

from __future__ import annotations

import threading
from pathlib import Path

from backend.core.config import get_settings
from backend.core.logging import get_logger
from backend.core.utils import json_loads

log = get_logger(__name__)

TABLE_NAME = "paper_chunks"
# 词法检索 / 按 id 取行需要的列（按 schema 取交集，见 LanceDBStore._select）
_ROW_FIELDS = ["chunk_id", "paper_id", "chunk_type", "page_no", "block_id",
               "section", "lang", "content", "metadata_json"]


def _row_out(r: dict) -> dict:
    return {**{k: r.get(k) for k in
               ("chunk_id", "paper_id", "chunk_type", "page_no", "block_id",
                "section", "content")},
            "lang": r.get("lang") or "en",
            "metadata": json_loads(r.get("metadata_json"), {}) or {}}


class LanceDBStore:
    """薄封装：线程安全（LanceDB 句柄不保证多线程写安全）。"""

    def __init__(self, path: str | Path | None = None) -> None:
        self._path = Path(path) if path else None
        self._db = None
        self._lock = threading.RLock()
        self._error: str | None = None

    @property
    def path(self) -> Path:
        if self._path is None:
            s = get_settings()
            self._path = s.index_dir / "lancedb"
        return self._path

    @property
    def available(self) -> bool:
        try:
            self._connect()
            return True
        except Exception:  # noqa: BLE001
            return False

    def _connect(self):
        if self._db is not None:
            return self._db
        with self._lock:
            if self._db is not None:
                return self._db
            try:
                import lancedb
            except ImportError as exc:
                self._error = "未安装 lancedb"
                raise RuntimeError(self._error) from exc
            self.path.mkdir(parents=True, exist_ok=True)
            self._db = lancedb.connect(str(self.path))
            return self._db

    def _table(self):
        db = self._connect()
        names = set(db.table_names())
        if TABLE_NAME not in names:
            return None
        return db.open_table(TABLE_NAME)

    def columns(self) -> set[str]:
        """当前表的列名（旧索引没有 ``lang`` 列 —— 本轮之后才加的）。"""
        table = self._table()
        if table is None:
            return set()
        try:
            return {f.name for f in table.schema}
        except Exception:  # noqa: BLE001
            return set()

    def has_lang(self) -> bool:
        return "lang" in self.columns()

    def _select(self, wanted: list[str]) -> list[str]:
        """只 select 表里真有的列。

        ★ 踩过的坑：旧索引没有 ``lang`` 列，``select([... "lang" ...])`` 会直接报错，
        而调用方是"失败就返回空列表"的语义 → **整条词法检索静默失效**
        （实测：查询词明明在正文里，`lexical_hits` 还是 0）。所以这里按 schema 取交集。
        """
        have = self.columns()
        if not have:
            return wanted
        return [c for c in wanted if c in have]

    # ------------------------------------------------------------------
    def upsert_chunks(self, chunks: list[dict], vectors: list[list[float]],
                      index_version: int, *, embedding_model_id: str | None = None
                      ) -> int:
        """写入/覆盖 chunk 向量。返回写入条数。"""
        if not chunks:
            return 0
        if len(chunks) != len(vectors):
            raise ValueError("chunks 与 vectors 数量不一致")
        rows = []
        for c, v in zip(chunks, vectors):
            rows.append({
                "chunk_id": c["id"], "paper_id": c["paper_id"],
                "chunk_type": c["chunk_type"], "page_no": int(c["page_no"]),
                "order_global": int(c["order_global"]),
                "block_id": c.get("block_id") or "",
                "section": c.get("section") or "",
                "lang": c.get("lang") or "en",
                "content": c["content"], "content_hash": c["content_hash"],
                "index_version": int(index_version),
                "embedding_model_id": embedding_model_id or "",
                "metadata_json": _dumps_meta(c.get("metadata")),
                "vector": list(map(float, v)),
            })
        with self._lock:
            import pyarrow as pa

            db = self._connect()
            tbl = pa.Table.from_pylist(rows)
            names = set(db.table_names())
            if TABLE_NAME in names:
                table = db.open_table(TABLE_NAME)
                why = _schema_mismatch(table, rows[0])
                if why:
                    # 换 embedding 模型（维度变）或加字段（本轮加 lang）都会让表结构与
                    # 新数据对不上。向量库是**派生数据、任何时刻可全量重建**
                    # （规划 §6.2 第 4 条），所以直接丢表重建，不静默写坏索引。
                    log.warning("LanceDB 表结构变化（%s）：丢弃并重建 %s", why, TABLE_NAME)
                    db.drop_table(TABLE_NAME)
                    names.discard(TABLE_NAME)
            if TABLE_NAME in names:
                table = db.open_table(TABLE_NAME)
                # 先按 chunk_id 删除旧行，再追加（LanceDB 无原生 upsert）
                ids = [c["id"] for c in chunks]
                try:
                    table.delete(_where_in("chunk_id", ids))
                except Exception:  # noqa: BLE001
                    pass
                table.add(tbl)
            else:
                db.create_table(TABLE_NAME, data=tbl)
        log.info("LanceDB 写入 %d 条 chunk（index_version=%d）", len(rows), index_version)
        return len(rows)

    def delete_paper(self, paper_id: str) -> int:
        """按 paper_id 删除全部 chunk（删除流程第 2 步，规划 §21.12）。"""
        n = self.count(paper_id=paper_id)
        if n == 0:
            return 0
        with self._lock:
            table = self._table()
            if table is None:
                return 0
            table.delete(f"paper_id = '{_escape(paper_id)}'")
        log.info("LanceDB 删除论文 %s 的 %d 条 chunk", paper_id, n)
        return n

    def delete_chunks(self, chunk_ids: list[str]) -> int:
        if not chunk_ids:
            return 0
        with self._lock:
            table = self._table()
            if table is None:
                return 0
            table.delete(_where_in("chunk_id", chunk_ids))
        return len(chunk_ids)

    def count(self, paper_id: str | None = None) -> int:
        table = self._table()
        if table is None:
            return 0
        try:
            if paper_id:
                return table.count_rows(f"paper_id = '{_escape(paper_id)}'")
            return table.count_rows()
        except TypeError:  # 老版本 API
            df = table.to_pandas()
            return int((df["paper_id"] == paper_id).sum()) if paper_id else len(df)

    def stats(self) -> dict:
        try:
            db = self._connect()
            names = list(db.table_names())
            n = self.count() if TABLE_NAME in names else 0
            return {"available": True, "path": str(self.path), "tables": names,
                    "chunks": n}
        except Exception as exc:  # noqa: BLE001
            return {"available": False, "path": str(self.path),
                    "error": f"{type(exc).__name__}: {str(exc)[:200]}"}

    # ------------------------------------------------------------------
    def search(self, vector: list[float], *, paper_ids: list[str] | None = None,
               chunk_types: list[str] | None = None, langs: list[str] | None = None,
               top_k: int = 20,
               exclude_paper: str | None = None,
               exclude_chunk: str | None = None) -> list[dict]:
        """向量检索（规划 §19.1 VectorRetriever 的底层）。"""
        table = self._table()
        if table is None:
            return []
        try:
            q = table.search(list(map(float, vector))).limit(max(1, top_k))
            filters = []
            if paper_ids:
                filters.append(_where_in("paper_id", paper_ids))
            if chunk_types:
                filters.append(_where_in("chunk_type", chunk_types))
            if langs:
                if self.has_lang():
                    filters.append(_where_in("lang", langs))
                else:
                    # 旧索引没有 lang 列：**宁可不筛，也不返回空**（上层会在
                    # retrieval.notes 里说明"语言过滤未生效，请重建索引"）
                    log.warning("索引里没有 lang 列（旧索引）：本次忽略语言过滤 %s", langs)
            if exclude_paper:
                filters.append(f"paper_id != '{_escape(exclude_paper)}'")
            if exclude_chunk:
                filters.append(f"chunk_id != '{_escape(exclude_chunk)}'")
            if filters:
                q = q.where(" AND ".join(filters), prefilter=True)
            rows = q.to_list()
        except Exception as exc:  # noqa: BLE001
            log.warning("LanceDB 检索失败：%s", exc)
            return []
        out = []
        for r in rows:
            out.append({
                "chunk_id": r.get("chunk_id"), "paper_id": r.get("paper_id"),
                "chunk_type": r.get("chunk_type"), "page_no": r.get("page_no"),
                "block_id": r.get("block_id"), "section": r.get("section"),
                "lang": r.get("lang") or "en",
                "content": r.get("content"),
                "score": r.get("_distance"),
                "metadata": json_loads(r.get("metadata_json"), {}) or {},
            })
        return out

    def get_rows(self, chunk_ids: list[str]) -> list[dict]:
        """按 chunk_id 取行（"以某段为查询"的相似检索要用）。"""
        table = self._table()
        if table is None or not chunk_ids:
            return []
        try:
            rows = (table.search().select(self._select(_ROW_FIELDS))
                    .where(_where_in("chunk_id", chunk_ids))
                    .limit(len(chunk_ids)).to_list())
        except Exception as exc:  # noqa: BLE001
            log.warning("LanceDB 按 id 读取失败：%s", exc)
            return []
        return [_row_out(r) for r in rows]

    def chunk_ids(self, *, paper_id: str | None = None) -> list[str]:
        """只取 chunk_id（对账用：不读正文，几千条也很轻）。"""
        table = self._table()
        if table is None:
            return []
        try:
            fields = self._select(["chunk_id", "paper_id"])
            q = table.search().select(fields)
            if paper_id and "paper_id" in fields:
                q = q.where(f"paper_id = '{_escape(paper_id)}'")
            rows = q.limit(1000000).to_list()
        except Exception as exc:  # noqa: BLE001
            log.warning("LanceDB 读取 chunk_id 失败：%s", exc)
            return []
        return [str(r.get("chunk_id")) for r in rows if r.get("chunk_id")]

    def all_rows(self, *, paper_ids: list[str] | None = None) -> list[dict]:
        """整表扫描（词法检索用：本库规模只有几千条，Python 侧 BM25 足够快）。

        真正的全文索引（LanceDB FTS / SQLite FTS5）留到"库大到扫不动"再上，
        现在多一层外部索引只会多一处不一致。
        """
        table = self._table()
        if table is None:
            return []
        try:
            q = table.search().select(self._select(_ROW_FIELDS))
            if paper_ids:
                q = q.where(_where_in("paper_id", paper_ids))
            rows = q.limit(100000).to_list()
        except Exception as exc:  # noqa: BLE001
            log.warning("LanceDB 全表读取失败：%s", exc)
            return []
        return [_row_out(r) for r in rows]


def _vector_dim(table) -> int | None:
    """读表里向量列的定长维度（拿不到就返回 None，调用方按"未知"处理）。"""
    try:
        field = table.schema.field("vector")
        size = int(getattr(field.type, "list_size", 0) or 0)
        return size or None
    except Exception:  # noqa: BLE001
        return None


def _schema_mismatch(table, sample: dict) -> str | None:
    """旧表结构和新数据不一致的原因（没有则返回 None）。

    两种情况都必须重建：**缺列**（本轮加 ``lang``）和**向量维度变化**
    （换 embedding 模型）。LanceDB 不支持加列后直接 add，也不支持不同维度的向量列。
    """
    try:
        have = {f.name for f in table.schema}
    except Exception:  # noqa: BLE001
        return None
    missing = set(sample) - have
    if missing:
        return f"缺列 {sorted(missing)}"
    dim, new_dim = _vector_dim(table), len(sample.get("vector") or [])
    if dim and new_dim and dim != new_dim:
        return f"向量维度 {dim} → {new_dim}"
    return None


def _where_in(column: str, values: list[str]) -> str:
    items = ", ".join(f"'{_escape(v)}'" for v in values)
    return f"{column} IN ({items})"


def _escape(value: str) -> str:
    return str(value).replace("'", "''")


def _dumps_meta(meta) -> str:
    from backend.core.utils import json_dumps

    return json_dumps(meta or {})


vector_store = LanceDBStore()
