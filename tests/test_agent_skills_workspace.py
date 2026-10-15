"""Skill 库必须用 **workspace 里的虚拟路径** 交给框架，不能交真实绝对路径。

回归的是 2026-10-15 用户报的那个红字：

    WorkspaceError: 路径逃出 workspace：/root/Cylit/backend/agent/skills

成因：`ReadOnlyWorkspace` 把真实 skills 目录挂在虚拟根 `/workspace` 上，
而框架的 `Workspace.resolve()` 遇到以 `/` 开头的路径**原样返回**（当它是 workspace 内
的绝对路径）。于是真实路径 `/root/.../skills` 被 backend 再当成虚拟路径换算一次，
变成 `<root>/root/.../skills` → 越界。

这个测试同时钉住两件事，缺任一件都是"看着 9 个 skill、其实一个都没加载"：
  1. 传的必须是虚拟路径（这里断言真实路径会抛错，防止有人"顺手改回去"）；
  2. 加载器必须真的发现 `skills/` 下的每一个包。
"""

from __future__ import annotations

import asyncio

import pytest

pytest.importorskip("pydantic_ai")

from pydantic_ai.workspaces import Workspace  # noqa: E402
from pydantic_ai_harness.skills._loader import load_skill_libraries  # noqa: E402

from backend.agent.runtime import AgentRuntime  # noqa: E402
from backend.agent.workspace import VIRTUAL_ROOT, ReadOnlyWorkspace  # noqa: E402
from backend.core.config import get_settings  # noqa: E402


def _load(paths: list[str]) -> list[str]:
    ws = Workspace(ReadOnlyWorkspace(get_settings().agent_skills_dir))
    defs = asyncio.run(load_skill_libraries(ws, paths, include=None, exclude=frozenset()))
    return sorted(d.name for d in defs)


def test_skill_library_path_is_virtual() -> None:
    assert AgentRuntime._SKILL_LIBRARY == VIRTUAL_ROOT


def test_virtual_path_discovers_skills() -> None:
    names = _load([VIRTUAL_ROOT])
    assert names, "一个 skill 都没加载出来，检查 skills/ 是否还在仓库里"
    # 仓库里那份 skills/ 的每个子目录都得被认出来
    expected = sorted(p.name for p in get_settings().agent_skills_dir.iterdir()
                      if p.is_dir() and (p / "SKILL.md").is_file())
    assert names == expected


def test_real_absolute_path_is_rejected() -> None:
    """真实路径会越界 —— 这正是线上那个报错。谁改回去，这条会红。"""
    from pydantic_ai.workspaces import WorkspaceError

    with pytest.raises(WorkspaceError):
        _load([str(get_settings().agent_skills_dir)])
