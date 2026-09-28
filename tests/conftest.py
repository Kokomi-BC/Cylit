"""pytest 配置与共享 fixture。

**在线解析的测试默认不跑**（用户口径 2026-09-28：在线那套单独跑，避免和主测试混在一起）。
实现方式是在这里统一加 `--run-online` 开关：

- ``pytest``（默认）：带 ``@pytest.mark.online`` 的用例被**跳过**，主测试行为一字不变；
- ``pytest --run-online``：跑全部在线用例（它们用假传输层/样例结果包，**不联网**）；
- 需要真实 token 的联网冒烟另标 ``@pytest.mark.mineru_live``，没有 token 时自行 skip。
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


def pytest_addoption(parser: pytest.Parser) -> None:
    parser.addoption(
        "--run-online", action="store_true", default=False,
        help="跑 @pytest.mark.online 的在线解析（MinerU）用例（默认跳过）")


def pytest_collection_modifyitems(config: pytest.Config, items: list) -> None:
    if config.getoption("--run-online"):
        return
    skip = pytest.mark.skip(reason="在线解析用例：默认跳过（加 --run-online 才跑）")
    for item in items:
        if "online" in item.keywords or "mineru_live" in item.keywords:
            item.add_marker(skip)


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
