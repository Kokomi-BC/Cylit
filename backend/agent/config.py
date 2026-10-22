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
    evidence_limit: int
    evidence_chars: int
    archive_chars: int
    image_max_pages: int
    image_max_dpi: int
    model_timeout: float = 180.0
    host_timeout: float = 60.0
    #: 这两项 agent 自己不用，但要**跟着一起读**并上报：`context_budget` 与
    #: `compact_threshold` 的读取点在主后端（`conversation_usage` / 自动压缩），
    #: 而"设置页改的六个值有没有被这个进程读到"是 `GET /agent/limits` 的
    #: `restart_required` 唯一的问题 —— 报半套会让时间戳代表的含义失真。
    context_budget: int = 200000
    compact_threshold: float = 90.0

    @property
    def host_api(self) -> str:
        return f"{self.main_base}{self.api_prefix}"


class AgentConfigError(RuntimeError):
    """配置不完整（比如令牌没生成）——启动即失败，不要带病运行。"""


def load() -> AgentConfig:
    #: **限额覆盖值**（设置页改的那五项）就在这一步被读进来：
    #: `get_settings()` 建好 `Settings` 之后会把 `<data_dir>/agent_limits.json` 里的
    #: 保存值贴到字段上（见 `Settings.apply_agent_limits`），下面逐项取的就是"覆盖后"的值。
    #: 所以改完限额必须**重启 agent 进程**才生效 —— 界面与 `PUT /agent/limits` 都这么说。
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
        evidence_limit=int(s.agent_evidence_limit),
        evidence_chars=int(s.agent_evidence_chars),
        archive_chars=int(s.agent_archive_chars),
        image_max_pages=int(s.agent_image_max_pages),
        image_max_dpi=int(s.agent_image_max_dpi),
        context_budget=int(s.agent_context_budget),
        compact_threshold=float(s.agent_auto_compress_percent),
    )


def limits_payload(cfg: AgentConfig) -> dict[str, float]:
    """本进程实际生效的五项限额（契约键，见 `core.config.AGENT_LIMITS`）。

    写进 `data/agent_limits.applied.json`：`GET /agent/limits` 的 `restart_required`
    就是拿它的时间戳和保存时间比出来的 —— 没有它，"改完到底生效没有"谁都答不上来。
    """
    from backend.core.config import AGENT_LIMITS

    actual = {
        "agent_max_rounds": cfg.max_rounds,
        "agent_deadline_seconds": cfg.deadline_seconds,
        "agent_context_budget": cfg.context_budget,
        "agent_auto_compress_percent": cfg.compact_threshold,
        "agent_attachment_inline_limit": int(get_settings().agent_attachment_inline_limit),
    }
    return {key: AGENT_LIMITS[key][1](actual[field])
            for key, (field, _kind, _low, _high) in AGENT_LIMITS.items()}
