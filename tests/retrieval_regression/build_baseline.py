"""用**改造前的代码**在隔离环境里重建一份"改造前索引"，供检索回归做前后对比。

为什么需要它：chunk 分布已经变了（turboquant 258 → 177 行），要回答"RAG 是不是真的更准"，
就得让同一批问题在两个索引上各跑一遍。改造前的索引没有留档，但原料都在：
- **解析产物**：`data/_backup/preparse-<stamp>/<paper>/{ir.sqlite, md/, pages/}`（Parser 改造前的黄金快照）
- **改造前的 chunking 代码**：`git show <rev>:backend/services/knowledge/knowledge_service.py`

做法（全程不碰真实库/真实索引）：
1. 建隔离环境 `--env <temp>`：拷 `secrets/`（模型密钥）与备份产物到 `<temp>/papers/<paper>/`，
   写一个 sqlite 库（Paper / Block / ModelConfig / Setting 四张表）；
2. 用 `git show` 取出改造前的 chunking 模块，在**隔离进程**里跑 `index_paper(rebuild=True)`
   → 向量写进 `<temp>/index/lancedb`。

之后这样跑回归（同一套检索实现，只换索引）::

    PR_DB_BACKEND=sqlite PR_DATA_DIR=<temp> PR_PYTHONPATH=... \\
        python -m tests.retrieval_regression.run --out docs/retrieval-regression/before.json

注意：`index_paper` 会调真实 embedding（DashScope），有 API 成本；只在需要重测时跑。
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import sqlite3
import subprocess
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
DEFAULT_REV = "719a72b"          # 第 1 批之前（改造前的 chunking）
OLD_MODULE = "backend/services/knowledge/knowledge_service.py"
DEFAULT_BACKUP = "data/_backup/preparse-20260925-141157"


def old_chunk_module(rev: str) -> Path:
    """把改造前的 chunking 模块导出到 `<temp>/old_chunking.py`（由调用方指定位置）。"""
    src = subprocess.run(["git", "show", f"{rev}:{OLD_MODULE}"], cwd=REPO,
                         capture_output=True, text=True, check=True).stdout
    return src  # type: ignore[return-value]


def build_env(paper_id: str, backup: Path, env_dir: Path, rev: str) -> Path:
    """建隔离环境（产物 + secrets + sqlite 库），返回 env 目录。"""
    env_dir.mkdir(parents=True, exist_ok=True)
    # ① 解析产物（改造前的 IR / Markdown / 页级译文）
    src_paper = backup / paper_id
    dst_paper = env_dir / "papers" / paper_id
    if not src_paper.is_dir():
        raise SystemExit(f"备份里没有 {paper_id}：{src_paper}")
    if dst_paper.exists():
        shutil.rmtree(dst_paper)
    dst_paper.parent.mkdir(parents=True, exist_ok=True)
    shutil.copytree(src_paper, dst_paper)
    # ② 密钥（embedding / rerank 要用；只读拷贝）
    secrets_src = REPO / "data" / "secrets"
    if secrets_src.is_dir():
        shutil.copytree(secrets_src, env_dir / "secrets", dirs_exist_ok=True)

    # ③ sqlite 库：Paper / Block（来自**改造前的 IR**）/ ModelConfig + Setting（从真实库拷）
    sys.path.insert(0, str(REPO))
    from sqlalchemy import create_engine
    from sqlalchemy.orm import Session

    from backend.repositories.mysql.models import (Base, Block, ModelConfig, Paper,
                                                   Setting)
    from backend.repositories.mysql.session import session_scope

    db_path = env_dir / "papermind.sqlite3"
    if db_path.exists():
        db_path.unlink()
    engine = create_engine(f"sqlite:///{db_path.as_posix()}")
    Base.metadata.create_all(engine)

    ir_rows = sqlite3.connect(str(dst_paper / "ir.sqlite"))
    ir_rows.row_factory = sqlite3.Row
    blocks = [dict(r) for r in ir_rows.execute("SELECT * FROM block ORDER BY order_global")]
    ir_rows.close()

    with session_scope() as live:
        paper = live.get(Paper, paper_id)
        models = [dict(r.__dict__) for r in live.query(ModelConfig).all()]
        settings = [dict(r.__dict__) for r in live.query(Setting).all()]
        paper_fields = {c.name: getattr(paper, c.name) for c in Paper.__table__.columns}

    with Session(engine) as sess:
        sess.add(Paper(**paper_fields))
        for b in blocks:
            sess.add(Block(
                id=b["id"], paper_id=paper_id, page_no=b["page_no"], type=b["type"],
                col=b["col"], order_global=b["order_global"],
                x0=b["x0"], y0=b["y0"], x1=b["x1"], y1=b["y1"],
                md_anchor=b["md_anchor"], content_hash=b["content_hash"],
                has_translation=False, section_id=b["section_id"],
                char_count=b["char_count"] or 0,
                preview=(b["text_clean"] or b["text"] or "")[:200],
                meta_json=b["meta_json"]))
        for m in models:
            m.pop("_sa_instance_state", None)
            sess.add(ModelConfig(**m))
        for s in settings:
            s.pop("_sa_instance_state", None)
            sess.add(Setting(**s))
        sess.commit()
    print(f"隔离环境就绪：{env_dir}")
    print(f"  Block {len(blocks)} 条（来自改造前 IR）· ModelConfig {len(models)} · "
          f"Setting {len(settings)} · 密钥 {'已拷' if secrets_src.is_dir() else '缺失'}")
    return env_dir


def index_with_old_code(paper_id: str, env_dir: Path, rev: str) -> None:
    """在隔离进程里用改造前的 chunking 建索引（向量写进隔离环境的 index/）。"""
    module_path = env_dir / "old_chunking.py"
    module_path.write_text(old_chunk_module(rev), encoding="utf-8")
    script = env_dir / "run_old_index.py"
    script.write_text(
        "import importlib.util, sys\n"
        f"spec = importlib.util.spec_from_file_location('old_chunking', {str(module_path)!r})\n"
        "old = importlib.util.module_from_spec(spec); sys.modules['old_chunking'] = old\n"
        "spec.loader.exec_module(old)\n"
        f"print('改造前代码：base chunk', len(old._collect_chunks({paper_id!r})))\n"
        f"out = old.index_paper({paper_id!r}, rebuild=True)\n"
        "print('改造前索引：', {k: out[k] for k in ('chunks','embedded','index_version','by_type','by_lang') if k in out})\n",
        encoding="utf-8")
    env = {**os.environ, "PYTHONUTF8": "1", "PYTHONIOENCODING": "utf-8",
           "PYTHONPATH": str(REPO), "PR_DB_BACKEND": "sqlite",
           "PR_DATA_DIR": str(env_dir)}
    print(f"用 {rev} 的代码建索引（会调 embedding 模型）…")
    proc = subprocess.run([sys.executable, str(script)], env=env, cwd=str(REPO),
                          capture_output=True, text=True)
    print(proc.stdout[-3000:])
    if proc.returncode != 0:
        print(proc.stderr[-4000:])
        raise SystemExit(f"改造前索引构建失败（exit {proc.returncode}）")


def main() -> None:
    ap = argparse.ArgumentParser(description="重建改造前的索引（隔离环境）")
    ap.add_argument("--paper", default="turboquant-online-vector-iclr-26")
    ap.add_argument("--backup", default=DEFAULT_BACKUP)
    ap.add_argument("--env", required=True, help="隔离环境目录（建议放临时目录）")
    ap.add_argument("--rev", default=DEFAULT_REV, help="改造前的 git 版本")
    ap.add_argument("--skip-index", action="store_true", help="只建环境，不建索引")
    args = ap.parse_args()

    env_dir = Path(args.env).resolve()
    build_env(args.paper, Path(args.backup).resolve(), env_dir, args.rev)
    if not args.skip_index:
        index_with_old_code(args.paper, env_dir, args.rev)
    print(json.dumps({"env": str(env_dir), "paper": args.paper, "rev": args.rev},
                     ensure_ascii=False))


if __name__ == "__main__":
    main()
