"""Agent 主后端侧的服务层：会话落库、工具投影、模型配置、SSE 转发。

分四个模块，边界与 `docs/agent-接入规划.md` §4 一一对应：

- `project.py`      —— 给 agent 的只读投影（工具面唯一的数据出口）
- `conversations.py`—— 会话与消息落库（主后端是唯一写入方）
- `model_config.py` —— 模型配置下发（密钥唯一入口仍在设置页）
- `bridge.py`       —— 到 agent 进程的 SSE 字节级转发

⚠️ 本包**不能** import `backend.agent.*`：那会把 pydantic-ai 拉进主后端的导入图。
"""
