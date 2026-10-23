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
        # detail 复制一份再往外给：错误信封是一次快照，调用方之后的补充（上游字段）
        # 不该反写回异常对象，也不该让两个响应共享同一个 dict。
        detail = dict(self.detail)
        provider = provider_detail_of(self)
        message = self.message
        if provider is not None:
            # 已经填过的（model_manager 在调用现场补的那份，带 model / provider_label）
            # 优先：那里才知道"用的是哪个模型、哪把 key"，这里再算一次只会更少信息
            detail.setdefault("provider", provider)
            # 兜底的 message 里可能裹着上游原文（"...: str(exc)[:200]"），密钥一律抹掉
            from backend.core import provider_errors

            message = provider_errors.redact(message)
        return {"code": self.code, "message": message, "detail": detail}


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
    """模型调用失败（可重试时由调度器捕获）。

    信封里的结构化上游字段（HTTP 状态码 / 上游错误码 / 分类）由
    provider_errors.classify 统一产出，落在 detail.provider；它与 SSE 的 error 帧
    是**同一份**对象（契约见 backend/core/provider_errors.py）。
    """

    code = "PROVIDER_ERROR"
    http_status = 502

    def __init__(self, message: str, *, retryable: bool = True,
                 retry_after: float | None = None, detail: dict | None = None,
                 model: str = "", provider_label: str = "",
                 secrets: tuple = ()):
        super().__init__(message, detail=detail)
        self.retryable = retryable
        self.retry_after = retry_after
        #: 这一条错误属于哪个模型、哪个供应商（显示名），以及哪把密钥要参与脱敏。
        #: 只影响 detail.provider 的填充，**不影响**重试判断与 HTTP 状态码。
        self.model = model
        self.provider_label = provider_label
        self.secrets = secrets


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


def provider_detail_of(exc: BaseException) -> dict | None:
    """异常的 detail.provider 字段（唯一产出点，见 provider_errors）。

    只给"上游失败"与"本地限额"两类：校验 / 找不到这类业务异常没有供应商语义，
    多给一个 kind 只会让前端以为每条错误都能重试。

    为什么不在两个调用点各拼一份：HTTP 信封与 SSE 帧必须说同一件事 ——
    两处各拼一份就是两份随时会分叉的契约。
    """
    from backend.core import provider_errors

    if isinstance(exc, ProviderError):
        return provider_errors.classify(
            exc, model=getattr(exc, "model", "") or "",
            provider_label=getattr(exc, "provider_label", "") or "",
            secrets=getattr(exc, "secrets", ()) or ())
    if isinstance(exc, LimitExceededError):
        return provider_errors.classify(exc)
    return None


class TaskError(PaperMindError):
    code = "TASK_ERROR"
    http_status = 500
