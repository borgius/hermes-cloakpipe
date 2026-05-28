"""CloakPipe virtual model-provider plugin for Hermes.

Hermes selects provider ``cloakpipe`` and model IDs shaped as
``cloakpipe/<provider>-<model>``.  This plugin keeps CloakPipe out of the LLM
transport path: it pseudonymizes outbound text with CloakPipe's direct privacy
API, dispatches the sanitized payload to the selected real provider/model, and
rehydrates the response before Hermes sees it.
"""

from __future__ import annotations

import copy
import hashlib
import json
import os
import shlex
import shutil
import subprocess
import threading
import time
import uuid
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib import error as urllib_error
from urllib import parse as urllib_parse
from urllib import request as urllib_request

from providers import register_provider
from providers.base import ProviderProfile

_DEFAULT_BASE_URL = "http://127.0.0.1:3100/v1"
_DEFAULT_PROVIDER_BASE_URL = "http://127.0.0.1:3199/v1"
_DEFAULT_UPSTREAM_URL = "https://api.openai.com"
_DEFAULT_OPENAI_BASE_URL = "https://api.openai.com/v1"
_DEFAULT_READY_TIMEOUT = 8.0
_DEFAULT_ACTIVE_PROVIDER_MODEL_LIMIT = 8
_DEFAULT_OPENROUTER_PROBE_TIMEOUT = 8.0
_DEFAULT_OPENROUTER_PROBE_CACHE_TTL = 3600.0
_DEFAULT_NER_BACKEND = "distilbert_pii"
_DEFAULT_NER_SIDECAR_URL = "http://127.0.0.1:9111"
_HEALTH_REQUEST_TIMEOUT = 2.0
_CARGO_INSTALL_TIMEOUT = 300.0
_NER_DOWNLOAD_TIMEOUT = 300.0
_NER_INSTALL_TIMEOUT = 300.0
_STARTUP_TIMEOUT = 15.0
_NER_STARTUP_TIMEOUT = 45.0
_POLL_INTERVAL_SECONDS = 0.25
_NER_ENABLED_PROFILES = {"general", "legal", "healthcare"}
_COMMON_UPSTREAM_PROVIDERS = (
    "openai-codex",
    "azure-foundry",
    "kimi-coding",
    "minimax-cn",
    "openrouter",
    "anthropic",
    "deepseek",
    "gemini",
    "google",
    "openai",
    "ollama",
    "local",
    "nous",
    "xai",
    "xai-oauth",
    "zai",
    "kimi",
    "minimax",
    "copilot",
    "bedrock",
    "custom",
)
_UPSTREAM_PROVIDER_ALIASES = {
    "google": "gemini",
    "local": "ollama",
}
_PROVIDER_AUTH_ENV_VARS = (
    "CLOAKPIPE_HERMES_API_KEY",
    "CLOAKPIPE_API_KEY",
    "OPENAI_API_KEY",
    "OPENROUTER_API_KEY",
    "ANTHROPIC_API_KEY",
    "GOOGLE_API_KEY",
    "GEMINI_API_KEY",
    "DEEPSEEK_API_KEY",
    "XAI_API_KEY",
    "KIMI_API_KEY",
    "MINIMAX_API_KEY",
    "ZAI_API_KEY",
    "DASHSCOPE_API_KEY",
    "NVIDIA_API_KEY",
    "OLLAMA_API_KEY",
    "AZURE_OPENAI_API_KEY",
    "AZURE_FOUNDRY_API_KEY",
)

_process_lock = threading.Lock()
_ner_process_lock = threading.Lock()
_virtual_server_lock = threading.Lock()
_managed_process: subprocess.Popen[Any] | None = None
_managed_ner_process: subprocess.Popen[Any] | None = None
_virtual_server: ThreadingHTTPServer | None = None
_virtual_server_thread: threading.Thread | None = None
_openrouter_model_usability_cache: dict[tuple[str, str], tuple[float, bool]] = {}


class CloakPipeUnavailableError(RuntimeError):
    """Raised when CloakPipe cannot be reached or started."""


class CloakPipeRequestError(RuntimeError):
    """Raised for request-time wrapper failures with an HTTP status."""

    def __init__(self, message: str, *, status: int = 500, code: str = "cloakpipe_wrapper_error") -> None:
        super().__init__(message)
        self.status = status
        self.code = code


def _read_base_url() -> str:
    override = os.environ.get("CLOAKPIPE_BASE_URL", "").strip()
    if override:
        return override.rstrip("/")
    return _DEFAULT_BASE_URL


def _read_provider_base_url() -> str:
    for env_name in ("CLOAKPIPE_HERMES_BASE_URL", "CLOAKPIPE_WRAPPER_BASE_URL"):
        override = os.environ.get(env_name, "").strip()
        if override:
            return override.rstrip("/")
    return _DEFAULT_PROVIDER_BASE_URL


def _coerce_timeout(value: Any, default: float) -> float:
    try:
        timeout = float(value)
    except (TypeError, ValueError):
        return default
    if timeout <= 0:
        return default
    return timeout


def _coerce_float(value: Any, default: float) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def _is_truthy(value: Any) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return bool(value)
    if isinstance(value, str):
        return value.strip().lower() in {"1", "true", "yes", "on", "enabled"}
    return bool(value)


def _get_value(source: Any, key: str) -> Any:
    if isinstance(source, dict):
        return source.get(key)
    return getattr(source, key, None)


def _get_nested_value(source: Any, *path: str) -> Any:
    current = source
    for key in path:
        if current is None:
            return None
        current = _get_value(current, key)
    return current


def _iter_ner_sources(*sources: Any):
    seen: set[int] = set()
    pending = [source for source in sources if source is not None]

    while pending:
        source = pending.pop(0)
        identity = id(source)
        if identity in seen:
            continue
        seen.add(identity)
        yield source

        if isinstance(source, str):
            continue

        for key in ("profile", "config", "settings", "provider", "provider_profile", "detection", "ner"):
            nested = _get_value(source, key)
            if nested is not None:
                pending.append(nested)


def _detect_ner_enabled(*sources: Any) -> bool:
    override = os.environ.get("CLOAKPIPE_NER_ENABLED", "").strip()
    if override:
        return _is_truthy(override)

    for source in _iter_ner_sources(*sources):
        for path in (
            ("detection", "ner", "enabled"),
            ("ner", "enabled"),
            ("ner_enabled",),
            ("enable_ner",),
        ):
            value = _get_nested_value(source, *path)
            if value is not None:
                return _is_truthy(value)

    for source in _iter_ner_sources(*sources):
        if isinstance(source, str) and source.strip().lower() in _NER_ENABLED_PROFILES:
            return True

        profile_name = _get_nested_value(source, "profile")
        if isinstance(profile_name, str) and profile_name.strip().lower() in _NER_ENABLED_PROFILES:
            return True

    return False


def _resolve_ner_sidecar_url(*sources: Any) -> str:
    override = os.environ.get("CLOAKPIPE_NER_SIDECAR_URL", "").strip()
    if override:
        return override.rstrip("/")

    for source in _iter_ner_sources(*sources):
        for path in (("detection", "ner", "sidecar_url"), ("ner", "sidecar_url")):
            value = _get_nested_value(source, *path)
            if isinstance(value, str) and value.strip():
                return value.rstrip("/")

    return _DEFAULT_NER_SIDECAR_URL


def _normalize_ner_backend(value: Any) -> str | None:
    if not isinstance(value, str):
        return None

    normalized = value.strip().lower().replace("-", "_")
    aliases = {
        "glinerpii": "gliner_pii",
        "distilbertpii": "distilbert_pii",
    }
    normalized = aliases.get(normalized, normalized)
    return normalized or None


def _resolve_ner_backend(*sources: Any) -> str:
    override = _normalize_ner_backend(os.environ.get("CLOAKPIPE_NER_BACKEND", "").strip())
    if override:
        return override

    for source in _iter_ner_sources(*sources):
        for path in (("detection", "ner", "backend"), ("ner", "backend")):
            resolved = _normalize_ner_backend(_get_nested_value(source, *path))
            if resolved:
                return resolved

    for source in _iter_ner_sources(*sources):
        for path in (("detection", "ner", "sidecar_url"), ("ner", "sidecar_url")):
            value = _get_nested_value(source, *path)
            if isinstance(value, str) and value.strip():
                return "gliner_pii"

    return _DEFAULT_NER_BACKEND


def _resolve_ner_model_path(*sources: Any) -> str | None:
    override = os.environ.get("CLOAKPIPE_NER_MODEL_PATH", "").strip()
    if override:
        return str(Path(override).expanduser())

    for source in _iter_ner_sources(*sources):
        for path in (("detection", "ner", "model"), ("ner", "model")):
            value = _get_nested_value(source, *path)
            if isinstance(value, str) and value.strip():
                return str(Path(value).expanduser())

    return None


def _resolve_ner_threshold(*sources: Any) -> float:
    for source in _iter_ner_sources(*sources):
        for path in (("detection", "ner", "confidence_threshold"), ("ner", "confidence_threshold")):
            value = _get_nested_value(source, *path)
            if value is not None:
                return _coerce_float(value, 0.4)

    return 0.4


def _resolve_ner_settings(*sources: Any) -> dict[str, Any]:
    return {
        "enabled": _detect_ner_enabled(*sources),
        "backend": _resolve_ner_backend(*sources),
        "sidecar_url": _resolve_ner_sidecar_url(*sources),
        "threshold": _resolve_ner_threshold(*sources),
        "model_path": _resolve_ner_model_path(*sources),
    }


def _derive_health_url(base_url: str) -> str:
    parsed = urllib_parse.urlsplit((base_url or "").rstrip("/"))
    path_segments = [segment for segment in parsed.path.split("/") if segment]

    if "v1" in path_segments:
        path_segments = path_segments[: path_segments.index("v1")]

    if path_segments:
        health_path = "/" + "/".join(path_segments + ["health"])
    else:
        health_path = "/health"

    return urllib_parse.urlunsplit(
        (
            parsed.scheme or "http",
            parsed.netloc,
            health_path,
            "",
            "",
        )
    )


def _is_local_base_url(base_url: str) -> bool:
    hostname = urllib_parse.urlsplit(base_url).hostname or ""
    return hostname in {"127.0.0.1", "localhost", "0.0.0.0", "::1"}


def _probe_health(base_url: str, *, timeout: float = _HEALTH_REQUEST_TIMEOUT) -> tuple[bool, str]:
    health_url = _derive_health_url(base_url)
    request = urllib_request.Request(health_url, method="GET")

    try:
        with urllib_request.urlopen(request, timeout=_coerce_timeout(timeout, _HEALTH_REQUEST_TIMEOUT)) as response:
            status = getattr(response, "status", response.getcode())
            response.read()
        if 200 <= status < 300:
            return True, f"responded with HTTP {status}"
        return False, f"responded with HTTP {status}"
    except urllib_error.HTTPError as exc:
        return False, f"returned HTTP {exc.code}"
    except urllib_error.URLError as exc:
        return False, f"request failed: {exc.reason}"
    except TimeoutError:
        return False, "timed out"
    except Exception as exc:  # pragma: no cover - defensive fallback
        return False, f"request failed: {exc}"


def _join_endpoint(base_url: str, endpoint: str) -> str:
    return f"{base_url.rstrip('/')}/{endpoint.lstrip('/')}"


def _read_json_response(response: Any) -> dict[str, Any]:
    raw = response.read().decode("utf-8")
    if not raw.strip():
        return {}
    data = json.loads(raw)
    if not isinstance(data, dict):
        raise CloakPipeRequestError("Expected a JSON object response from CloakPipe")
    return data


def _post_json(url: str, payload: dict[str, Any], *, timeout: float) -> dict[str, Any]:
    body = json.dumps(payload).encode("utf-8")
    request = urllib_request.Request(
        url,
        data=body,
        method="POST",
        headers={
            "Accept": "application/json",
            "Content-Type": "application/json",
        },
    )

    try:
        with urllib_request.urlopen(request, timeout=_coerce_timeout(timeout, _DEFAULT_READY_TIMEOUT)) as response:
            return _read_json_response(response)
    except urllib_error.HTTPError as exc:
        detail = ""
        try:
            detail = exc.read().decode("utf-8").strip()
        except Exception:
            detail = ""
        message = f"{url} returned HTTP {exc.code}"
        if detail:
            message = f"{message}: {_trim_output(detail)}"
        raise CloakPipeRequestError(message, status=exc.code, code="cloakpipe_http_error") from exc
    except urllib_error.URLError as exc:
        raise CloakPipeRequestError(
            f"Could not reach {url}: {exc.reason}",
            status=503,
            code="cloakpipe_unreachable",
        ) from exc
    except TimeoutError as exc:
        raise CloakPipeRequestError(
            f"Timed out calling {url}",
            status=504,
            code="cloakpipe_timeout",
        ) from exc


def _extract_text_result(data: dict[str, Any], *, endpoint: str) -> str:
    for key in ("text", "result", "output", "pseudonymized", "rehydrated"):
        value = data.get(key)
        if isinstance(value, str):
            return value
    raise CloakPipeRequestError(f"CloakPipe {endpoint} response did not include a text field")


def _cloakpipe_text_transform(base_url: str, endpoint: str, text: str, *, timeout: float) -> str:
    if not text:
        return text
    data = _post_json(_join_endpoint(base_url, endpoint), {"text": text}, timeout=timeout)
    return _extract_text_result(data, endpoint=endpoint)


def _pseudonymize_text(base_url: str, text: str, *, timeout: float) -> str:
    return _cloakpipe_text_transform(base_url, "pseudonymize", text, timeout=timeout)


def _rehydrate_text(base_url: str, text: str, *, timeout: float) -> str:
    return _cloakpipe_text_transform(base_url, "rehydrate", text, timeout=timeout)


def _transform_content_value(value: Any, transform) -> Any:
    if isinstance(value, str):
        return transform(value)

    if isinstance(value, list):
        transformed_parts: list[Any] = []
        for part in value:
            if isinstance(part, dict):
                next_part = copy.deepcopy(part)
                if isinstance(next_part.get("text"), str):
                    next_part["text"] = transform(next_part["text"])
                if isinstance(next_part.get("content"), str):
                    next_part["content"] = transform(next_part["content"])
                transformed_parts.append(next_part)
            elif isinstance(part, str):
                transformed_parts.append(transform(part))
            else:
                transformed_parts.append(part)
        return transformed_parts

    return value


def _transform_message_text(message: dict[str, Any], transform) -> dict[str, Any]:
    transformed = copy.deepcopy(message)
    if "content" in transformed:
        transformed["content"] = _transform_content_value(transformed.get("content"), transform)

    function_call = transformed.get("function_call")
    if isinstance(function_call, dict) and isinstance(function_call.get("arguments"), str):
        function_call["arguments"] = transform(function_call["arguments"])

    tool_calls = transformed.get("tool_calls")
    if isinstance(tool_calls, list):
        for tool_call in tool_calls:
            if not isinstance(tool_call, dict):
                continue
            function = tool_call.get("function")
            if isinstance(function, dict) and isinstance(function.get("arguments"), str):
                function["arguments"] = transform(function["arguments"])

    return transformed


def _pseudonymize_chat_body(body: dict[str, Any], *, base_url: str, timeout: float) -> dict[str, Any]:
    sanitized = copy.deepcopy(body)
    messages = sanitized.get("messages")
    if isinstance(messages, list):
        sanitized["messages"] = [
            _transform_message_text(message, lambda text: _pseudonymize_text(base_url, text, timeout=timeout))
            if isinstance(message, dict)
            else message
            for message in messages
        ]
    return sanitized


def _rehydrate_chat_response(payload: dict[str, Any], *, base_url: str, timeout: float) -> dict[str, Any]:
    rehydrated = copy.deepcopy(payload)
    choices = rehydrated.get("choices")
    if isinstance(choices, list):
        for choice in choices:
            if not isinstance(choice, dict):
                continue
            for key in ("message", "delta"):
                message = choice.get(key)
                if isinstance(message, dict):
                    choice[key] = _transform_message_text(
                        message,
                        lambda text: _rehydrate_text(base_url, text, timeout=timeout),
                    )
            if isinstance(choice.get("text"), str):
                choice["text"] = _rehydrate_text(base_url, choice["text"], timeout=timeout)
    return rehydrated


def _to_jsonable(value: Any) -> Any:
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    if isinstance(value, dict):
        return {str(k): _to_jsonable(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_to_jsonable(item) for item in value]
    if hasattr(value, "model_dump"):
        return _to_jsonable(value.model_dump())
    if hasattr(value, "dict"):
        return _to_jsonable(value.dict())
    if hasattr(value, "__dict__"):
        return {
            key: _to_jsonable(item)
            for key, item in vars(value).items()
            if not key.startswith("_")
        }
    return str(value)


def _canonical_upstream_provider(provider: str) -> str:
    normalized = (provider or "").strip().lower()
    return _UPSTREAM_PROVIDER_ALIASES.get(normalized, normalized)


def _resolve_upstream_client(provider: str, model: str):
    canonical_provider = _canonical_upstream_provider(provider)
    try:
        from agent.auxiliary_client import resolve_provider_client
    except Exception as exc:
        raise CloakPipeRequestError(
            f"Hermes upstream provider router is unavailable: {exc}",
            status=500,
            code="provider_router_unavailable",
        ) from exc

    if canonical_provider == "cloakpipe":
        raise CloakPipeRequestError(
            "Recursive cloakpipe provider routing is not supported",
            status=400,
            code="recursive_provider",
        )

    if canonical_provider == "openai":
        openai_base_url = os.environ.get("OPENAI_BASE_URL", "").strip() or _DEFAULT_OPENAI_BASE_URL
        openai_key = os.environ.get("OPENAI_API_KEY", "").strip() or os.environ.get("CLOAKPIPE_API_KEY", "").strip()
        client, resolved_model = resolve_provider_client(
            "custom",
            model=model,
            explicit_base_url=openai_base_url,
            explicit_api_key=openai_key,
        )
    else:
        client, resolved_model = resolve_provider_client(canonical_provider, model=model)

    if client is None:
        raise CloakPipeRequestError(
            f"No credentials or runtime are configured for upstream provider '{canonical_provider}'",
            status=401,
            code="upstream_auth_unavailable",
        )

    return client, resolved_model or model, canonical_provider


def _dispatch_chat_completion(body: dict[str, Any], *, cloakpipe_base_url: str, timeout: float) -> dict[str, Any]:
    requested_model = str(body.get("model") or "").strip()
    split_model = _split_cloakpipe_model(requested_model)
    if split_model is None:
        raise CloakPipeRequestError(
            "CloakPipe model IDs must use the form cloakpipe/<provider>-<model>",
            status=400,
            code="invalid_model",
        )

    provider, upstream_model = split_model
    sanitized = _pseudonymize_chat_body(body, base_url=cloakpipe_base_url, timeout=timeout)
    sanitized["model"] = upstream_model
    sanitized.pop("stream", None)
    sanitized.pop("stream_options", None)

    client, resolved_model, canonical_provider = _resolve_upstream_client(provider, upstream_model)
    sanitized["model"] = resolved_model or upstream_model

    try:
        response = client.chat.completions.create(**sanitized)
    except Exception as exc:
        if canonical_provider == "openrouter" and _is_openrouter_privacy_restricted_error(exc):
            raise CloakPipeRequestError(
                (
                    f"OpenRouter blocked upstream model '{resolved_model or upstream_model}' "
                    "because your account's guardrail/data policy does not permit any matching endpoints. "
                    "Choose another cloakpipe/openrouter model or update https://openrouter.ai/settings/privacy"
                ),
                status=502,
                code="openrouter_privacy_restricted",
            ) from exc
        raise CloakPipeRequestError(
            f"Upstream provider '{canonical_provider}' request failed: {exc}",
            status=502,
            code="upstream_request_failed",
        ) from exc

    payload = _to_jsonable(response)
    if not isinstance(payload, dict):
        raise CloakPipeRequestError(
            "Upstream provider returned a non-object chat completion response",
            status=502,
            code="invalid_upstream_response",
        )
    payload.setdefault("model", requested_model)
    return _rehydrate_chat_response(payload, base_url=cloakpipe_base_url, timeout=timeout)


def _ordered_unique_strings(values: list[str]) -> list[str]:
    seen: set[str] = set()
    ordered: list[str] = []
    for value in values:
        item = str(value or "").strip()
        if not item or item in seen:
            continue
        seen.add(item)
        ordered.append(item)
    return ordered


def _read_openrouter_probe_cache_ttl() -> float:
    return _coerce_timeout(os.environ.get("CLOAKPIPE_OPENROUTER_PROBE_CACHE_TTL"), _DEFAULT_OPENROUTER_PROBE_CACHE_TTL)


def _read_openrouter_probe_timeout() -> float:
    return _coerce_timeout(os.environ.get("CLOAKPIPE_OPENROUTER_PROBE_TIMEOUT"), _DEFAULT_OPENROUTER_PROBE_TIMEOUT)


def _is_openrouter_privacy_restricted_error(error: Exception | str | None) -> bool:
    text = str(error or "").lower()
    return (
        "no endpoints available matching your guardrail restrictions and data policy" in text
        or "openrouter.ai/settings/privacy" in text
    )


def _openrouter_probe_cache_key(model_id: str) -> tuple[str, str]:
    fingerprint = ""
    try:
        from hermes_cli.runtime_provider import resolve_runtime_provider

        runtime = resolve_runtime_provider(requested="openrouter", target_model=model_id)
        api_key = str(runtime.get("api_key") or "")
        if api_key:
            fingerprint = hashlib.sha256(api_key.encode("utf-8")).hexdigest()[:12]
    except Exception:
        fingerprint = ""
    return fingerprint, model_id


def _openrouter_model_is_callable(model_id: str) -> bool:
    cache_key = _openrouter_probe_cache_key(model_id)
    cached = _openrouter_model_usability_cache.get(cache_key)
    now = time.time()
    ttl = _read_openrouter_probe_cache_ttl()
    if cached is not None:
        checked_at, usable = cached
        if now - checked_at <= ttl:
            return usable

    usable = True
    try:
        from agent.auxiliary_client import resolve_provider_client

        client, resolved_model = resolve_provider_client("openrouter", model=model_id)
        client.chat.completions.create(
            model=resolved_model or model_id,
            messages=[{"role": "user", "content": "Reply with exactly: ok"}],
            max_tokens=1,
            timeout=_read_openrouter_probe_timeout(),
        )
    except Exception as exc:
        if _is_openrouter_privacy_restricted_error(exc):
            usable = False

    _openrouter_model_usability_cache[cache_key] = (now, usable)
    return usable


def _read_active_provider_model_limit() -> int:
    try:
        limit = int(os.environ.get("CLOAKPIPE_ACTIVE_PROVIDER_MODEL_LIMIT", "").strip() or _DEFAULT_ACTIVE_PROVIDER_MODEL_LIMIT)
    except ValueError:
        return _DEFAULT_ACTIVE_PROVIDER_MODEL_LIMIT
    return max(1, limit)


def _mirrored_models_from_rows(rows: list[dict[str, Any]]) -> list[str]:
    mirrored: list[str] = []
    for row in rows:
        provider = str(row.get("slug") or "").strip().lower()
        if not provider or provider == "cloakpipe":
            continue
        models = row.get("models") or ()
        for model_id in models:
            normalized_model_id = str(model_id or "").strip()
            if provider == "openrouter" and normalized_model_id and not _openrouter_model_is_callable(normalized_model_id):
                continue
            virtual_model = _virtual_model_for_provider(provider, str(model_id or "").strip())
            if virtual_model:
                mirrored.append(virtual_model)
    return _ordered_unique_strings(mirrored)


def _limited_active_rows(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    limit = _read_active_provider_model_limit()
    limited_rows: list[dict[str, Any]] = []
    for row in rows:
        if not isinstance(row, dict):
            continue
        limited_row = dict(row)
        models = [str(model_id or "").strip() for model_id in limited_row.get("models") or ()]
        limited_row["models"] = [model_id for model_id in models if model_id][:limit]
        limited_rows.append(limited_row)
    return limited_rows


def _active_provider_rows(max_models: int | None = None) -> list[dict[str, Any]]:
    resolved_max_models = max_models if max_models is not None else _read_active_provider_model_limit()

    try:
        from hermes_cli.model_switch import list_picker_providers

        rows = list_picker_providers(max_models=resolved_max_models)
        if rows:
            return [row for row in rows if isinstance(row, dict)]
    except Exception:
        pass

    try:
        from hermes_cli.model_switch import list_authenticated_providers

        rows = list_authenticated_providers(max_models=resolved_max_models)
        return [row for row in rows if isinstance(row, dict)]
    except Exception:
        return []


def _virtual_model_for_provider(provider_id: str, model_id: str) -> str:
    provider = (provider_id or "").strip().lower()
    model = (model_id or "").strip()
    if not provider or not model:
        return ""
    if model.startswith("cloakpipe/"):
        return model
    return f"cloakpipe/{provider}-{model}"


def _mirrored_active_models_from_hermes() -> list[str]:
    return _mirrored_models_from_rows(_active_provider_rows())


def _virtual_models_from_rows(
    rows: list[dict[str, Any]],
    fallback_models: tuple[str, ...] | list[str] | None = None,
) -> list[str]:
    override = os.environ.get("CLOAKPIPE_MODELS", "").strip()
    if override:
        raw_models = [part.strip() for part in override.split(",")]
        mapped = [_to_cloakpipe_model(model_id) for model_id in raw_models if model_id]
        return _ordered_unique_strings(
            [model_id for model_id in mapped if _split_cloakpipe_model(model_id) is not None]
        )

    mirrored = _mirrored_models_from_rows(rows)
    if mirrored:
        return mirrored

    raw_models = list(fallback_models or ("cloakpipe/openai-gpt-4o-mini",))
    mapped = [_to_cloakpipe_model(model_id) for model_id in raw_models if model_id]
    return _ordered_unique_strings(
        [model_id for model_id in mapped if _split_cloakpipe_model(model_id) is not None]
    )


def _configured_virtual_models(fallback_models: tuple[str, ...] | list[str] | None = None) -> list[str]:
    return _virtual_models_from_rows(_active_provider_rows(), fallback_models)


def _with_cloakpipe_picker_row(
    rows: list[dict[str, Any]],
    *,
    current_provider: str = "",
    max_models: int = 8,
) -> list[dict[str, Any]]:
    normalized_rows = [dict(row) for row in rows if isinstance(row, dict)]
    mirrored = _virtual_models_from_rows(_limited_active_rows(normalized_rows), ("cloakpipe/openai-gpt-4o-mini",))
    if not mirrored:
        return normalized_rows

    current = str(current_provider or "").strip().lower()
    top = mirrored[:max_models]
    inserted = False
    for row in normalized_rows:
        if str(row.get("slug") or "").strip().lower() != "cloakpipe":
            continue
        row["models"] = top
        row["total_models"] = len(mirrored)
        row["name"] = row.get("name") or "CloakPipe"
        row["is_current"] = bool(row.get("is_current")) or current == "cloakpipe"
        inserted = True
        break

    if not inserted:
        normalized_rows.append(
            {
                "slug": "cloakpipe",
                "name": "CloakPipe",
                "is_current": current == "cloakpipe",
                "is_user_defined": False,
                "models": top,
                "total_models": len(mirrored),
                "source": "plugin",
            }
        )

    normalized_rows.sort(key=lambda row: (not bool(row.get("is_current")), -int(row.get("total_models") or 0)))
    return normalized_rows


def _plugin_provider_def(name: str):
    requested = str(name or "").strip().lower()
    if not requested:
        return None

    profile = None
    try:
        from providers import get_provider_profile

        profile = get_provider_profile(requested)
    except Exception:
        profile = None

    if profile is None:
        try:
            from providers import list_providers

            for candidate in list_providers():
                candidate_name = str(getattr(candidate, "name", "") or "").strip().lower()
                aliases = {
                    str(alias or "").strip().lower()
                    for alias in (getattr(candidate, "aliases", ()) or ())
                    if str(alias or "").strip()
                }
                if requested and requested in {candidate_name, *aliases}:
                    profile = candidate
                    break
        except Exception:
            profile = None

    if profile is None:
        return None

    try:
        from hermes_cli.providers import ProviderDef
    except Exception:
        return None

    provider_id = str(getattr(profile, "name", "") or requested).strip().lower() or requested
    display_name = str(getattr(profile, "display_name", "") or getattr(profile, "name", "") or provider_id).strip() or provider_id
    env_vars = tuple(
        str(env_var or "").strip()
        for env_var in (getattr(profile, "env_vars", ()) or ())
        if str(env_var or "").strip()
    )

    return ProviderDef(
        id=provider_id,
        name=display_name,
        transport="openai_chat",
        api_key_env_vars=env_vars,
        base_url=str(getattr(profile, "base_url", "") or "").strip(),
        is_aggregator=False,
        auth_type=str(getattr(profile, "auth_type", "api_key") or "api_key"),
        source="plugin",
    )


def _patch_hermes_provider_resolution() -> None:
    try:
        import hermes_cli.providers as hermes_providers
    except Exception:
        return

    if getattr(hermes_providers, "_cloakpipe_provider_resolution_patched", False):
        return

    original_get_provider = getattr(hermes_providers, "get_provider", None)
    if callable(original_get_provider):

        def _patched_get_provider(name: str):
            resolved = original_get_provider(name)
            if resolved is not None:
                return resolved
            return _plugin_provider_def(name)

        hermes_providers.get_provider = _patched_get_provider

    hermes_providers._cloakpipe_provider_resolution_patched = True


def _patch_hermes_model_picker() -> None:
    try:
        import hermes_cli.model_switch as model_switch
    except Exception:
        return

    if getattr(model_switch, "_cloakpipe_picker_patched", False):
        return

    original_authenticated = getattr(model_switch, "list_authenticated_providers", None)
    if callable(original_authenticated):

        def _patched_list_authenticated_providers(
            current_provider: str = "",
            current_base_url: str = "",
            user_providers: dict | None = None,
            custom_providers: list | None = None,
            max_models: int = 8,
            current_model: str = "",
        ):
            rows = original_authenticated(
                current_provider=current_provider,
                current_base_url=current_base_url,
                user_providers=user_providers,
                custom_providers=custom_providers,
                max_models=max_models,
                current_model=current_model,
            )
            return _with_cloakpipe_picker_row(rows, current_provider=current_provider, max_models=max_models)

        model_switch.list_authenticated_providers = _patched_list_authenticated_providers

    original_picker = getattr(model_switch, "list_picker_providers", None)
    if callable(original_picker):

        def _patched_list_picker_providers(
            current_provider: str = "",
            current_base_url: str = "",
            user_providers: dict | None = None,
            custom_providers: list | None = None,
            max_models: int = 8,
            current_model: str = "",
        ):
            rows = original_picker(
                current_provider=current_provider,
                current_base_url=current_base_url,
                user_providers=user_providers,
                custom_providers=custom_providers,
                max_models=max_models,
                current_model=current_model,
            )
            return _with_cloakpipe_picker_row(rows, current_provider=current_provider, max_models=max_models)

        model_switch.list_picker_providers = _patched_list_picker_providers

    model_switch._cloakpipe_picker_patched = True


def _patch_hermes_model_switch() -> None:
    try:
        import hermes_cli.model_switch as model_switch
    except Exception:
        return

    if getattr(model_switch, "_cloakpipe_switch_patched", False):
        return

    original_switch_model = getattr(model_switch, "switch_model", None)
    if callable(original_switch_model):

        def _patched_switch_model(
            raw_input: str,
            current_provider: str,
            current_model: str,
            current_base_url: str = "",
            current_api_key: str = "",
            is_global: bool = False,
            explicit_provider: str = "",
            user_providers: dict | None = None,
            custom_providers: list | None = None,
        ):
            requested_model = str(raw_input or "").strip()
            target_explicit_provider = explicit_provider

            if requested_model.startswith("cloakpipe/") and not target_explicit_provider:
                target_explicit_provider = "cloakpipe"
            elif target_explicit_provider == "cloakpipe" and requested_model and not requested_model.startswith("cloakpipe/"):
                prefixed_model = f"cloakpipe/{requested_model.lstrip('/')}"
                requested_model = (
                    prefixed_model
                    if _split_cloakpipe_model(prefixed_model) is not None
                    else _to_cloakpipe_model(requested_model)
                )

            return original_switch_model(
                raw_input=requested_model,
                current_provider=current_provider,
                current_model=current_model,
                current_base_url=current_base_url,
                current_api_key=current_api_key,
                is_global=is_global,
                explicit_provider=target_explicit_provider,
                user_providers=user_providers,
                custom_providers=custom_providers,
            )

        model_switch.switch_model = _patched_switch_model

    model_switch._cloakpipe_switch_patched = True


def _stream_chunk_from_choice(choice: dict[str, Any]) -> dict[str, Any]:
    message = choice.get("message") if isinstance(choice.get("message"), dict) else {}
    delta: dict[str, Any] = {"role": message.get("role") or "assistant"}
    for key in ("content", "tool_calls", "function_call", "reasoning", "reasoning_content"):
        if key in message and message[key] is not None:
            delta[key] = message[key]
    return {
        "index": int(choice.get("index") or 0),
        "delta": delta,
        "finish_reason": None,
    }


def _finish_chunk_from_choice(choice: dict[str, Any]) -> dict[str, Any]:
    return {
        "index": int(choice.get("index") or 0),
        "delta": {},
        "finish_reason": choice.get("finish_reason") or "stop",
    }


def _chat_completion_to_sse(payload: dict[str, Any]) -> bytes:
    choices = [choice for choice in payload.get("choices", []) if isinstance(choice, dict)]
    now = int(time.time())
    completion_id = str(payload.get("id") or f"chatcmpl-cloakpipe-{uuid.uuid4().hex}")
    model = str(payload.get("model") or "cloakpipe")
    first = {
        "id": completion_id,
        "object": "chat.completion.chunk",
        "created": now,
        "model": model,
        "choices": [_stream_chunk_from_choice(choice) for choice in choices],
    }
    second = {
        "id": completion_id,
        "object": "chat.completion.chunk",
        "created": now,
        "model": model,
        "choices": [_finish_chunk_from_choice(choice) for choice in choices],
    }
    if isinstance(payload.get("usage"), dict):
        second["usage"] = payload["usage"]
    lines = [
        f"data: {json.dumps(first)}\n\n",
        f"data: {json.dumps(second)}\n\n",
        "data: [DONE]\n\n",
    ]
    return "".join(lines).encode("utf-8")


class _CloakPipeVirtualServer(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True

    def __init__(self, server_address: tuple[str, int], handler_class, *, base_url: str, cloakpipe_base_url: str):
        super().__init__(server_address, handler_class)
        self.base_url = base_url.rstrip("/")
        self.cloakpipe_base_url = cloakpipe_base_url.rstrip("/")


class _CloakPipeVirtualHandler(BaseHTTPRequestHandler):
    server: _CloakPipeVirtualServer

    def log_message(self, format: str, *args: Any) -> None:  # noqa: A002 - stdlib signature
        if os.environ.get("CLOAKPIPE_WRAPPER_DEBUG", "").strip().lower() in {"1", "true", "yes", "on"}:
            super().log_message(format, *args)

    def _path(self) -> str:
        return urllib_parse.urlsplit(self.path).path.rstrip("/") or "/"

    def _send_json(self, status: int, payload: dict[str, Any]) -> None:
        data = json.dumps(payload).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def _send_sse(self, payload: dict[str, Any]) -> None:
        data = _chat_completion_to_sse(payload)
        self.send_response(HTTPStatus.OK)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Cache-Control", "no-cache")
        self.send_header("Connection", "close")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def _send_error(self, exc: Exception) -> None:
        status = getattr(exc, "status", 500)
        code = getattr(exc, "code", "cloakpipe_wrapper_error")
        self._send_json(
            int(status),
            {
                "error": {
                    "message": str(exc),
                    "type": "cloakpipe_wrapper_error",
                    "code": code,
                }
            },
        )

    def _read_body(self) -> dict[str, Any]:
        length = int(self.headers.get("Content-Length") or "0")
        raw = self.rfile.read(length) if length else b"{}"
        data = json.loads(raw.decode("utf-8") or "{}")
        if not isinstance(data, dict):
            raise CloakPipeRequestError("Expected a JSON object request body", status=400, code="invalid_request")
        return data

    def do_GET(self) -> None:  # noqa: N802 - stdlib hook
        path = self._path()
        if path in {"/health", "/v1/health"}:
            self._send_json(HTTPStatus.OK, {"status": "ok"})
            return
        if path in {"/models", "/v1/models"}:
            models = _configured_virtual_models(("cloakpipe/openai-gpt-4o-mini",))
            self._send_json(
                HTTPStatus.OK,
                {
                    "object": "list",
                    "data": [{"id": model_id, "object": "model", "owned_by": "cloakpipe"} for model_id in models],
                },
            )
            return
        self._send_json(HTTPStatus.NOT_FOUND, {"error": {"message": "not found"}})

    def do_POST(self) -> None:  # noqa: N802 - stdlib hook
        path = self._path()
        if path not in {"/chat/completions", "/v1/chat/completions"}:
            self._send_json(HTTPStatus.NOT_FOUND, {"error": {"message": "not found"}})
            return

        try:
            body = self._read_body()
            wants_stream = _is_truthy(body.get("stream"))
            payload = _dispatch_chat_completion(
                body,
                cloakpipe_base_url=self.server.cloakpipe_base_url,
                timeout=_coerce_timeout(os.environ.get("CLOAKPIPE_REQUEST_TIMEOUT"), _DEFAULT_READY_TIMEOUT),
            )
            if wants_stream:
                self._send_sse(payload)
            else:
                self._send_json(HTTPStatus.OK, payload)
        except Exception as exc:
            self._send_error(exc)


def _to_listen_address(base_url: str) -> str:
    parsed = urllib_parse.urlsplit(base_url)
    host = parsed.hostname or "127.0.0.1"
    port = parsed.port or (443 if parsed.scheme == "https" else 80)
    if ":" in host and not host.startswith("["):
        host = f"[{host}]"
    return f"{host}:{port}"


def _parse_sidecar_binding(sidecar_url: str) -> tuple[str, int]:
    parsed = urllib_parse.urlsplit(sidecar_url)
    host = parsed.hostname or "127.0.0.1"
    port = parsed.port or 9111
    return host, port


def _parse_http_binding(base_url: str) -> tuple[str, int]:
    parsed = urllib_parse.urlsplit(base_url)
    host = parsed.hostname or "127.0.0.1"
    port = parsed.port or (443 if parsed.scheme == "https" else 80)
    return host, port


def _start_virtual_provider_server(provider_base_url: str, cloakpipe_base_url: str) -> None:
    global _virtual_server, _virtual_server_thread

    with _virtual_server_lock:
        if _virtual_server is not None and _virtual_server_thread is not None and _virtual_server_thread.is_alive():
            if getattr(_virtual_server, "base_url", "") == provider_base_url.rstrip("/"):
                _virtual_server.cloakpipe_base_url = cloakpipe_base_url.rstrip("/")
                return
            _virtual_server.shutdown()
            _virtual_server.server_close()
            _virtual_server = None
            _virtual_server_thread = None

        host, port = _parse_http_binding(provider_base_url)
        server = _CloakPipeVirtualServer(
            (host, port),
            _CloakPipeVirtualHandler,
            base_url=provider_base_url,
            cloakpipe_base_url=cloakpipe_base_url,
        )
        thread = threading.Thread(target=server.serve_forever, name="cloakpipe-hermes-wrapper", daemon=True)
        thread.start()
        _virtual_server = server
        _virtual_server_thread = thread


def _wait_for_virtual_provider(base_url: str, *, timeout: float) -> tuple[bool, str]:
    deadline = time.monotonic() + _coerce_timeout(timeout, _STARTUP_TIMEOUT)
    last_detail = "health check did not succeed"

    while time.monotonic() < deadline:
        healthy, detail = _probe_health(base_url, timeout=_HEALTH_REQUEST_TIMEOUT)
        if healthy:
            return True, detail
        last_detail = detail
        time.sleep(_POLL_INTERVAL_SECONDS)

    return False, last_detail


def _ensure_virtual_provider_ready(provider_base_url: str, cloakpipe_base_url: str, *, timeout: float) -> None:
    healthy, health_detail = _probe_health(provider_base_url, timeout=min(_coerce_timeout(timeout, _DEFAULT_READY_TIMEOUT), _HEALTH_REQUEST_TIMEOUT))
    if healthy:
        return

    if not _is_local_base_url(provider_base_url):
        raise CloakPipeUnavailableError(
            f"CloakPipe Hermes wrapper is not ready at {provider_base_url}. "
            f"Health check {_derive_health_url(provider_base_url)}: {health_detail}. "
            "Automatic wrapper startup is only supported for localhost/loopback URLs."
        )

    try:
        _start_virtual_provider_server(provider_base_url, cloakpipe_base_url)
    except OSError as exc:
        raise CloakPipeUnavailableError(f"Could not start CloakPipe Hermes wrapper at {provider_base_url}: {exc}") from exc

    healthy, health_detail = _wait_for_virtual_provider(
        provider_base_url,
        timeout=max(_coerce_timeout(timeout, _DEFAULT_READY_TIMEOUT), _STARTUP_TIMEOUT),
    )
    if not healthy:
        raise CloakPipeUnavailableError(
            f"CloakPipe Hermes wrapper started at {provider_base_url}, but health check "
            f"{_derive_health_url(provider_base_url)} still failed: {health_detail}"
        )


def _is_executable(path: Path) -> bool:
    return path.exists() and path.is_file() and os.access(path, os.X_OK)


def _find_named_binary(name: str) -> Path | None:
    discovered = shutil.which(name)
    if discovered:
        return Path(discovered)

    cargo_home = os.environ.get("CARGO_HOME", "").strip()
    if cargo_home:
        cargo_candidate = Path(cargo_home).expanduser() / "bin" / name
        if _is_executable(cargo_candidate):
            return cargo_candidate

    cargo_candidate = Path.home() / ".cargo" / "bin" / name
    if _is_executable(cargo_candidate):
        return cargo_candidate

    return None


def _find_cloakpipe_binary() -> Path | None:
    return _find_named_binary("cloakpipe")


def _find_cargo_binary() -> Path | None:
    return _find_named_binary("cargo")


def _find_cloakpipe_source_dir(binary_path: Path | None = None) -> Path | None:
    override = os.environ.get("CLOAKPIPE_SOURCE_DIR", "").strip()
    candidates: list[Path] = []

    def _append_candidate(path: Path) -> None:
        expanded = path.expanduser()
        if expanded not in candidates:
            candidates.append(expanded)

    if override:
        _append_candidate(Path(override))

    current_dir = Path.cwd()
    for candidate in [current_dir, *current_dir.parents]:
        _append_candidate(candidate)
        _append_candidate(candidate / "cloakpipe")
        if candidate.parent != candidate:
            _append_candidate(candidate.parent / "cloakpipe")

    if binary_path is not None:
        for candidate in [binary_path.parent, *binary_path.parent.parents]:
            _append_candidate(candidate)
            _append_candidate(candidate / "cloakpipe")
            if candidate.parent != candidate:
                _append_candidate(candidate.parent / "cloakpipe")

    for candidate in candidates:
        if (candidate / "tools" / "gliner-pii-server.py").exists() or (candidate / "tools" / "download_model.sh").exists():
            return candidate

    return None


def _distilbert_ner_model_path(source_dir: Path) -> Path:
    return source_dir / "models" / "distilbert-pii" / "quantized" / "model_quantized.onnx"


def _known_upstream_providers() -> list[str]:
    providers = {provider.strip().lower() for provider in _COMMON_UPSTREAM_PROVIDERS if provider.strip()}
    try:
        from hermes_cli.auth import PROVIDER_REGISTRY

        providers.update(str(provider).strip().lower() for provider in PROVIDER_REGISTRY if str(provider).strip())
    except Exception:
        pass
    try:
        from providers import list_providers

        for profile in list_providers():
            name = str(getattr(profile, "name", "") or "").strip().lower()
            if name and name != "cloakpipe":
                providers.add(name)
            for alias in getattr(profile, "aliases", ()) or ():
                alias_name = str(alias or "").strip().lower()
                if alias_name and alias_name != "cloakpipe":
                    providers.add(alias_name)
    except Exception:
        pass
    try:
        for row in _active_provider_rows(max_models=1):
            slug = str(row.get("slug") or "").strip().lower()
            if slug and slug != "cloakpipe":
                providers.add(slug)
    except Exception:
        pass
    return sorted(providers, key=len, reverse=True)


def _split_cloakpipe_model(model_id: str | None) -> tuple[str, str] | None:
    raw = (model_id or "").strip()
    if not raw.startswith("cloakpipe/"):
        return None

    remainder = raw[len("cloakpipe/") :]
    remainder_lower = remainder.lower()
    for provider in _known_upstream_providers():
        prefix = f"{provider}-"
        if remainder_lower.startswith(prefix):
            model = remainder[len(prefix) :]
            if model:
                return provider, model

    if "-" not in remainder:
        return None

    provider, model = remainder.split("-", 1)
    if not provider or not model:
        return None

    return provider, model


def _requested_provider(model_id: str | None) -> str:
    split_model = _split_cloakpipe_model(model_id)
    if split_model is not None:
        provider, _ = split_model
        return provider.strip().lower()

    raw = (model_id or "").strip()
    if "/" not in raw:
        return ""
    provider, _ = raw.split("/", 1)
    return provider.strip().lower()


def _select_upstream_url(model_id: str | None) -> str:
    override = os.environ.get("CLOAKPIPE_UPSTREAM_URL", "").strip()
    if override:
        return override.rstrip("/")

    provider = _requested_provider(model_id)
    if provider == "anthropic":
        return "https://api.anthropic.com"
    if provider in {"ollama", "local"}:
        return "http://127.0.0.1:11434"
    return _DEFAULT_UPSTREAM_URL


def _select_api_key_env(model_id: str | None) -> str:
    override = os.environ.get("CLOAKPIPE_UPSTREAM_API_KEY_ENV", "").strip()
    if override:
        return override

    provider = _requested_provider(model_id)
    candidates_by_provider = {
        "anthropic": ("ANTHROPIC_API_KEY", "CLOAKPIPE_API_KEY"),
        "azure": ("AZURE_OPENAI_API_KEY", "CLOAKPIPE_API_KEY"),
        "ollama": ("OLLAMA_API_KEY", "CLOAKPIPE_API_KEY"),
        "openai": ("CLOAKPIPE_API_KEY", "OPENAI_API_KEY"),
    }
    candidates = candidates_by_provider.get(provider, ("CLOAKPIPE_API_KEY", "OPENAI_API_KEY"))

    for candidate in candidates:
        if os.environ.get(candidate):
            return candidate
    return candidates[0]


def _managed_runtime_dir() -> Path:
    override = os.environ.get("CLOAKPIPE_MANAGED_DIR", "").strip()
    if override:
        return Path(override).expanduser()
    return Path.home() / ".hermes-cloakpipe"


def _ner_install_marker_path() -> Path:
    return _managed_runtime_dir() / "ner-installed"


def _toml_string(value: str) -> str:
    escaped = value.replace("\\", "\\\\").replace('"', '\\"')
    return f'"{escaped}"'


def _render_managed_config(base_url: str, model_id: str | None, ner_settings: dict[str, Any] | None = None) -> str:
    runtime_dir = _managed_runtime_dir()
    vault_path = runtime_dir / "vault.enc"

    lines = [
        "[proxy]",
        f"listen = {_toml_string(_to_listen_address(base_url))}",
        f"upstream = {_toml_string(_select_upstream_url(model_id))}",
        f"api_key_env = {_toml_string(_select_api_key_env(model_id))}",
        "",
        "[vault]",
        f"path = {_toml_string(str(vault_path))}",
        'key_env = "CLOAKPIPE_VAULT_KEY"',
        "",
        "[detection]",
        "secrets = true",
        "financial = true",
        "dates = true",
        "emails = true",
        "phone_numbers = false",
        "ip_addresses = false",
        "urls_internal = false",
    ]

    if ner_settings and ner_settings.get("enabled"):
        backend = str(ner_settings.get("backend") or _DEFAULT_NER_BACKEND)
        lines.extend(
            [
                "",
                "[detection.ner]",
                "enabled = true",
                f"backend = {_toml_string(backend)}",
                f"confidence_threshold = {ner_settings['threshold']}",
            ]
        )

        model_path = ner_settings.get("model_path")
        if isinstance(model_path, str) and model_path.strip():
            lines.append(f"model = {_toml_string(model_path)}")

        if backend == "gliner_pii":
            lines.append(f"sidecar_url = {_toml_string(ner_settings['sidecar_url'])}")

    lines.append("")
    return "\n".join(lines)


def _write_managed_config(base_url: str, model_id: str | None, ner_settings: dict[str, Any] | None = None) -> Path:
    runtime_dir = _managed_runtime_dir()
    runtime_dir.mkdir(parents=True, exist_ok=True)

    config_path = runtime_dir / "cloakpipe.toml"
    config_body = _render_managed_config(base_url, model_id, ner_settings)
    if not config_path.exists() or config_path.read_text(encoding="utf-8") != config_body:
        config_path.write_text(config_body, encoding="utf-8")

    return config_path


def _trim_output(output: str, *, limit: int = 240) -> str:
    text = (output or "").strip()
    if len(text) <= limit:
        return text
    return f"{text[: limit - 3]}..."


def _install_cloakpipe_with_cargo(cargo_path: Path, *, timeout: float = _CARGO_INSTALL_TIMEOUT) -> Path:
    try:
        result = subprocess.run(
            [str(cargo_path), "install", "cloakpipe-cli"],
            capture_output=True,
            text=True,
            check=False,
            timeout=_coerce_timeout(timeout, _CARGO_INSTALL_TIMEOUT),
        )
    except subprocess.TimeoutExpired as exc:
        raise RuntimeError("Timed out while running cargo install cloakpipe-cli") from exc
    except OSError as exc:
        raise RuntimeError(f"Could not run Cargo at {cargo_path}: {exc}") from exc

    if result.returncode != 0:
        details = _trim_output(result.stderr or result.stdout)
        raise RuntimeError(f"cargo install cloakpipe-cli failed: {details or f'exit code {result.returncode}'}")

    binary_path = _find_cloakpipe_binary()
    if binary_path is None:
        raise RuntimeError("cargo install cloakpipe-cli completed, but the cloakpipe binary was still not found")

    return binary_path


def _install_ner_with_cloakpipe(binary_path: Path, *, timeout: float = _NER_INSTALL_TIMEOUT) -> None:
    try:
        result = subprocess.run(
            [str(binary_path), "ner", "install"],
            capture_output=True,
            text=True,
            check=False,
            timeout=_coerce_timeout(timeout, _NER_INSTALL_TIMEOUT),
        )
    except subprocess.TimeoutExpired as exc:
        raise RuntimeError("Timed out while running cloakpipe ner install") from exc
    except OSError as exc:
        raise RuntimeError(f"Could not run {binary_path} ner install: {exc}") from exc

    if result.returncode != 0:
        details = _trim_output(result.stderr or result.stdout)
        raise RuntimeError(f"cloakpipe ner install failed: {details or f'exit code {result.returncode}'}")

    marker_path = _ner_install_marker_path()
    marker_path.parent.mkdir(parents=True, exist_ok=True)
    marker_path.write_text(str(binary_path), encoding="utf-8")


def _download_ner_model_with_cloakpipe(
    binary_path: Path,
    source_dir: Path,
    *,
    timeout: float = _NER_DOWNLOAD_TIMEOUT,
) -> Path:
    try:
        result = subprocess.run(
            [str(binary_path), "ner", "download"],
            cwd=str(source_dir),
            capture_output=True,
            text=True,
            check=False,
            timeout=_coerce_timeout(timeout, _NER_DOWNLOAD_TIMEOUT),
        )
    except subprocess.TimeoutExpired as exc:
        raise RuntimeError("Timed out while running cloakpipe ner download") from exc
    except OSError as exc:
        raise RuntimeError(f"Could not run {binary_path} ner download: {exc}") from exc

    if result.returncode != 0:
        details = _trim_output(result.stderr or result.stdout)
        raise RuntimeError(f"cloakpipe ner download failed: {details or f'exit code {result.returncode}'}")

    model_path = _distilbert_ner_model_path(source_dir)
    if not model_path.is_file():
        raise RuntimeError(
            "cloakpipe ner download completed, but the DistilBERT model was not found at "
            f"{model_path}"
        )

    return model_path


def _start_local_cloakpipe(binary_path: Path, config_path: Path) -> tuple[bool, Path]:
    global _managed_process

    log_path = config_path.parent / "cloakpipe.log"
    with _process_lock:
        if _managed_process is not None and _managed_process.poll() is None:
            return False, log_path

        _managed_process = None
        with log_path.open("a", encoding="utf-8") as log_file:
            _managed_process = subprocess.Popen(
                [str(binary_path), "--config", str(config_path), "start"],
                cwd=str(config_path.parent),
                stdin=subprocess.DEVNULL,
                stdout=log_file,
                stderr=subprocess.STDOUT,
                close_fds=True,
            )

    return True, log_path


def _start_local_ner(binary_path: Path, sidecar_url: str, *, threshold: float) -> tuple[bool, Path]:
    global _managed_ner_process

    source_dir = _find_cloakpipe_source_dir(binary_path)
    if source_dir is None:
        raise RuntimeError(
            "Could not find a CloakPipe source checkout with tools/gliner-pii-server.py. "
            "Set CLOAKPIPE_SOURCE_DIR to your CloakPipe checkout before using automatic NER startup."
        )

    runtime_dir = _managed_runtime_dir()
    runtime_dir.mkdir(parents=True, exist_ok=True)
    log_path = runtime_dir / "cloakpipe-ner.log"
    host, port = _parse_sidecar_binding(sidecar_url)

    with _ner_process_lock:
        if _managed_ner_process is not None and _managed_ner_process.poll() is None:
            return False, log_path

        _managed_ner_process = None
        with log_path.open("a", encoding="utf-8") as log_file:
            _managed_ner_process = subprocess.Popen(
                [
                    str(binary_path),
                    "ner",
                    "start",
                    "--host",
                    host,
                    "--port",
                    str(port),
                    "--threshold",
                    str(threshold),
                ],
                cwd=str(source_dir),
                stdin=subprocess.DEVNULL,
                stdout=log_file,
                stderr=subprocess.STDOUT,
                close_fds=True,
            )

    return True, log_path


def _wait_for_health(base_url: str, *, timeout: float) -> tuple[bool, str]:
    global _managed_process

    deadline = time.monotonic() + _coerce_timeout(timeout, _STARTUP_TIMEOUT)
    last_detail = "health check did not succeed"

    while time.monotonic() < deadline:
        healthy, detail = _probe_health(base_url, timeout=_HEALTH_REQUEST_TIMEOUT)
        if healthy:
            return True, detail
        last_detail = detail

        with _process_lock:
            process = _managed_process
            if process is not None and process.poll() is not None:
                exit_code = process.returncode
                _managed_process = None
                return False, f"managed cloakpipe process exited with code {exit_code}; last health check {detail}"

        time.sleep(_POLL_INTERVAL_SECONDS)

    return False, last_detail


def _wait_for_ner_health(sidecar_url: str, *, timeout: float) -> tuple[bool, str]:
    global _managed_ner_process

    deadline = time.monotonic() + _coerce_timeout(timeout, _NER_STARTUP_TIMEOUT)
    last_detail = "NER health check did not succeed"

    while time.monotonic() < deadline:
        healthy, detail = _probe_health(sidecar_url, timeout=_HEALTH_REQUEST_TIMEOUT)
        if healthy:
            return True, detail
        last_detail = detail

        with _ner_process_lock:
            process = _managed_ner_process
            if process is not None and process.poll() is not None:
                exit_code = process.returncode
                _managed_ner_process = None
                return False, f"managed NER process exited with code {exit_code}; last health check {detail}"

        time.sleep(_POLL_INTERVAL_SECONDS)

    return False, last_detail


def _format_unavailable_message(
    *,
    base_url: str,
    model_id: str | None,
    attempts: list[str],
    health_detail: str,
    config_path: Path | None,
    log_path: Path | None,
) -> str:
    quoted_config = shlex.quote(str(config_path)) if config_path is not None else "cloakpipe.toml"
    health_url = _derive_health_url(base_url)

    lines = [
        f"CloakPipe is not ready at {base_url}.",
        f"Health check {health_url}: {health_detail}",
    ]

    if model_id:
        lines.append(f"Requested model: {model_id}")

    if attempts:
        lines.extend(["", "What the plugin tried:"])
        lines.extend(f"- {attempt}" for attempt in attempts)

    lines.extend(
        [
            "",
            "Manual next steps:",
            "- Install the verified CLI package with: cargo install cloakpipe-cli",
            f"- Start the proxy with: cloakpipe --config {quoted_config} start",
            "- Or, if Docker Desktop is already installed and running, use: docker run -p 3100:3100 ghcr.io/cloakpipe/cloakpipe:latest",
            "",
            "Automation notes:",
            "- The plugin does not use https://app.cloakpipe.co/install.sh because it currently resolves to a sign-in page.",
            "- The plugin does not attempt to install Docker Desktop on macOS because that flow can require an app install, license acceptance, and privileged configuration.",
        ]
    )

    if not _is_local_base_url(base_url):
        lines.insert(3, "Configured CLOAKPIPE_BASE_URL points at a non-local host, so automatic local install/start was skipped.")

    if log_path is not None:
        lines.append(f"- Managed startup logs: {log_path}")

    return "\n".join(lines)


def _format_ner_unavailable_message(
    *,
    sidecar_url: str,
    attempts: list[str],
    health_detail: str,
    log_path: Path | None,
) -> str:
    quoted_source = shlex.quote(os.environ.get("CLOAKPIPE_SOURCE_DIR", "/path/to/cloakpipe"))
    lines = [
        f"CloakPipe NER sidecar is not ready at {sidecar_url}.",
        f"Health check {_derive_health_url(sidecar_url)}: {health_detail}",
    ]

    if attempts:
        lines.extend(["", "What the plugin tried:"])
        lines.extend(f"- {attempt}" for attempt in attempts)

    lines.extend(
        [
            "",
            "Manual next steps:",
            "- Install NER dependencies with: cloakpipe ner install",
            f"- If CloakPipe is not running from its source checkout, set CLOAKPIPE_SOURCE_DIR={quoted_source}",
            "- Start the sidecar with: cloakpipe ner start",
        ]
    )

    if log_path is not None:
        lines.append(f"- Managed NER startup logs: {log_path}")

    return "\n".join(lines)


def _format_distilbert_ner_unavailable_message(
    *,
    attempts: list[str],
    model_path: Path | None,
) -> str:
    quoted_source = shlex.quote(os.environ.get("CLOAKPIPE_SOURCE_DIR", "/path/to/cloakpipe"))
    quoted_model_path = shlex.quote(
        str(model_path or Path("/path/to/cloakpipe/models/distilbert-pii/quantized/model_quantized.onnx"))
    )

    lines = ["CloakPipe built-in NER model is not ready."]

    if model_path is not None:
        lines.append(f"Expected model path: {model_path}")

    if attempts:
        lines.extend(["", "What the plugin tried:"])
        lines.extend(f"- {attempt}" for attempt in attempts)

    lines.extend(
        [
            "",
            "Manual next steps:",
            "- Download the built-in NER model with: cloakpipe ner download",
            f"- If needed, point CLOAKPIPE_SOURCE_DIR at your CloakPipe checkout: {quoted_source}",
            f"- Or point directly at the downloaded ONNX model with CLOAKPIPE_NER_MODEL_PATH={quoted_model_path}",
            "- No separate NER sidecar start is required for the built-in DistilBERT backend.",
        ]
    )

    return "\n".join(lines)


def _resolve_cloakpipe_binary(base_url: str, attempts: list[str]) -> Path:
    binary_path = _find_cloakpipe_binary()
    if binary_path is not None:
        attempts.append(f"Found cloakpipe executable at {binary_path}")
        return binary_path

    attempts.append("No cloakpipe executable was found on PATH or in ~/.cargo/bin")
    cargo_path = _find_cargo_binary()
    if cargo_path is None:
        attempts.append("Cargo was not available, so automatic CLI installation was skipped")
        raise CloakPipeUnavailableError(
            _format_unavailable_message(
                base_url=base_url,
                model_id=None,
                attempts=attempts,
                health_detail="cloakpipe executable missing",
                config_path=None,
                log_path=None,
            )
        )

    attempts.append(f"Found Cargo at {cargo_path}; trying cargo install cloakpipe-cli")
    try:
        binary_path = _install_cloakpipe_with_cargo(cargo_path)
    except Exception as exc:
        attempts.append(str(exc))
        raise CloakPipeUnavailableError(
            _format_unavailable_message(
                base_url=base_url,
                model_id=None,
                attempts=attempts,
                health_detail="cloakpipe executable missing",
                config_path=None,
                log_path=None,
            )
        ) from exc

    attempts.append(f"Installed cloakpipe and found the binary at {binary_path}")
    return binary_path


def _ensure_ner_ready(binary_path: Path, ner_settings: dict[str, Any], *, timeout: float) -> None:
    backend = _normalize_ner_backend(ner_settings.get("backend")) or _DEFAULT_NER_BACKEND
    ner_settings["backend"] = backend

    if backend != "gliner_pii":
        attempts: list[str] = []
        configured_model_path = ner_settings.get("model_path")
        resolved_model_path: Path | None = None

        if isinstance(configured_model_path, str) and configured_model_path.strip():
            resolved_model_path = Path(configured_model_path).expanduser()
            ner_settings["model_path"] = str(resolved_model_path)
            if resolved_model_path.is_file():
                return
            attempts.append(f"Configured NER model path {resolved_model_path} was not present")

        source_dir = _find_cloakpipe_source_dir(binary_path)
        if source_dir is None:
            attempts.append(
                "Could not find a CloakPipe source checkout with tools/download_model.sh. "
                "Set CLOAKPIPE_SOURCE_DIR or CLOAKPIPE_NER_MODEL_PATH before using automatic built-in NER setup."
            )
            raise CloakPipeUnavailableError(
                _format_distilbert_ner_unavailable_message(
                    attempts=attempts,
                    model_path=resolved_model_path,
                )
            )

        expected_model_path = _distilbert_ner_model_path(source_dir)
        if expected_model_path.is_file():
            ner_settings["model_path"] = str(expected_model_path)
            return

        attempts.append(
            f"Built-in DistilBERT model was missing at {expected_model_path}; trying cloakpipe ner download"
        )
        try:
            downloaded_model_path = _download_ner_model_with_cloakpipe(binary_path, source_dir)
        except Exception as exc:
            attempts.append(str(exc))
            raise CloakPipeUnavailableError(
                _format_distilbert_ner_unavailable_message(
                    attempts=attempts,
                    model_path=expected_model_path,
                )
            ) from exc

        ner_settings["model_path"] = str(downloaded_model_path)
        return

    sidecar_url = ner_settings["sidecar_url"]
    probe_timeout = min(_coerce_timeout(timeout, _DEFAULT_READY_TIMEOUT), _HEALTH_REQUEST_TIMEOUT)
    healthy, health_detail = _probe_health(sidecar_url, timeout=probe_timeout)
    if healthy:
        return

    attempts = [f"Probed {_derive_health_url(sidecar_url)} and {health_detail}"]
    marker_path = _ner_install_marker_path()
    log_path: Path | None = None

    if not marker_path.exists():
        attempts.append("Managed NER install marker was missing; trying cloakpipe ner install")
        try:
            _install_ner_with_cloakpipe(binary_path)
        except Exception as exc:
            attempts.append(str(exc))
            raise CloakPipeUnavailableError(
                _format_ner_unavailable_message(
                    sidecar_url=sidecar_url,
                    attempts=attempts,
                    health_detail=health_detail,
                    log_path=None,
                )
            ) from exc
        attempts.append("Installed NER dependencies with cloakpipe ner install")
    else:
        attempts.append(f"Managed NER install marker already exists at {marker_path}")

    try:
        started, log_path = _start_local_ner(binary_path, sidecar_url, threshold=ner_settings["threshold"])
    except Exception as exc:
        attempts.append(str(exc))
        raise CloakPipeUnavailableError(
            _format_ner_unavailable_message(
                sidecar_url=sidecar_url,
                attempts=attempts,
                health_detail=health_detail,
                log_path=None,
            )
        ) from exc

    if started:
        host, port = _parse_sidecar_binding(sidecar_url)
        attempts.append(f"Started cloakpipe ner start --host {host} --port {port} --threshold {ner_settings['threshold']}")
    else:
        attempts.append("Reused the existing managed NER process")

    healthy, health_detail = _wait_for_ner_health(
        sidecar_url,
        timeout=max(_coerce_timeout(timeout, _DEFAULT_READY_TIMEOUT), _NER_STARTUP_TIMEOUT),
    )
    if healthy:
        return

    attempts.append(f"Local NER startup finished, but {_derive_health_url(sidecar_url)} still failed: {health_detail}")
    raise CloakPipeUnavailableError(
        _format_ner_unavailable_message(
            sidecar_url=sidecar_url,
            attempts=attempts,
            health_detail=health_detail,
            log_path=log_path,
        )
    )


def _ensure_cloakpipe_ready(
    base_url: str,
    *,
    timeout: float = _DEFAULT_READY_TIMEOUT,
    requested_model: str | None = None,
    ner_settings: dict[str, Any] | None = None,
) -> None:
    probe_timeout = min(_coerce_timeout(timeout, _DEFAULT_READY_TIMEOUT), _HEALTH_REQUEST_TIMEOUT)
    healthy, health_detail = _probe_health(base_url, timeout=probe_timeout)
    resolved_ner = ner_settings or {"enabled": False}

    if healthy:
        if resolved_ner.get("enabled") and _is_local_base_url(base_url):
            binary_path = _find_cloakpipe_binary()
            if binary_path is None:
                attempts = []
                binary_path = _resolve_cloakpipe_binary(base_url, attempts)
            _ensure_ner_ready(binary_path, resolved_ner, timeout=timeout)
        return

    attempts = [f"Probed {_derive_health_url(base_url)} and {health_detail}"]
    config_path: Path | None = None
    log_path: Path | None = None

    if not _is_local_base_url(base_url):
        attempts.append("Skipped automatic local setup because the configured base URL is not localhost/loopback")
        raise CloakPipeUnavailableError(
            _format_unavailable_message(
                base_url=base_url,
                model_id=requested_model,
                attempts=attempts,
                health_detail=health_detail,
                config_path=None,
                log_path=None,
            )
        )

    try:
        binary_path = _resolve_cloakpipe_binary(base_url, attempts)
    except CloakPipeUnavailableError as exc:
        config_path = _write_managed_config(base_url, requested_model, resolved_ner)
        raise CloakPipeUnavailableError(
            _format_unavailable_message(
                base_url=base_url,
                model_id=requested_model,
                attempts=attempts,
                health_detail=health_detail,
                config_path=config_path,
                log_path=None,
            )
        ) from exc

    if resolved_ner.get("enabled"):
        _ensure_ner_ready(binary_path, resolved_ner, timeout=timeout)
        if resolved_ner.get("backend") == "gliner_pii":
            attempts.append(f"Verified the NER sidecar at {resolved_ner['sidecar_url']}")
        else:
            attempts.append(f"Ensured the built-in NER model at {resolved_ner.get('model_path')}")

    config_path = _write_managed_config(base_url, requested_model, resolved_ner)
    attempts.append(f"Prepared a managed config at {config_path}")

    try:
        started, log_path = _start_local_cloakpipe(binary_path, config_path)
    except OSError as exc:
        attempts.append(f"Could not start {binary_path}: {exc}")
        raise CloakPipeUnavailableError(
            _format_unavailable_message(
                base_url=base_url,
                model_id=requested_model,
                attempts=attempts,
                health_detail=health_detail,
                config_path=config_path,
                log_path=None,
            )
        ) from exc

    if started:
        attempts.append(f"Started cloakpipe with {binary_path} --config {config_path} start")
    else:
        attempts.append("Reused the existing managed cloakpipe process")

    healthy, health_detail = _wait_for_health(base_url, timeout=max(_coerce_timeout(timeout, _DEFAULT_READY_TIMEOUT), _STARTUP_TIMEOUT))
    if healthy:
        return

    attempts.append(f"Local startup finished, but {_derive_health_url(base_url)} still failed: {health_detail}")
    raise CloakPipeUnavailableError(
        _format_unavailable_message(
            base_url=base_url,
            model_id=requested_model,
            attempts=attempts,
            health_detail=health_detail,
            config_path=config_path,
            log_path=log_path,
        )
    )


def _to_cloakpipe_model(model_id: str) -> str:
    raw = (model_id or "").strip()
    if not raw:
        return ""
    if raw.startswith("cloakpipe/"):
        return raw
    if "/" in raw:
        provider, model = raw.split("/", 1)
        if provider and model:
            return f"cloakpipe/{provider}-{model}"
    return f"cloakpipe/openai-{raw}"


def _to_upstream_model(model_id: str) -> str:
    raw = (model_id or "").strip()
    split_model = _split_cloakpipe_model(raw)
    if split_model is None:
        return raw

    _, model = split_model
    return model


class CloakPipeProfile(ProviderProfile):
    """Provider profile that exposes CloakPipe as a virtual wrapper."""

    def _ensure_runtime_ready(
        self,
        *,
        timeout: float,
        model: str | None = None,
        reasoning_config: dict[str, Any] | None = None,
        context: dict[str, Any] | None = None,
    ) -> None:
        ner_settings = _resolve_ner_settings(self, reasoning_config or {}, context or {})
        cloakpipe_base_url = _read_base_url()
        _ensure_cloakpipe_ready(
            cloakpipe_base_url,
            timeout=timeout,
            requested_model=model,
            ner_settings=ner_settings,
        )
        _ensure_virtual_provider_ready(self.base_url, cloakpipe_base_url, timeout=timeout)

    def _fallback_model_list(self) -> list[str]:
        fallback_models = getattr(self, "fallback_models", ()) or ()
        mapped = {_to_cloakpipe_model(model_id) for model_id in fallback_models}
        return sorted(model_id for model_id in mapped if model_id)

    def fetch_models(
        self,
        *,
        api_key: str | None = None,
        timeout: float = 8.0,
    ) -> list[str] | None:
        _ = api_key
        self._ensure_runtime_ready(timeout=_coerce_timeout(timeout, _DEFAULT_READY_TIMEOUT))
        return _configured_virtual_models(self._fallback_model_list()) or None

    def build_api_kwargs_extras(
        self,
        *,
        reasoning_config: dict | None = None,
        model: str | None = None,
        **context: Any,
    ) -> tuple[dict[str, Any], dict[str, Any]]:
        self._ensure_runtime_ready(
            timeout=_coerce_timeout(context.get("timeout"), _DEFAULT_READY_TIMEOUT),
            model=model,
            reasoning_config=reasoning_config or {},
            context=context,
        )
        virtual_model = _to_cloakpipe_model(model or "")
        if not virtual_model:
            return {}, {}
        return {}, {"model": virtual_model}


_base_url = _read_provider_base_url()

cloakpipe = CloakPipeProfile(
    name="cloakpipe",
    aliases=("cloak", "cp"),
    display_name="CloakPipe",
    description="CloakPipe privacy wrapper for selected Hermes LLM providers",
    signup_url="https://cloakpipe.co",
    env_vars=_PROVIDER_AUTH_ENV_VARS,
    base_url=_base_url,
    models_url=f"{_base_url}/models",
    auth_type="api_key",
    fallback_models=("cloakpipe/openai-gpt-4o-mini",),
    default_headers={"X-Hermes-Provider": "cloakpipe"},
)

_patch_hermes_model_picker()
_patch_hermes_provider_resolution()
_patch_hermes_model_switch()

register_provider(cloakpipe)
