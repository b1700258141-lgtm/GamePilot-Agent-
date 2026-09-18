"""HTTP 工具：把被测服务的接口包装成带错误分类的可观测请求。

`httpx.AsyncClient` 由外部注入：CLI 传入真实网络客户端，
测试传入 `ASGITransport` 驱动的客户端，两者走同一份适配器，
因此被测路径与分类逻辑完全一致。客户端生命周期由调用方负责（谁创建谁关闭）。

请求一律使用本次命令提供的 base_url 组成绝对地址：目标地址不来自报告或环境，
重跑时也不会悄悄沿用上次的地址。默认超时 5 秒，动作失败不自动重试。

错误分类只记录异常类型名，不记录异常文本：httpx 的异常消息可能带上
请求 URL 等信息，而报告是长期保存的证据文件。
"""

import time
from urllib.parse import urlsplit, urlunsplit

import httpx

from .models import ActionName, HttpObservation
from .rules import DEFAULT_TIMEOUT_SECONDS, INFRASTRUCTURE_ERROR_CODES


def _parse_json_object(response: httpx.Response) -> dict[str, object] | None:
    """把响应体解析成 JSON 对象；不是对象（或不是 JSON）时返回 None。"""
    try:
        parsed = response.json()
    except ValueError:
        return None
    return parsed if isinstance(parsed, dict) else None


def redact_url(url: str) -> str:
    """去掉 URL 中的凭据、查询串与片段，只保留可直接写进报告的地址。"""
    parts = urlsplit(url)
    host = parts.hostname or ""
    if parts.port is not None:
        host = f"{host}:{parts.port}"
    return urlunsplit((parts.scheme, host, parts.path.rstrip("/"), "", ""))


class GameClient:
    """被测服务的 HTTP 工具适配器。"""

    def __init__(
        self,
        http: httpx.AsyncClient,
        *,
        base_url: str,
        timeout: float = DEFAULT_TIMEOUT_SECONDS,
    ) -> None:
        self._http = http
        # 请求用的是完整地址；写进报告的地址先去掉凭据、查询串与片段，
        # 报告是长期保存的证据文件，不能成为凭据的传播渠道。
        self._request_base = base_url.rstrip("/")
        self._reported_base = redact_url(base_url)
        self._timeout = timeout

    @property
    def base_url(self) -> str:
        """写进报告的地址（已脱敏）。"""
        return self._reported_base

    @property
    def timeout(self) -> float:
        return self._timeout

    async def create_session(self, seed: int) -> HttpObservation:
        """POST /api/v1/game-sessions"""
        return await self._send("POST", "/api/v1/game-sessions", json_body={"seed": seed})

    async def get_session(self, session_id: str) -> HttpObservation:
        """GET /api/v1/game-sessions/{session_id}"""
        return await self._send("GET", f"/api/v1/game-sessions/{session_id}")

    async def perform_action(self, session_id: str, action: ActionName) -> HttpObservation:
        """POST /api/v1/game-sessions/{session_id}/actions"""
        return await self._send(
            "POST",
            f"/api/v1/game-sessions/{session_id}/actions",
            json_body={"action": action},
        )

    async def _send(
        self,
        method: str,
        path: str,
        *,
        json_body: dict[str, object] | None = None,
    ) -> HttpObservation:
        started = time.perf_counter()
        try:
            response = await self._http.request(
                method,
                f"{self._request_base}{path}",
                json=json_body,
                timeout=self._timeout,
            )
        except httpx.TimeoutException as exc:
            return self._transport_failure(method, path, "timeout", exc, started)
        except httpx.TransportError as exc:
            return self._transport_failure(method, path, "connection", exc, started)
        except Exception as exc:  # noqa: BLE001 - 适配器自身异常也要归类，不能让运行崩掉
            return self._transport_failure(method, path, "unexpected_error", exc, started)

        elapsed_ms = (time.perf_counter() - started) * 1000
        status_code = response.status_code
        parsed = _parse_json_object(response)
        if status_code >= 500:
            # 5xx 是执行错误：保留状态码与响应体作为证据，但不判定为游戏缺陷。
            # 先判状态码再判响应体：5xx 的具体原因（网关、崩溃页）比「不是 JSON」更重要。
            detail = f"server returned HTTP {status_code}"
            if parsed is None:
                detail += "（响应体不是 JSON 对象）"
            return HttpObservation(
                method=method,
                path=path,
                status_code=status_code,
                body=parsed,
                error_kind="server_error",
                error_detail=detail,
                elapsed_ms=elapsed_ms,
            )
        if parsed is None:
            return HttpObservation(
                method=method,
                path=path,
                status_code=status_code,
                error_kind="invalid_response",
                error_detail="response body is not a JSON object",
                elapsed_ms=elapsed_ms,
            )
        code = parsed.get("code")
        persistence_error = isinstance(code, str) and code in INFRASTRUCTURE_ERROR_CODES
        return HttpObservation(
            method=method,
            path=path,
            status_code=status_code,
            body=parsed,
            error_kind="persistence_error" if persistence_error else None,
            error_detail=code if persistence_error else None,
            elapsed_ms=elapsed_ms,
        )

    def _transport_failure(
        self,
        method: str,
        path: str,
        kind: str,
        exc: Exception,
        started: float,
    ) -> HttpObservation:
        return HttpObservation(
            method=method,
            path=path,
            status_code=None,
            error_kind=kind,  # type: ignore[arg-type]
            error_detail=type(exc).__name__,
            elapsed_ms=(time.perf_counter() - started) * 1000,
        )
