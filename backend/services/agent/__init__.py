"""Agent 主后端侧的服务层：会话落库、工具投影、模型配置、SSE 转发。

分五个模块，边界与 `docs/agent-接入规划.md` §4 一一对应：

- `project.py`      —— 给 agent 的只读投影（工具面唯一的数据出口）
- `conversations.py`—— 会话与消息落库（主后端是唯一写入方）
- `model_config.py` —— 模型配置下发（密钥唯一入口仍在设置页）
- `style.py`        —— 问答**风格**：提示词里唯一可由设置页改的那一段
- `bridge.py`       —— 到 agent 进程的 SSE 字节级转发

⚠️ 本包**不能** import `backend.agent.*`：那会把 pydantic-ai 拉进主后端的导入图。
唯一例外是 `style.py` 里**惰性**读 `prompts.DEFAULT_STYLE`：`prompts.py` 的导入链
（deps/citations/config/host）只有 httpx 与 dataclass，碰不到 pydantic-ai ——
`tests/test_agent_prompts.py::test_style_default_import_stays_light` 守着这条。
"""
