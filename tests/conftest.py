"""pytest 配置与共享 fixture。"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


@pytest.fixture(scope="session")
def project_root() -> Path:
    return ROOT


@pytest.fixture(scope="session")
def sample_pdf() -> Path | None:
    """首个验证用例：TurboQuant 论文 PDF（存在才跑端到端测试）。"""
    candidates = [
        ROOT.parent / "TurboQuant_Online_Vector ICLR-26.pdf",
        ROOT.parent / "data" / "TurboQuant_Online_Vector ICLR-26.pdf",
    ]
    for c in candidates:
        if c.is_file():
            return c
    return None
