"""Small, configurable OpenAI-compatible Chat Completions/Responses adapter.

The adapter accepts only explicit public prompt messages. It has no access to
campaign files, execution stores, or application services.
"""
from __future__ import annotations

from dataclasses import dataclass, field
import errno
import ipaddress
import json
import os
from pathlib import Path
import re
import socket
import ssl
import time
from typing import Any, Mapping, Sequence
from urllib.parse import urlsplit
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen


_ENV_KEYS = frozenset({
    "ASSAYPILOT_LLM_PROVIDER", "ASSAYPILOT_LLM_ENDPOINT", "ASSAYPILOT_LLM_MODEL",
    "ASSAYPILOT_LLM_API_KEY", "ASSAYPILOT_LLM_API_MODE", "ASSAYPILOT_LLM_AUTH_HEADER",
    "ASSAYPILOT_LLM_AUTH_SCHEME", "ASSAYPILOT_LLM_RESPONSE_FORMAT",
    "ASSAYPILOT_LLM_TIMEOUT_SECONDS", "ASSAYPILOT_LLM_MAX_OUTPUT_TOKENS",
    "ASSAYPILOT_LLM_RETRY_ATTEMPTS", "ASSAYPILOT_LLM_TOKEN_PARAMETER",
})
_SAFE_PROVIDER = re.compile(r"^[a-zA-Z0-9_.-]{1,80}$")
_SAFE_HEADER = re.compile(r"^[!#$%&'*+.^_`|~0-9A-Za-z-]{1,100}$")
_SAFE_AUTH_SCHEME = re.compile(r"^[A-Za-z0-9._~+/-]{0,80}$")


class LLMProviderError(RuntimeError):
    """A provider error with safe metadata; response bodies and credentials omitted."""

    def __init__(self, code: str, message: str, *, status_code: int | None = None,
                 request_id: str | None = None, failure_stage: str | None = None,
                 exception_class: str | None = None, cause_code: str | None = None,
                 cause_class: str | None = None,
                 http_response_received: bool | None = None,
                 latency_ms: int | None = None):
        self.code = code
        self.status_code = status_code
        self.request_id = request_id
        self.failure_stage = failure_stage
        self.exception_class = _safe_class_name(exception_class or type(self).__name__)
        self.cause_code = _safe_diagnostic_code(cause_code)
        self.cause_class = _safe_class_name(cause_class)
        self.http_response_received = http_response_received
        self.latency_ms = latency_ms
        super().__init__(message)

    def diagnostic_metadata(self, *, fallback_stage: str | None = None,
                             fallback_latency_ms: int | None = None) -> dict[str, object]:
        """Return an allowlisted failure record without exception text or response data."""
        received = self.http_response_received
        if received is None:
            received = self.status_code is not None
        return {
            "failure_stage": self.failure_stage or fallback_stage or "provider_call",
            "exception_class": self.exception_class,
            "cause_code": self.cause_code or _safe_diagnostic_code(self.code) or "provider_failed",
            "cause_class": self.cause_class,
            "http_response_received": received,
            "http_status": self.status_code,
            "latency_ms": self.latency_ms if self.latency_ms is not None else fallback_latency_ms,
        }


@dataclass(frozen=True)
class LLMSettings:
    provider: str
    endpoint: str
    model: str
    api_key: str = field(repr=False)
    auth_header: str = "Authorization"
    auth_scheme: str = "Bearer"
    response_format: str = "json_schema"
    token_parameter: str = "max_completion_tokens"
    timeout_seconds: float = 60.0
    max_output_tokens: int = 1200
    retry_attempts: int = 1
    api_mode: str = "auto"

    @property
    def resolved_api_mode(self) -> str:
        return _resolve_api_mode(self.api_mode, self.endpoint)

    @property
    def effective_token_parameter(self) -> str:
        return "max_output_tokens" if self.resolved_api_mode == "responses" else self.token_parameter

    @classmethod
    def from_environment(
        cls,
        *,
        env: Mapping[str, str] | None = None,
        env_file: str | Path | None = None,
    ) -> "LLMSettings":
        """Read settings from process environment and an optional private env file.

        Process environment wins. The default local file is `.env.stage5a.local`
        in the current project directory; it must not be group/world readable.
        """
        values: dict[str, str] = {}
        local_file = Path(env_file) if env_file is not None else Path.cwd() / ".env.stage5a.local"
        if local_file.exists():
            if not local_file.is_file():
                raise LLMProviderError("invalid_settings_file", "LLM settings path must be a regular file")
            if os.name == "posix" and local_file.stat().st_mode & 0o077:
                raise LLMProviderError("insecure_settings_file", "LLM settings file must have mode 0600")
            try:
                for line_no, line in enumerate(local_file.read_text(encoding="utf-8").splitlines(), start=1):
                    stripped = line.strip()
                    if not stripped or stripped.startswith("#"):
                        continue
                    name, separator, value = stripped.partition("=")
                    name = name.strip()
                    if not separator or name not in _ENV_KEYS:
                        raise LLMProviderError("invalid_settings_file", f"invalid LLM setting on line {line_no}")
                    value = value.strip()
                    if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
                        value = value[1:-1]
                    values[name] = value
            except UnicodeDecodeError as exc:
                raise LLMProviderError("invalid_settings_file", "LLM settings file must be UTF-8") from exc
        source = dict(os.environ if env is None else env)
        values.update({key: value for key, value in source.items() if key in _ENV_KEYS})

        provider = values.get("ASSAYPILOT_LLM_PROVIDER", "openai_compatible").strip()
        endpoint = values.get("ASSAYPILOT_LLM_ENDPOINT", "").strip()
        model = values.get("ASSAYPILOT_LLM_MODEL", "").strip()
        api_key = values.get("ASSAYPILOT_LLM_API_KEY", "").strip()
        api_mode = values.get("ASSAYPILOT_LLM_API_MODE", "auto").strip()
        response_format = values.get("ASSAYPILOT_LLM_RESPONSE_FORMAT", "json_schema").strip()
        token_parameter = values.get("ASSAYPILOT_LLM_TOKEN_PARAMETER", "max_completion_tokens").strip()
        auth_header = values.get("ASSAYPILOT_LLM_AUTH_HEADER", "Authorization").strip()
        auth_scheme = values.get("ASSAYPILOT_LLM_AUTH_SCHEME", "Bearer").strip()
        if not _SAFE_PROVIDER.fullmatch(provider):
            raise LLMProviderError("invalid_provider", "provider name contains unsupported characters")
        if not endpoint or not model:
            missing = []
            if not endpoint:
                missing.append("ASSAYPILOT_LLM_ENDPOINT")
            if not model:
                missing.append("ASSAYPILOT_LLM_MODEL")
            if not api_key:
                missing.append("ASSAYPILOT_LLM_API_KEY")
            raise LLMProviderError("missing_settings", "missing LLM settings: " + ", ".join(missing))
        if not api_key:
            raise LLMProviderError("missing_settings", "missing LLM settings: ASSAYPILOT_LLM_API_KEY")
        try:
            parsed_endpoint = urlsplit(endpoint)
            endpoint_host = parsed_endpoint.hostname
            endpoint_port = parsed_endpoint.port
        except ValueError:
            raise LLMProviderError("invalid_endpoint", "endpoint URL is malformed") from None
        if (any(ord(char) <= 32 or ord(char) == 127 for char in endpoint)
                or (parsed_endpoint.scheme != "https" and not _is_loopback_http(parsed_endpoint))
                or not endpoint_host or endpoint_port == 0
                or parsed_endpoint.username is not None
                or parsed_endpoint.password is not None or parsed_endpoint.fragment):
            raise LLMProviderError("invalid_endpoint", "endpoint must use HTTPS (HTTP is allowed only for loopback testing)")
        if not _SAFE_HEADER.fullmatch(auth_header):
            raise LLMProviderError("invalid_auth_header", "authentication header name is invalid")
        if not _SAFE_AUTH_SCHEME.fullmatch(auth_scheme):
            raise LLMProviderError("invalid_auth_scheme", "authentication scheme contains unsupported characters")
        if response_format not in {"json_schema", "json_object", "prompt_only"}:
            raise LLMProviderError("invalid_response_format", "response format must be json_schema, json_object, or prompt_only")
        if api_mode not in {"auto", "chat_completions", "responses"}:
            raise LLMProviderError("invalid_api_mode", "API mode must be auto, chat_completions, or responses")
        if token_parameter not in {"max_tokens", "max_completion_tokens", "max_output_tokens"}:
            raise LLMProviderError("invalid_token_parameter", "token parameter is unsupported")
        timeout = _bounded_float(values.get("ASSAYPILOT_LLM_TIMEOUT_SECONDS", "60"), 1.0, 180.0, "timeout")
        tokens = _bounded_int(values.get("ASSAYPILOT_LLM_MAX_OUTPUT_TOKENS", "1200"), 128, 8192, "output token limit")
        retries = _bounded_int(values.get("ASSAYPILOT_LLM_RETRY_ATTEMPTS", "1"), 0, 1, "retry attempts")
        resolved_mode = _resolve_api_mode(api_mode, endpoint)
        if resolved_mode == "chat_completions" and token_parameter == "max_output_tokens":
            raise LLMProviderError("invalid_token_parameter", "Chat Completions requires max_tokens or max_completion_tokens")
        return cls(provider, endpoint, model, api_key, auth_header, auth_scheme,
                   response_format, token_parameter, timeout, tokens, retries, resolved_mode)


@dataclass(frozen=True)
class ProviderResponse:
    content: str
    provider: str
    model: str
    request_id: str | None
    latency_ms: int
    usage: dict[str, int | str]
    attempts: int


class OpenAICompatibleChatProvider:
    """Call a configured Chat Completions or Responses endpoint with bounded retries."""

    def __init__(self, settings: LLMSettings):
        self.settings = settings

    def complete(self, messages: Sequence[Mapping[str, str]], *, max_output_tokens: int | None = None,
                 json_schema: dict[str, Any] | None = None) -> ProviderResponse:
        if not messages or any(set(message) - {"role", "content"} for message in messages):
            raise ValueError("messages must contain role/content pairs")
        if any(message.get("role") not in {"system", "user", "assistant"}
               or not isinstance(message.get("content"), str) for message in messages):
            raise ValueError("invalid chat message")
        tokens = max_output_tokens or self.settings.max_output_tokens
        if isinstance(tokens, bool) or not isinstance(tokens, int) or not 128 <= tokens <= self.settings.max_output_tokens:
            raise ValueError("max_output_tokens exceeds the configured bound")
        body: dict[str, Any] = {"model": self.settings.model}
        if self.settings.resolved_api_mode == "responses":
            body.update({
                "input": [dict(message) for message in messages],
                "max_output_tokens": tokens,
                "store": False,
            })
            if self.settings.response_format == "json_schema":
                if not isinstance(json_schema, dict):
                    raise ValueError("json_schema response format requires a schema")
                body["text"] = {"format": {
                    "type": "json_schema", "name": "assaypilot_decision",
                    "strict": True, "schema": json_schema,
                }}
            elif self.settings.response_format == "json_object":
                body["text"] = {"format": {"type": "json_object"}}
        else:
            body.update({
                "messages": [dict(message) for message in messages],
                "temperature": 0,
                self.settings.token_parameter: tokens,
            })
            if self.settings.response_format == "json_schema":
                if not isinstance(json_schema, dict):
                    raise ValueError("json_schema response format requires a schema")
                body["response_format"] = {
                    "type": "json_schema",
                    "json_schema": {"name": "assaypilot_decision", "strict": True, "schema": json_schema},
                }
            if self.settings.response_format == "json_object":
                body["response_format"] = {"type": "json_object"}
        payload = json.dumps(body, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
        auth_value = f"{self.settings.auth_scheme} {self.settings.api_key}".strip()
        request = Request(
            self.settings.endpoint, data=payload, method="POST",
            headers={"Content-Type": "application/json", self.settings.auth_header: auth_value},
        )
        start = time.monotonic()
        attempts = 0
        while True:
            attempts += 1
            try:
                with urlopen(request, timeout=self.settings.timeout_seconds) as response:
                    raw = response.read(2_000_001)
                    request_id = _request_id(response.headers)
                    status = response.status
            except HTTPError as exc:
                request_id = _request_id(exc.headers)
                status = exc.code
                if _retryable(status) and attempts <= self.settings.retry_attempts:
                    time.sleep(0.5)
                    continue
                code = "rate_limited" if status == 429 else "provider_http_error"
                raise LLMProviderError(code, f"provider returned HTTP {status}",
                                       status_code=status, request_id=request_id,
                                       failure_stage="http_response", exception_class="HTTPError",
                                       cause_code="http_status", http_response_received=True,
                                       latency_ms=_elapsed_ms(start)) from None
            except (URLError, TimeoutError, OSError) as exc:
                if attempts <= self.settings.retry_attempts:
                    time.sleep(0.25)
                    continue
                cause = exc.reason if isinstance(exc, URLError) else _nested_os_error(exc)
                cause_code = _transport_cause_code(cause if isinstance(cause, BaseException) else exc)
                raise LLMProviderError(
                    "provider_connection_error", "provider connection failed or timed out",
                    failure_stage="connection", exception_class=type(exc).__name__,
                    cause_code=cause_code, cause_class=type(cause).__name__ if isinstance(cause, BaseException) else None,
                    http_response_received=False, latency_ms=_elapsed_ms(start),
                ) from None
            if status < 200 or status >= 300:
                raise LLMProviderError("provider_http_error", f"provider returned HTTP {status}",
                                       status_code=status, request_id=request_id,
                                       failure_stage="http_response", exception_class="HTTPResponse",
                                       cause_code="http_status", http_response_received=True,
                                       latency_ms=_elapsed_ms(start))
            if len(raw) > 2_000_000:
                raise LLMProviderError("provider_response_too_large", "provider response exceeded 2 MB",
                                       status_code=status, request_id=request_id,
                                       failure_stage="response_processing", cause_code="response_too_large",
                                       http_response_received=True, latency_ms=_elapsed_ms(start))
            try:
                response_data = json.loads(raw)
                content = (_response_text(response_data) if self.settings.resolved_api_mode == "responses"
                           else response_data["choices"][0]["message"]["content"])
            except (json.JSONDecodeError, KeyError, IndexError, TypeError) as exc:
                expected = "Responses" if self.settings.resolved_api_mode == "responses" else "Chat Completions"
                raise LLMProviderError("invalid_provider_response", f"provider response did not match {expected} format",
                                       status_code=status, request_id=request_id,
                                       failure_stage="response_processing", exception_class=type(exc).__name__,
                                       cause_code="invalid_provider_response", cause_class=type(exc).__name__,
                                       http_response_received=True, latency_ms=_elapsed_ms(start)) from None
            if not isinstance(content, str) or not content.strip():
                raise LLMProviderError("empty_provider_response", "provider returned empty message content",
                                       status_code=status, request_id=request_id,
                                       failure_stage="response_processing", cause_code="empty_provider_response",
                                       http_response_received=True, latency_ms=_elapsed_ms(start))
            usage = _normalized_usage(response_data.get("usage"))
            reported_model = response_data.get("model")
            return ProviderResponse(
                content=content, provider=self.settings.provider,
                model=reported_model if isinstance(reported_model, str) else self.settings.model,
                request_id=request_id, latency_ms=max(0, round((time.monotonic() - start) * 1000)),
                usage=usage, attempts=attempts,
            )


def _request_id(headers: Any) -> str | None:
    for key in ("x-request-id", "request-id", "openai-request-id"):
        value = headers.get(key) if headers is not None else None
        if isinstance(value, str) and len(value) <= 200 and "\n" not in value:
            return value
    return None


def _elapsed_ms(start: float) -> int:
    return max(0, round((time.monotonic() - start) * 1000))


def _safe_class_name(value: object) -> str | None:
    if not isinstance(value, str) or not re.fullmatch(r"[A-Za-z][A-Za-z0-9_]{0,79}", value):
        return None
    return value


def _safe_diagnostic_code(value: object) -> str | None:
    if not isinstance(value, str) or not re.fullmatch(r"[a-z][a-z0-9_]{0,79}", value):
        return None
    return value


def _nested_os_error(exc: BaseException) -> BaseException:
    nested = exc.__cause__ or exc.__context__
    return nested if isinstance(nested, BaseException) else exc


def _transport_cause_code(exc: BaseException) -> str:
    if isinstance(exc, socket.gaierror):
        if exc.errno == getattr(socket, "EAI_AGAIN", object()):
            return "dns_temporary_failure"
        if exc.errno == getattr(socket, "EAI_NONAME", object()):
            return "dns_name_not_resolved"
        return "dns_resolution_error"
    if isinstance(exc, ssl.SSLCertVerificationError):
        return "tls_certificate_verification_failed"
    if isinstance(exc, ssl.SSLError):
        return "tls_handshake_failed"
    if isinstance(exc, (TimeoutError, socket.timeout)):
        return "connection_timeout"
    if isinstance(exc, ConnectionRefusedError):
        return "connection_refused"
    if isinstance(exc, ConnectionResetError):
        return "connection_reset"
    if isinstance(exc, BrokenPipeError):
        return "connection_broken"
    if isinstance(exc, OSError):
        code = errno.errorcode.get(exc.errno)
        return f"os_error_{code.lower()}" if code else "os_error_unknown"
    if isinstance(exc, URLError):
        return "url_error"
    return "transport_error"


def _resolve_api_mode(configured: str, endpoint: str) -> str:
    if configured != "auto":
        return configured
    path = urlsplit(endpoint).path.rstrip("/").lower()
    if path.endswith("/responses"):
        return "responses"
    return "chat_completions"


def _response_text(response_data: Mapping[str, Any]) -> str:
    output_text = response_data.get("output_text")
    if isinstance(output_text, str) and output_text.strip():
        return output_text
    output = response_data.get("output")
    chunks: list[str] = []
    if isinstance(output, list):
        for item in output:
            if not isinstance(item, dict) or item.get("type") != "message":
                continue
            content = item.get("content")
            if not isinstance(content, list):
                continue
            for part in content:
                if (isinstance(part, dict) and part.get("type") == "output_text"
                        and isinstance(part.get("text"), str)):
                    chunks.append(part["text"])
    return "".join(chunks)


def _is_loopback_http(parsed_endpoint: Any) -> bool:
    if parsed_endpoint.scheme != "http" or parsed_endpoint.hostname is None:
        return False
    host = parsed_endpoint.hostname.rstrip(".").lower()
    if host == "localhost":
        return True
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False


def _retryable(status: int) -> bool:
    return status == 429 or 500 <= status <= 599


def _normalized_usage(value: Any) -> dict[str, int | str]:
    if not isinstance(value, dict):
        return {}
    result: dict[str, int | str] = {}
    for source, target in (("input_tokens", "input_tokens"), ("prompt_tokens", "input_tokens"),
                           ("output_tokens", "output_tokens"), ("completion_tokens", "output_tokens"),
                           ("total_tokens", "total_tokens")):
        number = value.get(source)
        if isinstance(number, int) and not isinstance(number, bool) and number >= 0:
            result[target] = number
    return result


def _bounded_float(raw: str, lower: float, upper: float, label: str) -> float:
    try:
        number = float(raw)
    except ValueError as exc:
        raise LLMProviderError("invalid_settings", f"{label} must be numeric") from exc
    if number != number or not lower <= number <= upper:
        raise LLMProviderError("invalid_settings", f"{label} must be between {lower} and {upper}")
    return number


def _bounded_int(raw: str, lower: int, upper: int, label: str) -> int:
    try:
        number = int(raw)
    except ValueError as exc:
        raise LLMProviderError("invalid_settings", f"{label} must be an integer") from exc
    if not lower <= number <= upper:
        raise LLMProviderError("invalid_settings", f"{label} must be between {lower} and {upper}")
    return number
