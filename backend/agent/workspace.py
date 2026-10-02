"""只读 workspace backend（Windows 上没有别的选择）。

为什么需要它：`pydantic_ai_harness` 的 `Skills` 要挂在一个 workspace 上
（官方 README：*"A run without a workspace fails at its start"*），而框架自带的
`LocalWorkspaceBackend` / `LocalWorkspace` 在 Windows 上直接抛
``NotImplementedError: only supports POSIX platforms``。三条出路里我们选了自写只读实现：
容器/WSL2 与我们"将来打包成 Electron"的目标冲突，远端 workspace 又是另一台机器。

**关键坑（已踩，别再改回去）**：`working_dir()` 必须返回**虚拟 POSIX 根**
（``/workspace``），不能返回 ``C:/...``。因为框架的 `Workspace.resolve()` 是
``posixpath.join(base, path)`` —— POSIX 语义下 ``join("/workspace", "/workspace/x")``
幂等，而 ``C:/...`` 在 POSIX 眼里是**相对路径**，会被拼成 ``C:/ws/C:/ws/skills``，
随后 `Path()` 抛 ``WinError 123``。所以虚拟路径与真实路径的换算是本模块的全部工作。

写操作一律 `WorkspaceReadOnlyError`：这样"agent 不能改你的文件"是**类型级**的保证，
而不是靠"我们没写那个工具"。
"""

from __future__ import annotations

import posixpath
from pathlib import Path

from pydantic_ai.workspaces import (
    FileEntry, WorkspaceError, WorkspaceReadOnlyError, WorkspaceRef,
)

#: 虚拟根。真实目录挂在它下面，agent 看到的世界只有这一棵树。
VIRTUAL_ROOT = "/workspace"


class ReadOnlyWorkspace:
    """把一个真实目录以只读方式暴露给框架（实现 `WorkspaceBackend` 协议）。"""

    def __init__(self, real_root: str | Path) -> None:
        self._root = Path(real_root).resolve()

    # ---------------- 路径换算 ----------------
    def _virtual(self, path: str) -> str:
        """真实/相对路径 → 虚拟 POSIX 路径（幂等，允许 ``\\`` 与 ``/`` 混用）。"""
        return posixpath.normpath(posixpath.join(VIRTUAL_ROOT, str(path).replace("\\", "/")))

    def _local(self, path: str) -> Path:
        """虚拟路径 → 真实路径；越界（``..`` 逃逸）直接抛错。"""
        virtual = self._virtual(path)
        rel = posixpath.relpath(virtual, VIRTUAL_ROOT)
        target = self._root if rel == "." else (self._root / rel).resolve()
        if target != self._root and self._root not in target.parents:
            raise WorkspaceError(f"路径逃出 workspace：{path}")
        return target

    # ---------------- 协议 ----------------
    @property
    def ref(self) -> WorkspaceRef:
        return WorkspaceRef(provider="cylit-readonly", id=VIRTUAL_ROOT)

    async def working_dir(self) -> str:
        """**协程**，且必须返回虚拟根（见模块文档）。"""
        return VIRTUAL_ROOT + "/"

    async def exists(self, path: str) -> bool:
        return self._local(path).exists()

    async def stat(self, path: str) -> FileEntry:
        p = self._local(path)
        return FileEntry(name=p.name, path=self._virtual(path), is_dir=p.is_dir(),
                         size=None if p.is_dir() else p.stat().st_size)

    async def list_dir(self, path: str) -> list[FileEntry]:
        base = self._virtual(path)
        return [FileEntry(name=c.name, path=posixpath.join(base, c.name),
                          is_dir=c.is_dir(),
                          size=None if c.is_dir() else c.stat().st_size)
                for c in sorted(self._local(path).iterdir(), key=lambda x: x.name)]

    async def read_bytes(self, path: str) -> bytes:
        return self._local(path).read_bytes()

    async def realpath(self, path: str) -> str:
        return self._virtual(path)

    async def write_bytes(self, path: str, data: bytes) -> None:
        raise WorkspaceReadOnlyError(f"workspace 是只读的：{path}")

    async def make_dir(self, path: str) -> None:
        raise WorkspaceReadOnlyError(f"workspace 是只读的：{path}")

    async def remove(self, path: str) -> None:
        raise WorkspaceReadOnlyError(f"workspace 是只读的：{path}")
