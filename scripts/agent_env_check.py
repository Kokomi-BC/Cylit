"""Agent 依赖冒烟（**换机重装必跑**，不需要起服务、不花钱）。

规划 §14 R1：`pydantic-ai-harness` 是 0.x 且与 `pydantic-ai-slim` 强耦合
（0.53.0 钉死 2.53.0），而 agent 又**共用主环境**没有 venv 兜底 —— 所以升级/换机之后
必须有一条"能不能 import、Skills 能不能在 Windows 上真的发现 skill"的快速检查：

    python scripts/agent_env_check.py

覆盖两件事：
1. 框架侧：`pydantic_ai` / `harness` 可导入，`Skills` 在 Windows 上完成真实发现；
2. 主仓侧：`openai` 升级后 `OpenAICompatibleGeneration` 仍可构造客户端，
   且 `OpenAIChatModel` 能用 `base_url` 直连 OpenAI 兼容端点（DeepSeek 就是这一条）。
"""

from __future__ import annotations

import asyncio
import importlib.metadata as md
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import openai  # noqa: E402
import pydantic  # noqa: E402
import pydantic_ai  # noqa: E402
from pydantic_ai_harness import Skills  # noqa: E402

from backend.agent.workspace import ReadOnlyWorkspace  # noqa: E402


FAILED: list[str] = []


def ok(label: str, cond: bool, detail: str = "") -> None:
    if not cond:
        FAILED.append(label)
    print(f"[{'PASS' if cond else 'FAIL'}] {label}" + (f"  {detail}" if detail else ""))


def main() -> int:
    print(f"openai={openai.__version__} pydantic={pydantic.VERSION} "
          f"pydantic_ai={pydantic_ai.__version__} "
          f"harness={md.version('pydantic-ai-harness')}")

    root = Path(tempfile.mkdtemp(prefix="cylit_agent_smoke_"))
    (root / "paper-qa").mkdir(parents=True)
    (root / "paper-qa" / "SKILL.md").write_text(
        "---\ndescription: 证据式论文问答。\n---\n\n先检索后作答，只写 [n] 引用。\n",
        encoding="utf-8")
    (root / "formula-explain").mkdir()
    (root / "formula-explain" / "SKILL.md").write_text(
        "---\nname: formula-explain\ndescription: 解释或复现当前论文里的公式。\n---\n\n先 formula_get。\n",
        encoding="utf-8")

    workspace = ReadOnlyWorkspace(root)

    async def check_skills() -> None:
        # 用框架自己的加载器取"事实"：它才是真正往提示词里注入的那一份。
        # ⚠️ 它收的是**框架的 Workspace 包装**，不是裸 backend（内部会调 workspace.resolve）。
        from pydantic_ai.workspaces import Workspace
        from pydantic_ai_harness.skills._loader import load_skill_libraries

        defs = await load_skill_libraries(Workspace(workspace), [str(root)],
                                          include=None, exclude=frozenset())
        names = sorted(d.name for d in defs)
        ok("Skills 在 Windows 上完成发现", names == ["formula-explain", "paper-qa"],
           str(names))
        ok("SKILL.md 正文被读入", all(d.body for d in defs),
           ", ".join(f"{d.name}:{len(d.body)}字" for d in defs))
        only = await load_skill_libraries(Workspace(workspace), [str(root)],
                                         include=["paper-qa"], exclude=frozenset())
        ok("include 过滤生效", [d.name for d in only] == ["paper-qa"])
        # working_dir 必须是**虚拟 POSIX 根**（返回 C:/… 会被 posixpath.join 拼两次）
        wd = await workspace.working_dir()
        ok("workspace 的 working_dir 是虚拟 POSIX 根",
           wd.startswith("/") and ":" not in wd, repr(wd))
        try:
            await workspace.write_bytes("/workspace/x", b"x")
            ok("workspace 构造上只读", False, "写操作竟然成功了")
        except Exception as exc:  # noqa: BLE001
            ok("workspace 构造上只读", type(exc).__name__ == "WorkspaceReadOnlyError",
               type(exc).__name__)

    asyncio.run(check_skills())
    try:
        Skills(str(root), workspace=workspace)
        ok("Skills 能力可构造", True)
    except Exception as exc:  # noqa: BLE001
        ok("Skills 能力可构造", False, f"{type(exc).__name__}: {exc}")

    from backend.providers.generation.openai_compatible import (
        OpenAICompatibleGeneration,
    )

    provider = OpenAICompatibleGeneration(
        {"provider": "openai_compatible", "model_name": "deepseek-chat",
         "base_url": "https://api.deepseek.com/v1", "timeout": 5},
        api_key="sk-smoke-test-not-used")
    client = provider._ensure_client()
    ok("主仓 provider 在升级后的 openai 下可构造客户端", client is not None,
       type(client).__name__)
    ok("chat.completions 仍在", hasattr(client, "chat") and hasattr(client.chat, "completions"))

    from pydantic_ai.models.openai import OpenAIChatModel
    from pydantic_ai.providers.openai import OpenAIProvider

    model = OpenAIChatModel("deepseek-chat",
                            provider=OpenAIProvider(base_url="https://api.deepseek.com/v1",
                                                    api_key="sk-smoke-test-not-used"))
    ok("OpenAI 兼容端点可用 base_url 构造", model is not None, model.model_name)

    if FAILED:
        print(f"\n失败 {len(FAILED)} 项：{FAILED}")
        print("依赖被换过 —— 先去跑 scripts/agent_check.py 与 scripts/probe_agent_tools.py")
        return 1
    print("\n全部通过（依赖冒烟 OK）")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
