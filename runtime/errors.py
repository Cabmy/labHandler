"""Error taxonomy：纯枚举 + 无状态 classify。"""

from enum import Enum


class ErrorClass(str, Enum):
    OK = "ok"
    TRANSIENT = "transient"
    VALIDATION = "validation"
    PERMISSION = "permission"
    ENVIRONMENT = "environment"
    LOGIC = "logic"
    ACCEPTABLE = "acceptable"
    AUTH = "auth"
    FATAL = "fatal"
    LOOP = "loop"


_AUTH_MARKERS = (
    "unauthorized client detected",
    "invalid api key",
    "incorrect api key",
    "authentication",
)

_TRANSIENT_TYPES = {
    "TimeoutError",
    "asyncio.TimeoutError",
    "APITimeoutError",
    "RateLimitError",
    "InternalServerError",
    "APIConnectionError",
    "ConnectError",
    "ReadTimeout",
    "WriteTimeout",
}


def classify(exc: BaseException | None, *, text: str = "") -> ErrorClass:
    """把异常或工具正文映射到 ErrorClass。不含状态、不做处置。"""
    blob = f"{type(exc).__name__ if exc else ''} {exc or ''} {text}".lower()
    if any(m in blob for m in _AUTH_MARKERS) or "401" in blob or "403" in blob:
        if "permission" in blob and "401" not in blob:
            pass
        else:
            if any(m in blob for m in _AUTH_MARKERS) or "401" in blob:
                return ErrorClass.AUTH
            if "403" in blob and "forbidden" in blob:
                return ErrorClass.AUTH

    if exc is not None:
        name = type(exc).__name__
        qual = f"{type(exc).__module__}.{name}"
        if name in _TRANSIENT_TYPES or qual.endswith("TimeoutError"):
            return ErrorClass.TRANSIENT
        if name in {"PermissionError"}:
            return ErrorClass.PERMISSION
        if name in {"FileNotFoundError", "NotADirectoryError", "IsADirectoryError"}:
            return ErrorClass.ENVIRONMENT
        if name in {"json.JSONDecodeError", "JSONDecodeError", "ValidationError"}:
            return ErrorClass.VALIDATION
        if "rate limit" in blob or "429" in blob or "5xx" in blob or "status 5" in blob:
            return ErrorClass.TRANSIENT
        if "sandbox_unreachable" in blob:
            return ErrorClass.FATAL

    if "[error/permissionerror]" in blob:
        return ErrorClass.PERMISSION
    if "[sandbox_unreachable]" in blob:
        return ErrorClass.FATAL
    if "timeout" in blob or "429" in blob or "rate limit" in blob:
        return ErrorClass.TRANSIENT
    if "malformed" in blob or "invalid json" in blob or "schema" in blob and "fail" in blob:
        return ErrorClass.VALIDATION
    if "not found" in blob or "no such file" in blob:
        return ErrorClass.ENVIRONMENT
    return ErrorClass.LOGIC if exc is not None else ErrorClass.OK
