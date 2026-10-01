"""Small, configurable OpenAI Chat Completions-compatible provider adapter.

The adapter accepts only explicit public prompt messages. It has no access to
campaign files, execution stores, or application services.
"""
from __future__ import annotations

from dataclasses import dataclass, field
import ipaddress
import json
import os
from pathlib import Path
import re
import time
from typing import Any, Mapping, Sequence
from urllib.parse import urlsplit
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen


_ENV_KEYS = frozenset({
    "ASSAYPILOT_LLM_PROVIDER", "ASSAYPILOT_LLM_ENDPOINT", "ASSAYPILOT_LLM_MODEL",
    "ASSAYPILOT_LLM_API_KEY", "ASSAYPILOT_LLM_AUTH_HEADER",
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
                 request_id: str | None = None):
        self.code = code
        self.status_code = status_code
        self.request_id = request_id
        super().__init__(message)


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
        if token_parameter not in {"max_tokens", "max_completion_tokens"}:
            raise LLMProviderError("invalid_token_parameter", "token parameter must be max_tokens or max_completion_tokens")
        timeout = _bounded_float(values.get("ASSAYPILOT_LLM_TIMEOUT_SECONDS", "60"), 1.0, 180.0, "timeout")
        tokens = _bounded_int(values.get("ASSAYPILOT_LLM_MAX_OUTPUT_TOKENS", "1200"), 128, 8192, "output token limit")
        retries = _bounded_int(values.get("ASSAYPILOT_LLM_RETRY_ATTEMPTS", "1"), 0, 1, "retry attempts")
        return cls(provider, endpoint, model, api_key, auth_header, auth_scheme,
                   response_format, token_parameter, timeout, tokens, retries)


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
    """Call a configured chat-completions endpoint with bounded retries."""

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
        body: dict[str, Any] = {
            "model": self.settings.model,
            "messages": [dict(message) for message in messages],
            "temperature": 0,
        }
        body[self.settings.token_parameter] = tokens
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
                                       status_code=status, request_id=request_id) from None
            except (URLError, TimeoutError, OSError) as exc:
                if attempts <= self.settings.retry_attempts:
                    time.sleep(0.25)
                    continue
                raise LLMProviderError("provider_connection_error", "provider connection failed or timed out") from None
            if status < 200 or status >= 300:
                raise LLMProviderError("provider_http_error", f"provider returned HTTP {status}",
                                       status_code=status, request_id=request_id)
            if len(raw) > 2_000_000:
                raise LLMProviderError("provider_response_too_large", "provider response exceeded 2 MB",
                                       status_code=status, request_id=request_id)
            try:
                response_data = json.loads(raw)
                content = response_data["choices"][0]["message"]["content"]
            except (json.JSONDecodeError, KeyError, IndexError, TypeError):
                raise LLMProviderError("invalid_provider_response", "provider response did not match chat-completions format",
                                       status_code=status, request_id=request_id) from None
            if not isinstance(content, str) or not content.strip():
                raise LLMProviderError("empty_provider_response", "provider returned empty message content",
                                       status_code=status, request_id=request_id)
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
    for source, target in (("prompt_tokens", "input_tokens"), ("completion_tokens", "output_tokens"),
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
