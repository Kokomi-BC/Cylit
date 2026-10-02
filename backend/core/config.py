"""全局配置。

设计原则（对应规划 §1.2 本地优先、§6 分层）：
- 所有路径集中在 ``Settings``，其余模块一律通过 ``get_settings()`` 取，禁止散落硬编码。
- 密钥不落库、不进代码：``data/secrets/`` 下的独立文件（规划 §12.2 / §31.5）。
- 业务库默认 MySQL 8；无 MySQL 时可用 SQLite 退化运行（离线答辩兜底）。
- 模型名一律不写死在代码里，代码只引用 ``model_type``（规划 §12.2 默认路由）。
"""

from __future__ import annotations

import os
import sys
from functools import lru_cache
from pathlib import Path

from pydantic import Field, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

# backend/core/config.py -> backend/core -> backend -> Cylit（仓库根，2026-09-28 由 paper-reader 改名）
BACKEND_DIR = Path(__file__).resolve().parents[1]
PROJECT_ROOT = BACKEND_DIR.parent


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
    #: skill 目录（L1 名字与描述常驻提示词，L2 正文按需加载）
    agent_skills_dir: Path = PROJECT_ROOT / "backend" / "agent" / "skills"
    #: 单轮工具调用轮数上限。论文问答的正常形态是"检索 → 读 1~2 页 → 作答"，
    #: 给到 6 是为了容错（模型偶尔会多查一次），不是鼓励它多绕。
    agent_max_rounds: int = 6
    #: 单轮总时限（秒）。Cylit 的检索比 Rust FTS 慢，给得比参考实现宽
    agent_deadline_seconds: float = 120.0
    #: SSE 心跳间隔（保连接；前端忽略该事件）
    agent_heartbeat_seconds: float = 5.0
    #: 一次检索返回的候选条数上限（进模型前还会再截一次）
    agent_search_top_k: int = 8
    #: 注入模型的证据条数 / 字符上限（百万上下文也不是无限免费，仍要设闸）
    agent_evidence_limit: int = 12
    agent_evidence_chars: int = 24000
    #: 证据闸的**相关度下限**（重排模型给的最高分）。
    #:
    #: 为什么需要它：向量检索**永远**会返回 top-k，"有没有命中"本身不携带信息，
    #: 不带阈值时护栏永远不会触发（"论文里没写"这类问题会被硬答）。
    #: 实测（qwen3-vl-rerank，Attention Is All You Need）：真问题最高分 0.86~0.87，
    #: 无关问题 0.44~0.59 → 0.65 落在两者之间。
    #: ⚠️ 只在**真重排模型**上生效：启发式降级那套是词重合度，量纲完全不同，
    #: 拿它比阈值会把整库问答全部挡掉。
    agent_evidence_floor: float = 0.65
    #: 单轮预注入的档案 + 大纲字符上限
    agent_archive_chars: int = 6000
    #: 一次 `page_image_get` 允许的最大页数与最长边（视觉输入的字节闸）
    agent_image_max_pages: int = 3
    agent_image_max_dpi: int = 400

    # ------------------------------------------------------------------
    @field_validator("data_dir", mode="after")
    @classmethod
    def _abs_data_dir(cls, v: Path) -> Path:
        return v if v.is_absolute() else (PROJECT_ROOT / v).resolve()

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
            self.secrets_dir,
        ):
            p.mkdir(parents=True, exist_ok=True)

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
    return s


def reload_settings() -> Settings:
    get_settings.cache_clear()
    return get_settings()


settings = get_settings()


def bootstrap_sys_path() -> None:
    """让 ``backend`` 作为顶层包可被 import（CLI / 测试共用）。"""
    if str(BACKEND_DIR.parent) not in sys.path:
        sys.path.insert(0, str(BACKEND_DIR.parent))
