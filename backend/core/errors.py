"""统一错误类型与 API 错误码。

API 层统一响应契约 ``{data, error, trace_id}``（规划 §20 约定）。
"""

from __future__ import annotations


class PaperMindError(Exception):
    """所有业务异常的基类。"""

    code = "INTERNAL_ERROR"
    http_status = 500

    def __init__(self, message: str, *, detail: dict | None = None):
        super().__init__(message)
        self.message = message
        self.detail = detail or {}

    def to_dict(self) -> dict:
        return {"code": self.code, "message": self.message, "detail": self.detail}


class NotFoundError(PaperMindError):
    code = "NOT_FOUND"
    http_status = 404


class ValidationError(PaperMindError):
    code = "VALIDATION_ERROR"
    http_status = 400


class ConflictError(PaperMindError):
    code = "CONFLICT"
    http_status = 409


class StorageError(PaperMindError):
    code = "STORAGE_ERROR"
    http_status = 500


class ParseError(PaperMindError):
    code = "PARSE_ERROR"
    http_status = 422


class ProviderError(PaperMindError):
    """模型调用失败（可重试时由调度器捕获）。"""

    code = "PROVIDER_ERROR"
    http_status = 502

    def __init__(self, message: str, *, retryable: bool = True,
                 retry_after: float | None = None, detail: dict | None = None):
        super().__init__(message, detail=detail)
        self.retryable = retryable
        self.retry_after = retry_after


class RateLimitError(ProviderError):
    code = "RATE_LIMITED"
    http_status = 429


class ModelNotConfiguredError(ProviderError):
    code = "MODEL_NOT_CONFIGURED"
    # 缺模型是用户去 /models 就能自解的状态，不是服务端临时故障：别用 5xx，
    # 否则前端/网关会当成"稍后重试"，而 retryable=False 说明重试永远没用。
    http_status = 400

    def __init__(self, model_type: str):
        super().__init__(
            f"未配置可用的 {model_type} 模型。请在「设置 · 模型」里新增一个并设为默认，"
            f"密钥也在同一个分区写入。",
            retryable=False,
        )


class LimitExceededError(PaperMindError):
    """**本地**限额触发（模型 / 密钥 / 厂商三级，按日 / 周 / 月）。

    三条理由说明它为什么**不继承** `ProviderError`：

    1. `code` 必须是 `LIMIT_EXCEEDED`，**绝不能伪装成供应商 429**
       （`RateLimitError` 是「人家不让你调了，等一会儿再来」，这里是「你自己设的
       额度用完了，等一会儿也没用」—— 两者混在一起，用户会去找厂商理论）；
    2. `ProviderError` 默认 `retryable=True`，调度器会把它当临时故障重试；
       可限额在下一个周期到来之前**永远不会自己松开**，重试只是把配额表刷满日志；
    3. 被拦的那次调用**根本没发出去**：没有 token、没有钱、也没有厂商响应，
       按「调用失败」记一笔账就是错账（所以落 `status='blocked'` + `cost IS NULL`）。

    `detail["limit"]` 是固定形状（见 `services/model/limits.py` 的
    `SNAPSHOT_FIELDS`），前端横幅只认这一份数。
    """

    code = "LIMIT_EXCEEDED"
    http_status = 429

    def __init__(self, message: str, *, detail: dict | None = None):
        super().__init__(message, detail=detail)
        #: 与 ProviderError 同名但语义相反：额度用尽重试无用。照旧放这个字段，
        #: 免得通用兜底代码 getattr(exc, "retryable", True) 取到默认值 True。
        self.retryable = False


class TaskError(PaperMindError):
    code = "TASK_ERROR"
    http_status = 500
