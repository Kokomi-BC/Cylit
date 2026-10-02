"""``python -m backend.agent`` —— 起 Agent 服务（独立进程，默认 :8711）。

主后端不会替你拉起它（两个进程各有各的生命周期，见规划 §4.1 的三条理由），
用 ``pr.py agent`` 或这个模块都可以。
"""

from __future__ import annotations

from backend.core.config import bootstrap_sys_path

bootstrap_sys_path()

from backend.agent.service import main  # noqa: E402

if __name__ == "__main__":
    raise SystemExit(main())
