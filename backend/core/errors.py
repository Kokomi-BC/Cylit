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
    http_status = 503

    def __init__(self, model_type: str):
        super().__init__(
            f"未配置可用的 {model_type} 模型。请在 /models 中新增模型并设为默认，"
            f"或配置 data/secrets/ 下的密钥。",
            retryable=False,
        )


class TaskError(PaperMindError):
    code = "TASK_ERROR"
    http_status = 500
