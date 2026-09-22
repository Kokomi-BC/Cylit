"""全模块导入冒烟测试：语法 / import 错误必须在测试里立刻暴露。

**为什么需要它**：曾出现过 `pytest` 全绿、但
`backend.services.translation.translation_service` 有一处缩进错误的情况——
因为没有任何测试 import 过它（翻译任务只在脚本/服务里跑）。
这里把 `backend` 包下所有模块都导入一遍，几十毫秒的成本换掉一整类"跑起来才发现"。
"""

from __future__ import annotations

import importlib
import pkgutil

import backend


def test_all_backend_modules_import():
    names = [m.name for m in pkgutil.walk_packages(backend.__path__, "backend.")]
    assert len(names) > 50, f"只发现 {len(names)} 个模块，扫描路径可能不对"
    failed: list[str] = []
    for name in names:
        try:
            importlib.import_module(name)
        except Exception as exc:  # noqa: BLE001
            failed.append(f"{name}: {type(exc).__name__}: {exc}")
    assert not failed, "以下模块导入失败：\n" + "\n".join(failed)
