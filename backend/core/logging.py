"""日志：控制台 + 轮转文件。所有模块统一 ``get_logger(__name__)``。"""

from __future__ import annotations

import logging
import sys
from logging.handlers import RotatingFileHandler

from backend.core.config import get_settings

_CONFIGURED = False
_FMT = "%(asctime)s | %(levelname)-7s | %(name)-38s | %(message)s"


def setup_logging(level: str | None = None) -> None:
    global _CONFIGURED
    if _CONFIGURED:
        return
    s = get_settings()
    lvl = getattr(logging, (level or s.log_level).upper(), logging.INFO)

    root = logging.getLogger()
    root.setLevel(lvl)
    for h in list(root.handlers):
        root.removeHandler(h)

    stream = logging.StreamHandler(sys.stderr)
    stream.setFormatter(logging.Formatter(_FMT, datefmt="%H:%M:%S"))
    root.addHandler(stream)

    if s.log_to_file:
        fh = RotatingFileHandler(
            s.logs_dir / "backend.log", maxBytes=8 * 1024 * 1024,
            backupCount=3, encoding="utf-8",
        )
        fh.setFormatter(logging.Formatter(_FMT))
        root.addHandler(fh)

    # 第三方库降噪
    for noisy in ("httpx", "httpcore", "urllib3", "openai", "PIL", "asyncio", "multipart"):
        logging.getLogger(noisy).setLevel(logging.WARNING)

    _CONFIGURED = True


def get_logger(name: str) -> logging.Logger:
    setup_logging()
    return logging.getLogger(name)
