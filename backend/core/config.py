"""全局配置。

设计原则（对应规划 §1.2 本地优先、§6 分层）：
- 所有路径集中在 ``Settings``，其余模块一律通过 ``get_settings()`` 取，禁止散落硬编码。
- 密钥不落库、不进代码：``data/secrets/`` 下的独立文件（规划 §12.2 / §31.5）。
- 业务库默认 MySQL 8；无 MySQL 时可用 SQLite 退化运行（离线答辩兜底）。
- 模型名一律不写死在代码里，代码只引用 ``model_type``（规划 §12.2 默认路由）。
"""

from __future__ import annotations

import json
import os
import sys
from datetime import datetime, timezone
from functools import lru_cache
from pathlib import Path
from typing import Any

from pydantic import Field, field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

from backend.core.errors import ValidationError

# backend/core/config.py -> backend/core -> backend -> Cylit（仓库根，2026-09-28 由 paper-reader 改名）
BACKEND_DIR = Path(__file__).resolve().parents[1]
PROJECT_ROOT = BACKEND_DIR.parent


#: 设置页可改的**六项问答运行限额**：契约键 → (Settings 字段, 类型, 下限, 上限)。
#: 
#: 为什么要两张名字：契约键是**前端已经按它写好**的那一套（`max_rounds` /
#: `compact_threshold` …，见 `docs/agent-接入规划.md` 与本轮前端契约），而字段名是历史包袱
#: （比如"压缩触发线"在代码里叫 `agent_auto_compress_percent`）。对应关系只写在这里一处，
#: 读取点、校验、落盘覆盖都从这张表走 —— 抄成三份就会各自漂。
AGENT_LIMITS: dict[str, tuple[str, type, float, float]] = {
    "max_rounds": ("agent_max_rounds", int, 1, 200),
    "search_budget": ("agent_search_budget", int, 0, 50),
    "deadline_seconds": ("agent_deadline_seconds", float, 30, 3600),
    "context_budget": ("agent_context_budget", int, 8000, 1000000),
    "compact_threshold": ("agent_auto_compress_percent", float, 50, 99),
    "attachment_inline_limit": ("agent_attachment_inline_limit", int, 1, 50),
}


def _fmt_bound(value: float) -> str:
    return str(int(value)) if float(value).is_integer() else str(value)


def normalize_agent_limits(payload: Any, *, strict: bool = True) -> dict[str, float]:
    """把请求体收敛成 `{契约键: 值}`，顺手做范围校验。

    `strict=True`（PUT 用）：越界/类型不对就抛 `ValidationError`（400）；
    `strict=False`（读盘用）：只丢掉那一项 —— 磁盘上的文件可能被手改坏，读的时候
    不该因为一项非法就让整个限额接口 500。

    允许只给其中几项（前端的表单通常全给，但不必强制）：缺的键就是"沿用当前值"。
    """
    if not isinstance(payload, dict):
        raise ValidationError("限额必须是 JSON 对象")
    out: dict[str, float] = {}
    for key, (_field, kind, low, high) in AGENT_LIMITS.items():
        if key not in payload:
            continue
        raw = payload.get(key)
        try:
            value = kind(raw)
        except (TypeError, ValueError):
            if strict:
                raise ValidationError(f"{key} 必须是数字（收到 {raw!r}）") from None
            continue
        if not (low <= value <= high):
            if strict:
                raise ValidationError(
                    f"{key} 必须在 {_fmt_bound(low)}~{_fmt_bound(high)} 之间（收到 {raw!r}）")
            continue
        out[key] = value
    return out


def _parse_stamp(raw: Any) -> datetime | None:
    """读时间戳（ISO 8601 / 秒级浮点都认）；读不懂就是 None。"""
    if isinstance(raw, (int, float)):
        try:
            return datetime.fromtimestamp(float(raw), timezone.utc)
        except (OSError, ValueError, OverflowError):
            return None
    if isinstance(raw, str) and raw.strip():
        try:
            stamp = datetime.fromisoformat(raw.strip())
        except ValueError:
            return None
        return stamp if stamp.tzinfo else stamp.replace(tzinfo=timezone.utc)
    return None


class Settings(BaseSettings):
    """系统配置。优先级：环境变量 > .env > 默认值。"""

    model_config = SettingsConfigDict(
        env_file=(PROJECT_ROOT / ".env"),
        env_file_encoding="utf-8",
        env_prefix="PR_",
        extra="ignore",
    )

    # ---------- 基础 ----------
    # 应用名（`/health` 等对外露面用它）。2026-09-28 用户把项目改名为 **Cylit**：
    # 只改**对外可见的品牌字**，数据库名 / sqlite 文件名 / localStorage 的 `papermind.*` 键
    # 一律不动 —— 那些是数据标识，改了会丢数据（详见 dev-log 17.120）。
    app_name: str = "Cylit"
    app_version: str = "0.1.0"
    debug: bool = True
    host: str = "127.0.0.1"
    port: int = 8710
    api_prefix: str = "/api/v1"
    user_id: int = 1  # 单用户免登录（规划 §31.4）

    # ---------- 路径 ----------
    project_root: Path = PROJECT_ROOT
    data_dir: Path = PROJECT_ROOT / "data"

    # ---------- 业务库 ----------
    # mysql | sqlite
    db_backend: str = "mysql"
    db_host: str = "127.0.0.1"
    db_port: int = 3306
    db_user: str = "root"
    db_password: str = "root"
    db_name: str = "papermind"
    db_echo: bool = False
    db_pool_size: int = 5
    db_max_overflow: int = 10

    # ---------- 解析默认参数 ----------
    render_dpi: int = 150               # 页面图（复核与降级用）
    crop_dpi: int = 600                 # 公式高倍裁图（4× 于 150dpi）
    parse_max_pages: int = 0            # 0 = 全部
    min_text_block_chars: int = 2
    # ---------- 扫描件兜底（规划 §13.4：影印 PDF 无文本层）----------
    parse_vision_enabled: bool = True   # 关闭则扫描页不调用视觉模型（正文为空）
    parse_vision_dpi: int = 200         # 读页专用 dpi（高于阅读器页图，保住上下标）

    # ---------- 在线解析（MinerU 精准解析 API v4）----------
    # token **不放这里**：它是长期凭据，走 `data/secrets/mineru.key`（见 `read_secret`）。
    # 官方文档没写有效期，社区口径是 90 天且**没有续期接口** → 过期只能重新生成，
    # 所以这里只提供"读 + 校验"（`api/mineru.py` 的 verify）。
    mineru_base_url: str = "https://mineru.net/api/v4"
    mineru_engine_default: str = "local"     # local | mineru（导入弹窗的初始值）
    mineru_model_version: str = "pipeline"   # pipeline | vlm（vlm 复杂版面更强）
    #: OCR 语言。官方默认是 `ch`，但本系统面向英文论文，默认 `en` 才不至于把英文
    #: 认成中文（`language` 只影响 OCR；有文本层的 PDF 基本用不到）。
    mineru_language: str = "en"
    mineru_poll_interval: float = 2.0        # 轮询起步间隔（指数退避到 30s）
    mineru_poll_deadline: float = 1800.0     # 单个任务总等待上限（秒）
    mineru_upload_timeout: float = 900.0
    mineru_download_timeout: float = 600.0
    #: 超过这个页数就记一条告警（官方两处文档对上限说法不一致：600 页 / 200 页）
    mineru_page_warn: int = 200

    # ---------- 翻译 ----------
    # 逐页翻译**固定串行**（并发窗口恒为 1），**不再提供并发设置**：
    # 并发会让第 N 页在"第 N-1 页还没落盘"时拿不到前文（实测 prev_context_chars=0），
    # 既丢连贯性、又把"逐页追加式前文"的前缀缓存打碎；串行后相邻页 prompt 共享最长公共前缀，
    # 命中率最高（实测稳定态 80%+，逐页 35%→99% 递增）。
    translate_backoff_base: float = 1.0
    translate_backoff_cap: float = 30.0
    translate_breaker_threshold: int = 8
    translate_recover_successes: int = 5
    translate_skip_types: str = "ref"   # 逐段翻译跳过的 block 类型（逗号分隔）
    translate_timeout: float = 120.0
    translate_max_context_chars: int = 1200

    # ---------- 任务系统 ----------
    # 3 = 两路页级任务（TRANSCRIBE_EN / TRANSLATE_ZH）各占一个 worker，第三个留给
    # 解析 / 索引 / 对账 —— 设成 2 的话两路一开，索引任务就得排在后面等。
    task_worker_count: int = 3
    task_poll_interval: float = 0.8
    task_lease_seconds: int = 120
    task_max_retries: int = 2

    # ---------- 模型 / 密钥 ----------
    secrets_dir: Path = PROJECT_ROOT / "data" / "secrets"
    default_generation_model: str = ""   # 空 = 使用 mock provider
    allow_general_knowledge: bool = False  # RAG 允许通用知识开关（规划 §19.5）

    # ---------- 日志 ----------
    log_level: str = "INFO"
    log_to_file: bool = True

    # ---------- Agent（论文问答，独立进程 :8711，见 docs/agent-接入规划.md）----------
    #: 关掉后主后端的 `/agent/ask` 直接返回"未启用"，前端据此隐藏入口
    agent_enabled: bool = True
    agent_host: str = "127.0.0.1"
    agent_port: int = 8711
    #: 主后端 ↔ agent 服务的内部令牌（**不是**模型密钥）。两侧都读同一个密钥文件；
    #: 首次启动主后端会自动生成一个，所以用户不需要手填。
    agent_token_ref: str = "agent.token"
    #: **技能库的最终落点**（L1 名字与描述常驻提示词，L2 正文按需加载）。
    #: 
    #: 为什么不直接读仓库里那份：技能要能被用户上传/禁用/卸载，而仓库目录会被
    #: `git reset` 抹掉。所以落点是 `<data_dir>/skills`（部署后 data/ 不被版本管理动），
    #: 自带技能由 `agent_builtin_skills_dir` **幂等物化**进去（见 `ensure_dirs`）。
    agent_skills_dir: Path = PROJECT_ROOT / "data" / "skills"
    #: 自带技能的**源目录**（随仓库分发：只能禁用、不能卸载）。
    agent_builtin_skills_dir: Path = PROJECT_ROOT / "backend" / "agent" / "skills"
    #: 单轮工具调用轮数上限。论文问答的正常形态是"检索 → 读 1~2 页 → 作答"（3~4 轮），
    #: 给 8 是为了容错：跨库问题（"库里还有谁讲过 X"）模型会多查几次，实测能吃到 6 轮 ——
    #: 卡太紧的后果不是"少答一点"，而是**轮数用满、一个字都没输出**。
    #: 
    #: 2026-10-03 用户口径："工具调用轮数调大（50 轮以上）"—— 8 轮对"整篇对着讲 / 逐个公式核对 /
    #: 库里横向比"这类要读十几处的问法确实太紧，于是放到 60。上限本身仍然保留（见下面
    #: `agent_search_budget`：真正会被滥用的**检索**另有独立预算，轮数是防跑飞的兜底）。
    agent_max_rounds: int = 60
    #: 单轮总时限（秒）。Cylit 的检索比 Rust FTS 慢，给得比参考实现宽。
    #: 600s 是与 60 轮配套的：每轮一次模型调用，卡在 120s 会让轮数上限形同虚设
    #: （真正生效的变成"超时"）。SSE 这一路两侧都没有读超时（见 `bridge._timeout`：`read=None`）。
    agent_deadline_seconds: float = 600.0
    #: SSE 心跳间隔（保连接；前端忽略该事件）
    agent_heartbeat_seconds: float = 5.0
    #: 一次检索返回的候选条数上限（进模型前还会再截一次）
    agent_search_top_k: int = 8
    #: **检索预算**（与轮数预算分开扣，见规划 M7）。
    #: 
    #: 为什么必须单独设一个：实测"库里还有谁讲过 X"这类问题，模型会换着说法连发 10+ 次
    #: `kb_search`（每次都确实能捞到几条新片段，所以"没有新依据"的提示也不拦它），
    #: 最后把轮数用满、正文一个字没写。轮数上限管不住它，因为一次工具调用只占一轮。
    #: 用尽后检索工具直接回"预算用尽，请基于已有材料作答"。
    agent_search_budget: int = 5
    #: **改动预算**：历史字段，2026-11-26 起**不再设闸**（它挡的是正常的批量写入，见 deps.spend_edit）。
    #: 原设计是 12 处/轮，与检索预算、轮数三者互不顶替；实际挡住的是一次「把这些都写成笔记」的
    #: 正常批量写入（笔记 / 高亮都可删可撤，风险由「可撤回」兜）。现在只记账，不再拒绝。
    #: 
    agent_edit_budget: int = 12      # 已不再生效，保留只为 /agent/status 上报
    #: 注入模型的证据条数 / 字符上限（百万上下文也不是无限免费，仍要设闸）
    agent_evidence_limit: int = 12
    agent_evidence_chars: int = 24000
    #: 单轮预注入的档案 + 大纲字符上限
    agent_archive_chars: int = 6000
    #: 会话上下文的"预算"（tiktoken 估算的 token 数）。**不是模型的硬上限**（模型是百万上下文），
    #: 而是"聊到多大就该压一压"的提醒线 —— 前端那个圆环显示的就是它，点击即压缩。
    agent_context_budget: int = 200000
    #: 一次 `page_image_get` 允许的最大页数与最长边（视觉输入的字节闸）
    agent_image_max_pages: int = 3
    agent_image_max_dpi: int = 400
    #: 用户在对话里上传/粘贴的图片（附件）。**张数不设上限**（用户明确要求"不限制"），
    #: 只有两个工程闸：单张字节数，以及单个文件允许的 MIME。
    agent_attachment_max_bytes: int = 12 * 1024 * 1024
    agent_attachment_mimes: tuple[str, ...] = (
        "image/png", "image/jpeg", "image/webp", "image/gif",
    )
    #: 一轮里**真正送进模型**的图片张数上限。第 N 张之后仍然存着（历史里看得见、
    #: 下一轮还能用），只是这一轮不进 prompt —— 否则 50 张图的请求会被厂商直接拒掉。
    agent_attachment_inline_limit: int = 12
    #: **超预算自动压缩**的触发线（占 `agent_context_budget` 的百分比）。
    #: 到线之后下一轮提问会先自动压一次（多一轮 summary），用户不用自己盯圆环。
    #: 用户口径（2026-10-03）：**不限制轮数**，只在这条线附近压 —— 所以 85 → 90。
    agent_auto_compress_percent: float = 90.0

    # ------------------------------------------------------------------
    @field_validator("data_dir", mode="after")
    @classmethod
    def _abs_data_dir(cls, v: Path) -> Path:
        return v if v.is_absolute() else (PROJECT_ROOT / v).resolve()

    @model_validator(mode="after")
    def _skills_under_data_dir(self) -> "Settings":
        """默认技能库跟着 `data_dir` 走。

        改了 `PR_DATA_DIR` 之后技能库必须一起搬：否则"默认值"和"数据目录"分成两家，
        禁用/上传会写进一个跟业务数据无关的地方（而且下次换数据目录就全丢）。
        显式给了 `PR_AGENT_SKILLS_DIR` 时不动 —— 那是用户自己的决定。
        """
        default = PROJECT_ROOT / "data" / "skills"
        if self.agent_skills_dir == default and self.data_dir != PROJECT_ROOT / "data":
            self.agent_skills_dir = self.data_dir / "skills"
        return self

    # ---------- 派生路径 ----------
    @property
    def papers_dir(self) -> Path:
        return self.data_dir / "papers"

    @property
    def exports_dir(self) -> Path:
        return self.data_dir / "exports"

    @property
    def index_dir(self) -> Path:
        return self.data_dir / "index"

    @property
    def uploads_dir(self) -> Path:
        return self.data_dir / "uploads"

    @property
    def trash_dir(self) -> Path:
        return self.data_dir / "trash"

    @property
    def logs_dir(self) -> Path:
        return self.data_dir / "logs"

    @property
    def fonts_dir(self) -> Path:
        return self.data_dir / "fonts"

    @property
    def attachments_dir(self) -> Path:
        """对话里的图片附件（见 `services/agent/attachments.py`）。"""
        return self.data_dir / "attachments"

    @property
    def database_url(self) -> str:
        if self.db_backend == "sqlite":
            return f"sqlite:///{(self.data_dir / 'papermind.sqlite3').as_posix()}"
        return (
            f"mysql+pymysql://{self.db_user}:{self.db_password}"
            f"@{self.db_host}:{self.db_port}/{self.db_name}?charset=utf8mb4"
        )

    @property
    def admin_database_url(self) -> str:
        """不带库名的连接串，用于 CREATE DATABASE。"""
        if self.db_backend == "sqlite":
            return self.database_url
        return (
            f"mysql+pymysql://{self.db_user}:{self.db_password}"
            f"@{self.db_host}:{self.db_port}/?charset=utf8mb4"
        )

    @property
    def skip_translate_types(self) -> set[str]:
        return {t.strip() for t in self.translate_skip_types.split(",") if t.strip()}

    def paper_dir(self, paper_id: str) -> Path:
        return self.papers_dir / paper_id

    def ensure_dirs(self) -> None:
        for p in (
            self.data_dir, self.papers_dir, self.exports_dir, self.index_dir,
            self.uploads_dir, self.trash_dir, self.logs_dir, self.fonts_dir,
            self.attachments_dir, self.secrets_dir, self.agent_skills_dir,
        ):
            p.mkdir(parents=True, exist_ok=True)
        self.ensure_builtin_skills()

    def ensure_builtin_skills(self) -> None:
        """把仓库里的自带技能**幂等**物化进技能库（失败只记日志）。

        挂在这里（而不是只在主后端的 lifespan 里）是因为 agent 是**独立进程**：
        它可能比主后端先起、甚至主后端压根没起（PM2 单独重启 `cylit-agent`），
        那时技能库必须已经是有内容的样子。`get_settings()` 两个进程都会走一次，
        函数本身按内容哈希判断"要不要写"，重复调用的代价就是读 9 个文件。

        **失败不能让进程起不来**：技能只影响问答效果，不该连累主后端/agent 启动。
        """
        try:
            from backend.services.agent import skills_store

            skills_store.ensure_builtin_skills(self)
        except Exception as exc:  # noqa: BLE001
            import logging

            logging.getLogger(__name__).warning("自带技能物化失败（忽略）：%s", exc)

    # ---------- 密钥 ----------
    def read_secret(self, name: str) -> str | None:
        """从 data/secrets/<name> 读取密钥；也接受 <name>.txt。"""
        if not name:
            return None
        for candidate in (self.secrets_dir / name, self.secrets_dir / f"{name}.txt"):
            if candidate.is_file():
                return candidate.read_text(encoding="utf-8").strip()
        env_val = os.environ.get(f"PR_SECRET_{name.upper()}")
        return env_val.strip() if env_val else None

    # ---------- MinerU token ----------
    @property
    def mineru_token_file(self) -> Path:
        return self.secrets_dir / "mineru.key"

    # ---------- Agent 内部令牌 ----------
    @property
    def agent_token_file(self) -> Path:
        return self.secrets_dir / self.agent_token_ref

    def agent_token(self, *, create: bool = False) -> str | None:
        """主后端 ↔ agent 服务的内部令牌。

        与模型密钥走同一套（``data/secrets/`` 文件优先，其次 ``PR_AGENT_TOKEN`` 环境变量），
        区别只在于**缺失时可以自动生成**：这是两侧共享的进程间凭据，让用户为了跑起来
        先手造一个文件没有意义（模型密钥则必须用户自己填，我们造不出来）。
        """
        token = (self.read_secret(self.agent_token_ref)
                 or (os.environ.get("PR_AGENT_TOKEN") or "").strip() or None)
        if token or not create:
            return token
        import secrets as _secrets

        token = _secrets.token_urlsafe(32)
        self.secrets_dir.mkdir(parents=True, exist_ok=True)
        self.agent_token_file.write_text(token, encoding="utf-8")
        return token

    # ---------- 问答运行限额的落盘覆盖 ----------
    #
    # 为什么设成**文件 + 重启生效**而不是即时热更：agent 是独立进程（PM2 `cylit-agent`），
    # 主后端改一个数没法"推"进它的内存 —— 真做热更就要加一条控制通道，而用户已经接受
    # "改完重启问答服务"，界面上也明说了。所以这里只负责**写下**去，读是各进程启动时的事。
    #
    # 两个文件：
    # - `agent_limits.json`        —— 用户保存的覆盖值（`{"values": {...}, "saved_at": ...}`）
    # - `agent_limits.applied.json` —— agent 进程**真正读进去那一刻**的实际生效值 + 时间戳
    #   （`GET /agent/limits` 的 `restart_required` = saved_at 比 applied_at 新）
    @property
    def agent_limits_file(self) -> Path:
        return self.data_dir / "agent_limits.json"

    @property
    def agent_limits_applied_file(self) -> Path:
        return self.data_dir / "agent_limits.applied.json"

    def agent_limits_current(self) -> dict[str, Any]:
        """本进程里**当前生效**的六个值（契约键）。"""
        out: dict[str, Any] = {}
        for key, (field, kind, _low, _high) in AGENT_LIMITS.items():
            out[key] = kind(getattr(self, field))
        return out

    def _read_limits_file(self, path: Path) -> tuple[dict[str, Any], Any]:
        """`(values, stamp)`；文件缺失/坏了都当"没有"。"""
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return {}, None
        if not isinstance(payload, dict):
            return {}, None
        values = normalize_agent_limits(payload.get("values") or {}, strict=False)
        return values, _parse_stamp(payload.get("saved_at") or payload.get("applied_at"))

    def agent_limits_saved(self) -> dict[str, Any]:
        return self._read_limits_file(self.agent_limits_file)[0]

    def agent_limits_payload(self) -> dict[str, Any]:
        """给 `GET /agent/limits` 的六个值：**保存过就显示保存的**，否则是当前生效值。"""
        return {**self.agent_limits_current(), **self.agent_limits_saved()}

    def save_agent_limits(self, values: dict[str, Any]) -> dict[str, Any]:
        """合并写入覆盖值，返回写后的完整六项。"""
        merged = {**self.agent_limits_saved(), **normalize_agent_limits(values)}
        payload = {"values": merged,
                   "saved_at": datetime.now(timezone.utc).isoformat()}
        self.agent_limits_file.parent.mkdir(parents=True, exist_ok=True)
        self.agent_limits_file.write_text(
            json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
        return merged

    def mark_agent_limits_applied(self, values: dict[str, Any] | None = None) -> None:
        """agent 启动成功后调用：记下"实际生效值 + 这一刻"。"""
        payload = {
            "values": dict(values or self.agent_limits_current()),
            "applied_at": datetime.now(timezone.utc).isoformat(),
            "pid": os.getpid(),
        }
        self.agent_limits_applied_file.parent.mkdir(parents=True, exist_ok=True)
        self.agent_limits_applied_file.write_text(
            json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")

    def agent_limits_restart_required(self) -> bool:
        """保存过之后还没被任何 agent 进程读进去 → True。"""
        _values, saved_at = self._read_limits_file(self.agent_limits_file)
        if saved_at is None:
            return False
        _applied, applied_at = self._read_limits_file(self.agent_limits_applied_file)
        return applied_at is None or saved_at > applied_at

    def apply_agent_limits(self) -> list[str]:
        """把保存的覆盖值贴到本进程的 `Settings` 上，返回被改动的字段名。

        在 `get_settings()` 里调一次（进程启动时）。**主后端也要贴**：
        `context_budget` / `compact_threshold` 这两项真正的读取点在主后端
        （`services/agent/conversations.conversation_usage` 与 `api/agent._auto_compress`），
        不贴的话这两项在界面上能改、实际却永远不生效 —— 那是最难查的那种"静默失效"。
        """
        changed: list[str] = []
        for key, value in self.agent_limits_saved().items():
            field = AGENT_LIMITS[key][0]
            if getattr(self, field) != value:
                setattr(self, field, value)
                changed.append(field)
        return changed

    @property
    def mineru_token(self) -> str | None:
        """MinerU token：密钥文件优先，其次 `PR_MINERU_TOKEN` 环境变量。

        为什么额外认一个环境变量：CI / 临时验证时不该把长期凭据落到工作区文件里
        （`read_secret` 那条 `PR_SECRET_MINERU.KEY` 的推导名带点号，不适合手写）。
        """
        return (self.read_secret("mineru.key")
                or (os.environ.get("PR_MINERU_TOKEN") or "").strip() or None)


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    s = Settings()
    s.ensure_dirs()
    # 问答运行限额的覆盖值：**进程启动时贴一次**（界面上明说"重启后生效"）。
    # 主后端与 agent 都从这里拿到同一份数字。
    s.apply_agent_limits()
    return s


def reload_settings() -> Settings:
    get_settings.cache_clear()
    return get_settings()


settings = get_settings()


def bootstrap_sys_path() -> None:
    """让 ``backend`` 作为顶层包可被 import（CLI / 测试共用）。"""
    if str(BACKEND_DIR.parent) not in sys.path:
        sys.path.insert(0, str(BACKEND_DIR.parent))
