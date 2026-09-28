"""在线解析（MinerU 精准解析 API v4）：HTTP 客户端 + 结果归一化构建器。

与 ``parsing.pipeline``（本地 PyMuPDF 自研版面）**并列的第二条解析引擎**。
两条路最终产出同一份 :class:`backend.parsing.models.ParseResult`（IR + Markdown），
所以阅读器、逐页翻译、知识库索引、论文理解都**不需要区分引擎** —— 这是"选引擎"
这件事只落在导入/重解析入口的原因。

分两层，各自的依赖边界很硬：

- :mod:`backend.parsing.online.mineru_client`：只做"提交 → 轮询 → 下载 → 解包"，
  不碰数据库、不碰服务层（与 ``parsing.pipeline`` 同一条约束，CLI 也能直接跑）；
- :mod:`backend.parsing.online.mineru_builder`：把解包出来的 ``content_list.json``
  归一化成 ParseResult。

官方文档：https://mineru.net/apiManage/docs ｜ token：https://mineru.net/apiManage/token
（**token 每 90 天过期**，过期后返回 ``A0211``，见 ``ERROR_HINTS``）。
"""

from __future__ import annotations

from backend.parsing.online.mineru_builder import MineruBuilder
from backend.parsing.online.mineru_client import (
    MineruAuthError, MineruClient, MineruError, MineruQuotaError,
    MineruTimeoutError, mask_token,
)

__all__ = [
    "MineruBuilder", "MineruClient", "MineruError", "MineruAuthError",
    "MineruQuotaError", "MineruTimeoutError", "mask_token",
]
