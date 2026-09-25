"""A/B 两臂实验：arm2（只按 TOC 切）与 arm3（TOC + LLM 语义锚点）各建一次索引并跑题库回归。

为什么做成脚本：两臂之间只差一个 setting（`kb.chunk_use_breaks`），必须保证除了它以外
环境完全一致（同一份 pages.json、同一套代码），否则数字不可比。跑完停在 arm3。

用法::

    python scripts/kb_arm_compare.py
    # 之后可对比报告：
    python -m tests.retrieval_regression.run --compare \\
        data/logs/snapshot/retrieval-arm2-30q.json data/logs/snapshot/retrieval-arm3-30q.json
"""
from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.stdout.reconfigure(encoding="utf-8")
sys.path.insert(0, str(ROOT))
PY = sys.executable
PAPERS = ["turboquant-online-vector-iclr-26", "attention-is-all-you-need"]
OUT = ROOT / "data/logs/snapshot"

from backend.repositories.mysql.models import Setting  # noqa: E402
from backend.repositories.mysql.session import session_scope  # noqa: E402


def set_breaks(value: str) -> None:
    with session_scope() as sess:
        row = sess.get(Setting, "kb.chunk_use_breaks")
        if row is None:
            sess.add(Setting(key="kb.chunk_use_breaks", value=value))
        else:
            row.value = value
    print(f"  kb.chunk_use_breaks = {value}", flush=True)


def run(cmd: list[str]) -> None:
    proc = subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8")
    tail = (proc.stdout or "").strip().splitlines()[-1:] or [""]
    print(f"  $ {' '.join(cmd[1:4])} → {tail[0][:120]}", flush=True)
    if proc.returncode != 0:
        print(proc.stderr[-800:], flush=True)
        raise SystemExit(proc.returncode)


for arm, value in (("arm2", "0"), ("arm3", "1")):
    print(f"=== {arm}（kb.chunk_use_breaks={value}）", flush=True)
    set_breaks(value)
    for pid in PAPERS:
        run([PY, "cli/pr.py", "index", pid, "--rebuild"])
    run([PY, "-m", "tests.retrieval_regression.run",
         "--out", str(OUT / f"retrieval-{arm}-30q.json"),
         "--env-note", f"{arm} " + ("只按 TOC 层级切（无 LLM 锚点）" if arm == "arm2"
                                    else "TOC + LLM 语义锚点")])
    report = json.loads((OUT / f"retrieval-{arm}-30q.json").read_text(encoding="utf-8"))
    print(f"    hit@6 {report['hit_at_k']:.0%} 正文命中 {report['content_hit_at_k']:.0%} "
          f"MRR {report['mrr']} 正文MRR {report['content_mrr']} "
          f"公式上下文 {report['formula_context_coverage']:.0%} "
          f"appendix {report['appendix_coverage']:.0%}", flush=True)
print("完成（最终留在 arm3）", flush=True)
