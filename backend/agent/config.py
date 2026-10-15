"""Agent 服务的运行配置。

只有两个来源：主仓的 `Settings`（端口、skill 目录、预算）与
``data/secrets/agent.token``（调主后端用的内部令牌）。

**模型密钥刻意不在这里**：agent 是"能读论文、能问模型"的服务，但它自己去读
``deepseek.key`` 就等于把密钥散成两处入口。模型配置由主后端经 HTTP 下发（§4.2）。
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from backend.core.config import get_settings


@dataclass(frozen=True)
class AgentConfig:
    """一次进程生命周期内不变的配置。"""

    #: 主后端地址（工具与模型配置都从这里取）
    main_base: str
    api_prefix: str
    #: 内部令牌；缺失时 `load()` 直接抛错，而不是"匿名调通"——匿名会让边界形同虚设
    token: str
    skills_dir: Path
    max_rounds: int
    deadline_seconds: float
    heartbeat_seconds: float
    search_top_k: int
    search_budget: int
    edit_budget: int
    evidence_limit: int
    evidence_chars: int
    archive_chars: int
    image_max_pages: int
    image_max_dpi: int
    model_timeout: float = 180.0
    host_timeout: float = 60.0

    @property
    def host_api(self) -> str:
        return f"{self.main_base}{self.api_prefix}"


class AgentConfigError(RuntimeError):
    """配置不完整（比如令牌没生成）——启动即失败，不要带病运行。"""


def load() -> AgentConfig:
    s = get_settings()
    token = s.agent_token()
    if not token:
        raise AgentConfigError(
            "缺少内部令牌：先启动一次主后端（它会生成 data/secrets/agent.token），"
            "或设置 PR_AGENT_TOKEN 环境变量。"
        )
    base = f"http://{s.agent_host}:{s.port}"
    if s.host not in ("127.0.0.1", "localhost", "0.0.0.0"):
        # 主后端绑在别的地址上时（少见），用它的 host 而不是 agent 的
        base = f"http://{s.host}:{s.port}"
    return AgentConfig(
        main_base=base,
        api_prefix=s.api_prefix,
        token=token,
        skills_dir=Path(s.agent_skills_dir),
        max_rounds=int(s.agent_max_rounds),
        deadline_seconds=float(s.agent_deadline_seconds),
        heartbeat_seconds=float(s.agent_heartbeat_seconds),
        search_top_k=int(s.agent_search_top_k),
        search_budget=int(s.agent_search_budget),
        edit_budget=int(s.agent_edit_budget),
        evidence_limit=int(s.agent_evidence_limit),
        evidence_chars=int(s.agent_evidence_chars),
        archive_chars=int(s.agent_archive_chars),
        image_max_pages=int(s.agent_image_max_pages),
        image_max_dpi=int(s.agent_image_max_dpi),
    )
