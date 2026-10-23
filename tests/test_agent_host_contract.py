"""契约：`tools.py` 里每个 `ctx.deps.host.<name>` 都必须在 `HostClient` 上真的存在。

为什么值得一条专门的测试（评审点名要补的"防下一次"）：`HostClient` 曾经把
`async def meta_write(...)` **吞进上一行注释里**（`backend/agent/host.py` 的
"写：论文元数据"那一行标题与 def 拼成了一行）。结果是：

- 源码里**一眼看得到** `async def meta_write` 的签名（它在注释里），静态印象一切正常；
- 运行时 `HostClient` 根本没有这个方法 → `paper_meta_write` 每次 AttributeError；
- `grep meta_write tests/` 一个字都没有 —— 没有任何护栏会变红。

所以这里不检查"某个方法在不在"，而是**遍历工具层实际用到的每一个 host 方法**，
并额外钉一条"没有任何 def 被藏进注释行"（正是那次故障的形状）。
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

from backend.agent import host as host_mod
from backend.agent import tools as tools_mod
from backend.agent.host import HostClient

TOOLS_SRC = Path(tools_mod.__file__).read_text(encoding="utf-8")
HOST_SRC = Path(host_mod.__file__).read_text(encoding="utf-8")

#: 工具层真正调到的 host 方法（`ctx.deps.host.notes(...)` 这种）
USED = sorted({m.group(1) for m in re.finditer(r"ctx\.deps\.host\.(\w+)", TOOLS_SRC)})


def test_the_scan_is_not_vacuous() -> None:
    """扫不到东西 = 这条护栏是空的（改个名字就静默失效），所以先钉住扫描本身。"""
    assert len(USED) >= 20, f"只扫到 {len(USED)} 个 host 调用，扫描规则多半失效了"
    assert "meta_write" in USED, "工具层的元数据写口不见了：`paper_meta_write` 靠它落库"


@pytest.mark.parametrize("name", USED)
def test_host_client_exposes_every_call(name: str) -> None:
    """`tools.py` 调的每一个 host 方法，`HostClient` 上都得有一个**可调用**的同名属性。"""
    attr = getattr(HostClient, name, None)
    assert callable(attr), (
        f"tools.py 调了 `ctx.deps.host.{name}(…)`，但 HostClient 没有这个方法 —— "
        "运行时会 AttributeError（2026-12 的 meta_write 就是这么坏的）")


def test_no_definition_is_hidden_inside_a_comment() -> None:
    """`# ---- 标题 ----    async def x(...)` 这种被吞进注释的 def，一条都不许有。"""
    offenders = [f"host.py:{lineno}: {line.strip()}"
                 for lineno, line in enumerate(HOST_SRC.splitlines(), 1)
                 if line.lstrip().startswith("#") and ("def " in line or "class " in line)]
    assert offenders == [], "有定义被并进注释行了（那一行不会被执行）：\n" + "\n".join(offenders)


def test_every_used_bridge_actually_hits_the_main_backend() -> None:
    """光有名字不够：`meta_write` 必须真的往 `/agent/papers/{id}/metadata` 发 POST。"""
    assert "/agent/papers/{paper_id}/metadata" in HOST_SRC
    assert "async def meta_write(self, paper_id: str, payload: dict) -> dict" in HOST_SRC