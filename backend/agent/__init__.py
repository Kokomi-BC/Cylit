"""Cylit 的论文问答 Agent（独立进程，默认 :8711）。

**边界**（`docs/agent-接入规划.md` §4.2）：

- 不直连 MySQL / LanceDB / 产物目录 —— 论文数据一律经主后端 HTTP 取；
- 不持有模型密钥 —— 模型配置由主后端下发；
- 没有任何可写文件 / 执行命令的能力 —— workspace 是**构造上只读**的（见 `workspace.py`）。

对外只有四个接口：`POST /v1/ask`（SSE）、`GET /v1/skills`、`POST /v1/skills/reload`、
`GET /readyz`。启动：``python -m backend.agent``（或 ``pr.py agent``）。
"""
