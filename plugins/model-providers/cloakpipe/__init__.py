"""CloakPipe virtual model-provider plugin for Hermes.

Hermes selects provider ``cloakpipe`` and the stable model ID
``cloakpipe/latest``.  The plugin remembers the latest real Hermes
provider/model selected before CloakPipe, then keeps CloakPipe out of the LLM
transport path: it pseudonymizes outbound text with CloakPipe's direct privacy
API, dispatches the sanitized payload to that real provider/model, and
rehydrates the response before Hermes sees it.
"""

from __future__ import annotations

import atexit
import copy
import json
import logging
import os
import shlex
import shutil
import subprocess
import sys
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

logger = logging.getLogger(__name__)

_DEFAULT_BASE_URL = "http://127.0.0.1:3100/v1"
_DEFAULT_PROVIDER_BASE_URL = "http://127.0.0.1:3199/v1"
_DEFAULT_UPSTREAM_URL = "https://api.openai.com"
_DEFAULT_OPENAI_BASE_URL = "https://api.openai.com/v1"
_DEFAULT_READY_TIMEOUT = 8.0
_DEFAULT_NER_BACKEND = "distilbert_pii"
_DEFAULT_NER_SIDECAR_URL = "http://127.0.0.1:9111"
_CLOAKPIPE_MODEL_ID = "cloakpipe/latest"
_CLOAKPIPE_UPSTREAM_BODY_KEY = "_cloakpipe_upstream"
_UPSTREAM_STATE_FILE = "latest-upstream.json"
_HEALTH_REQUEST_TIMEOUT = 2.0
_CARGO_INSTALL_TIMEOUT = 300.0
_NER_DOWNLOAD_TIMEOUT = 300.0
_NER_INSTALL_TIMEOUT = 300.0
_STARTUP_TIMEOUT = 15.0
_NER_STARTUP_TIMEOUT = 45.0
_POLL_INTERVAL_SECONDS = 0.25
_NER_ENABLED_PROFILES = {"general", "legal", "healthcare"}
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
_upstream_state_lock = threading.Lock()
_managed_process: subprocess.Popen[Any] | None = None
_managed_ner_process: subprocess.Popen[Any] | None = None
_virtual_server: ThreadingHTTPServer | None = None
_virtual_server_thread: threading.Thread | None = None
_startup_warning_lock = threading.Lock()
_startup_warning_text: str | None = None
_last_emitted_startup_warning: str | None = None
_shutdown_hooks_registered = False


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


def _is_cloakpipe_provider(provider: str | None) -> bool:
    return str(provider or "").strip().lower() in {"cloakpipe", "cloak", "cp"}


def _is_cloakpipe_model_id(model_id: str | None) -> bool:
    raw = str(model_id or "").strip().lower()
    return raw in {"cloakpipe", "cloakpipe/latest", "cloak/latest", "cp/latest", "latest"}


def _upstream_selection_path() -> Path:
    return _managed_runtime_dir() / _UPSTREAM_STATE_FILE


def _normalize_upstream_selection(
    provider: Any,
    model: Any,
    *,
    source: str,
    updated_at: Any | None = None,
    context_length: Any | None = None,
) -> dict[str, Any] | None:
    provider_id = str(provider or "").strip().lower()
    model_id = str(model or "").strip()
    if not provider_id or not model_id:
        return None
    if _is_cloakpipe_provider(provider_id) or _is_cloakpipe_model_id(model_id):
        return None

    try:
        timestamp = float(updated_at) if updated_at is not None else time.time()
    except (TypeError, ValueError):
        timestamp = time.time()

    selection = {
        "provider": provider_id,
        "model": model_id,
        "display": f"{provider_id}/{model_id}",
        "source": str(source or "unknown"),
        "updated_at": timestamp,
    }
    normalized_context_length = _normalize_context_length(context_length)
    if normalized_context_length is not None:
        selection["context_length"] = normalized_context_length
    return selection


def _selection_from_mapping(value: Any, *, source: str) -> dict[str, Any] | None:
    if not isinstance(value, dict):
        return None
    return _normalize_upstream_selection(
        value.get("provider"),
        value.get("model"),
        source=str(value.get("source") or source),
        updated_at=value.get("updated_at"),
        context_length=value.get("context_length"),
    )


def _read_env_upstream_selection() -> dict[str, Any] | None:
    provider = os.environ.get("CLOAKPIPE_UPSTREAM_PROVIDER", "").strip()
    model = os.environ.get("CLOAKPIPE_UPSTREAM_MODEL", "").strip()
    return _normalize_upstream_selection(provider, model, source="env")


def _read_saved_upstream_selection() -> dict[str, Any] | None:
    path = _upstream_selection_path()
    with _upstream_state_lock:
        try:
            raw = path.read_text(encoding="utf-8")
        except FileNotFoundError:
            return None
        except OSError:
            return None

    try:
        data = json.loads(raw)
    except json.JSONDecodeError:
        return None
    return _selection_from_mapping(data, source="state")


def _remember_upstream_selection(
    provider: Any,
    model: Any,
    *,
    source: str = "model_switch",
    context_length: Any | None = None,
) -> dict[str, Any] | None:
    selection = _normalize_upstream_selection(provider, model, source=source, context_length=context_length)
    if selection is None:
        return None

    path = _upstream_selection_path()
    temp_path = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
    payload = json.dumps(selection, indent=2, sort_keys=True)
    with _upstream_state_lock:
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            temp_path.write_text(payload, encoding="utf-8")
            temp_path.replace(path)
        except OSError:
            try:
                temp_path.unlink(missing_ok=True)
            except OSError:
                pass
            return None
    return selection


def _latest_upstream_selection() -> dict[str, Any] | None:
    return _read_saved_upstream_selection() or _read_env_upstream_selection()


def _latest_upstream_selection_or_raise() -> dict[str, Any]:
    selection = _latest_upstream_selection()
    if selection is not None:
        return selection
    raise CloakPipeRequestError(
        (
            "CloakPipe does not know which upstream model to wrap yet. "
            "Select a real Hermes model first, then switch to provider 'cloakpipe' "
            "with model 'cloakpipe/latest'. For non-interactive runs, set both "
            "CLOAKPIPE_UPSTREAM_PROVIDER and CLOAKPIPE_UPSTREAM_MODEL."
        ),
        status=400,
        code="upstream_model_not_selected",
    )


def _latest_upstream_display() -> str:
    selection = _latest_upstream_selection()
    if selection is None:
        return "select a real model first"
    return str(selection.get("display") or f"{selection['provider']}/{selection['model']}")


def _latest_upstream_model_label() -> str | None:
    selection = _latest_upstream_selection()
    if selection is None:
        return None
    model = str(selection.get("model") or "").strip()
    return model or None


def _format_cloakpipe_switch_label(model_id: str | None) -> str:
    raw = str(model_id or "").strip()
    if not _is_cloakpipe_model_id(raw):
        return raw

    upstream_model = _latest_upstream_model_label()
    if not upstream_model:
        return _CLOAKPIPE_MODEL_ID

    return f"{_CLOAKPIPE_MODEL_ID} ({upstream_model})"


def _rewrite_cloakpipe_switch_text(text: str) -> str:
    raw = str(text or "")
    marker = "Model switched:"
    if marker not in raw:
        return raw

    formatted_label = _format_cloakpipe_switch_label(_CLOAKPIPE_MODEL_ID)
    if formatted_label == _CLOAKPIPE_MODEL_ID or formatted_label in raw:
        return raw

    prefix, separator, suffix = raw.partition(marker)
    if _CLOAKPIPE_MODEL_ID not in suffix:
        return raw
    return f"{prefix}{separator}{suffix.replace(_CLOAKPIPE_MODEL_ID, formatted_label, 1)}"


def _resolve_model_context_length(
    model: str,
    *,
    provider: str,
    base_url: str = "",
    api_key: Any = "",
    custom_providers: list[Any] | None = None,
    resolver=None,
) -> int | None:
    effective_resolver = resolver
    if effective_resolver is None:
        try:
            from agent.model_metadata import get_model_context_length
        except Exception:
            return None
        effective_resolver = get_model_context_length

    effective_model = str(model or "").strip()
    effective_provider = _canonical_upstream_provider(provider)
    effective_api_key = api_key if isinstance(api_key, str) else ""
    if not effective_model or not effective_provider:
        return None

    try:
        resolved = effective_resolver(
            effective_model,
            base_url=str(base_url or "").strip(),
            api_key=effective_api_key,
            config_context_length=None,
            provider=effective_provider,
            custom_providers=custom_providers,
        )
    except Exception:
        return None
    return _normalize_context_length(resolved)


def _latest_upstream_context_length(*, custom_providers: list[Any] | None = None, resolver=None) -> int | None:
    selection = _latest_upstream_selection()
    if selection is None:
        return None

    saved_context_length = _normalize_context_length(selection.get("context_length"))
    if saved_context_length is not None:
        return saved_context_length

    provider = str(selection.get("provider") or "").strip().lower()
    model = str(selection.get("model") or "").strip()
    if not provider or not model or _is_cloakpipe_provider(provider) or _is_cloakpipe_model_id(model):
        return None

    runtime_provider = _canonical_upstream_provider(provider)
    runtime_base_url = ""
    runtime_api_key: Any = ""

    try:
        from hermes_cli.runtime_provider import resolve_runtime_provider

        runtime = resolve_runtime_provider(requested=provider, target_model=model)
    except Exception:
        runtime = None

    if isinstance(runtime, dict):
        runtime_provider = str(runtime.get("provider") or runtime_provider or provider)
        runtime_base_url = str(runtime.get("base_url") or "").strip()
        runtime_api_key = runtime.get("api_key", "")

    return _resolve_model_context_length(
        model,
        provider=runtime_provider or provider,
        base_url=runtime_base_url,
        api_key=runtime_api_key,
        custom_providers=custom_providers,
        resolver=resolver,
    )


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


def _normalize_context_length(value: Any) -> int | None:
    try:
        context_length = int(value)
    except (TypeError, ValueError):
        return None
    if context_length <= 0:
        return None
    return context_length


def _is_truthy(value: Any) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return bool(value)
    if isinstance(value, str):
        return value.strip().lower() in {"1", "true", "yes", "on", "enabled"}
    return bool(value)


def _is_debug_enabled() -> bool:
    for env_name in ("CLOAKPIPE_DEBUG", "CLOAKPIPE_WRAPPER_DEBUG"):
        if _is_truthy(os.environ.get(env_name, "").strip()):
            return True
    return False


def _emit_debug(message: str) -> None:
    normalized = str(message or "").strip()
    if not normalized or not _is_debug_enabled():
        return

    logger.info("CloakPipe debug: %s", normalized)

    try:
        print(f"[CloakPipe debug] {normalized}", flush=True)
    except Exception:
        return


def _stop_managed_process(
    *,
    attr_name: str,
    lock: threading.Lock,
    label: str,
    timeout: float = 5.0,
) -> None:
    process: subprocess.Popen[Any] | None = None

    with lock:
        process = globals().get(attr_name)
        globals()[attr_name] = None

    if process is None:
        return

    exit_code = process.poll()
    if exit_code is not None:
        _emit_debug(f"{label} already exited with code {exit_code}")
        return

    _emit_debug(f"Stopping managed {label} process (pid={process.pid})")
    try:
        process.terminate()
    except OSError as exc:
        _emit_debug(f"Failed to terminate managed {label} process: {exc}")
        return

    try:
        process.wait(timeout=timeout)
    except subprocess.TimeoutExpired:
        _emit_debug(f"Managed {label} process did not exit after {timeout}s; killing it")
        try:
            process.kill()
            process.wait(timeout=1.0)
        except OSError as exc:
            _emit_debug(f"Failed to kill managed {label} process: {exc}")
    else:
        _emit_debug(f"Managed {label} process exited with code {process.returncode}")


def _stop_virtual_provider_server() -> None:
    global _virtual_server, _virtual_server_thread

    server: ThreadingHTTPServer | None = None
    thread: threading.Thread | None = None
    with _virtual_server_lock:
        server = _virtual_server
        thread = _virtual_server_thread
        _virtual_server = None
        _virtual_server_thread = None

    if server is None:
        return

    _emit_debug("Stopping in-process CloakPipe Hermes wrapper server")
    try:
        server.shutdown()
        server.server_close()
    except OSError as exc:
        _emit_debug(f"Failed to stop in-process CloakPipe Hermes wrapper server: {exc}")

    if thread is not None and thread.is_alive():
        thread.join(timeout=1.0)


def _shutdown_managed_runtime() -> None:
    _emit_debug("Interpreter shutdown detected; cleaning up managed CloakPipe runtime")
    _stop_virtual_provider_server()
    _stop_managed_process(attr_name="_managed_ner_process", lock=_ner_process_lock, label="CloakPipe NER")
    _stop_managed_process(attr_name="_managed_process", lock=_process_lock, label="CloakPipe")


def _ensure_shutdown_hooks_registered() -> None:
    global _shutdown_hooks_registered

    if _shutdown_hooks_registered:
        return

    atexit.register(_shutdown_managed_runtime)
    _shutdown_hooks_registered = True
    _emit_debug("Registered CloakPipe managed-runtime shutdown hook")


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
    _emit_debug(f"Health check: {health_url} (timeout={_coerce_timeout(timeout, _HEALTH_REQUEST_TIMEOUT):.2f}s)")
    request = urllib_request.Request(health_url, method="GET")

    try:
        with urllib_request.urlopen(request, timeout=_coerce_timeout(timeout, _HEALTH_REQUEST_TIMEOUT)) as response:
            status = getattr(response, "status", None)
            if status is None:
                status = response.getcode()
            response.read()
        if 200 <= status < 300:
            _emit_debug(f"Health check OK: {health_url} -> HTTP {status}")
            return True, f"responded with HTTP {status}"
        _emit_debug(f"Health check failed: {health_url} -> HTTP {status}")
        return False, f"responded with HTTP {status}"
    except urllib_error.HTTPError as exc:
        _emit_debug(f"Health check failed: {health_url} -> HTTP {exc.code}")
        return False, f"returned HTTP {exc.code}"
    except urllib_error.URLError as exc:
        _emit_debug(f"Health check failed: {health_url} -> {exc.reason}")
        return False, f"request failed: {exc.reason}"
    except TimeoutError:
        _emit_debug(f"Health check timed out: {health_url}")
        return False, "timed out"
    except Exception as exc:  # pragma: no cover - defensive fallback
        _emit_debug(f"Health check failed unexpectedly: {health_url} -> {exc}")
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


def _pop_request_upstream_selection(body: dict[str, Any]) -> dict[str, Any]:
    raw_selection = body.pop(_CLOAKPIPE_UPSTREAM_BODY_KEY, None)
    extra_body = body.get("extra_body")
    if raw_selection is None and isinstance(extra_body, dict):
        raw_selection = extra_body.pop(_CLOAKPIPE_UPSTREAM_BODY_KEY, None)
        if not extra_body:
            body.pop("extra_body", None)

    selection = _selection_from_mapping(raw_selection, source="request")
    if selection is None:
        raise CloakPipeRequestError(
            (
                "CloakPipe request is missing upstream routing metadata. "
                "Select a real Hermes model first, then switch to 'cloakpipe/latest', "
                "or set CLOAKPIPE_UPSTREAM_PROVIDER and CLOAKPIPE_UPSTREAM_MODEL."
            ),
            status=400,
            code="upstream_model_not_selected",
        )
    return selection


def _normalize_requested_cloakpipe_model(model_id: str | None) -> str:
    raw = str(model_id or "").strip()
    if not raw or _is_cloakpipe_model_id(raw):
        return _CLOAKPIPE_MODEL_ID
    raise CloakPipeRequestError(
        f"CloakPipe exposes one model, '{_CLOAKPIPE_MODEL_ID}'. Select a real model first, then switch to CloakPipe.",
        status=400,
        code="invalid_model",
    )


def _dispatch_chat_completion(body: dict[str, Any], *, cloakpipe_base_url: str, timeout: float) -> dict[str, Any]:
    request_body = copy.deepcopy(body)
    requested_model = _normalize_requested_cloakpipe_model(request_body.get("model"))
    request_body["model"] = requested_model

    upstream_selection = _pop_request_upstream_selection(request_body)
    provider = str(upstream_selection["provider"])
    upstream_model = str(upstream_selection["model"])

    sanitized = _pseudonymize_chat_body(request_body, base_url=cloakpipe_base_url, timeout=timeout)
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
                    "Select another real OpenRouter model before switching to CloakPipe, or update "
                    "https://openrouter.ai/settings/privacy"
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
    payload["model"] = requested_model
    return _rehydrate_chat_response(payload, base_url=cloakpipe_base_url, timeout=timeout)


def _is_openrouter_privacy_restricted_error(error: Exception | str | None) -> bool:
    text = str(error or "").lower()
    return (
        "no endpoints available matching your guardrail restrictions and data policy" in text
        or "openrouter.ai/settings/privacy" in text
    )


def _with_cloakpipe_picker_row(
    rows: list[dict[str, Any]],
    *,
    current_provider: str = "",
    max_models: int = 8,
) -> list[dict[str, Any]]:
    _ = max_models
    normalized_rows = [dict(row) for row in rows if isinstance(row, dict)]
    current = str(current_provider or "").strip().lower()
    label = f"CloakPipe: {_latest_upstream_display()}"
    startup_warning = _startup_warning_summary()
    inserted = False
    for row in normalized_rows:
        if str(row.get("slug") or "").strip().lower() != "cloakpipe":
            continue
        row["models"] = [_CLOAKPIPE_MODEL_ID]
        row["total_models"] = 1
        row["name"] = label
        row["is_current"] = bool(row.get("is_current")) or current == "cloakpipe"
        if startup_warning:
            row["warning"] = startup_warning
        else:
            row.pop("warning", None)
        inserted = True
        break

    if not inserted:
        normalized_rows.append(
            {
                "slug": "cloakpipe",
                "name": label,
                "is_current": current == "cloakpipe",
                "is_user_defined": False,
                "models": [_CLOAKPIPE_MODEL_ID],
                "total_models": 1,
                "source": "plugin",
                **({"warning": startup_warning} if startup_warning else {}),
            }
        )

    normalized_rows.sort(key=lambda row: (not bool(row.get("is_current")), -int(row.get("total_models") or 0)))
    return normalized_rows


def _build_cloakpipe_model_card() -> dict[str, Any]:
    model_card: dict[str, Any] = {
        "id": _CLOAKPIPE_MODEL_ID,
        "object": "model",
        "owned_by": "cloakpipe",
    }
    context_length = _latest_upstream_context_length()
    if context_length is not None:
        model_card["context_length"] = context_length
        model_card["context_window"] = context_length
    return model_card


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


def _patch_hermes_context_length_resolution() -> None:
    try:
        import agent.model_metadata as model_metadata
    except Exception:
        return

    if getattr(model_metadata, "_cloakpipe_context_length_patched", False):
        return

    original_get_model_context_length = getattr(model_metadata, "get_model_context_length", None)
    if callable(original_get_model_context_length):

        def _patched_get_model_context_length(
            model: str,
            base_url: str = "",
            api_key: Any = "",
            config_context_length: int | None = None,
            provider: str = "",
            custom_providers: list | None = None,
        ) -> int:
            if _normalize_context_length(config_context_length) is None and (
                _is_cloakpipe_provider(provider) or _is_cloakpipe_model_id(model)
            ):
                context_length = _latest_upstream_context_length(
                    custom_providers=custom_providers,
                    resolver=original_get_model_context_length,
                )
                if context_length is not None:
                    return context_length

            return original_get_model_context_length(
                model,
                base_url=base_url,
                api_key=api_key,
                config_context_length=config_context_length,
                provider=provider,
                custom_providers=custom_providers,
            )

        model_metadata.get_model_context_length = _patched_get_model_context_length

    model_metadata._cloakpipe_context_length_patched = True


def _patch_cli_model_switch_display() -> None:
    cli_module = sys.modules.get("cli")
    if cli_module is None or getattr(cli_module, "_cloakpipe_model_switch_display_patched", False):
        return

    original_cprint = getattr(cli_module, "_cprint", None)
    if callable(original_cprint):

        def _patched_cprint(text: str):
            return original_cprint(_rewrite_cloakpipe_switch_text(text))

        cli_module._cprint = _patched_cprint

    cli_module._cloakpipe_model_switch_display_patched = True


def _patch_gateway_model_switch_display() -> None:
    gateway_run_module = sys.modules.get("gateway.run")
    if gateway_run_module is None or getattr(gateway_run_module, "_cloakpipe_model_switch_display_patched", False):
        return

    runner_cls = getattr(gateway_run_module, "GatewayRunner", None)
    original_handle_model_command = getattr(runner_cls, "_handle_model_command", None)
    if callable(original_handle_model_command):

        async def _patched_handle_model_command(self, event):
            result = await original_handle_model_command(self, event)
            if isinstance(result, str):
                return _rewrite_cloakpipe_switch_text(result)
            return result

        runner_cls._handle_model_command = _patched_handle_model_command

    gateway_run_module._cloakpipe_model_switch_display_patched = True


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


def _patch_hermes_model_aliases() -> None:
    try:
        import hermes_cli.model_switch as model_switch
    except Exception:
        return

    if getattr(model_switch, "_cloakpipe_aliases_patched", False):
        return

    direct_alias_cls = getattr(model_switch, "DirectAlias", None)
    builtin_direct_aliases = getattr(model_switch, "_BUILTIN_DIRECT_ALIASES", None)
    if direct_alias_cls is None or not isinstance(builtin_direct_aliases, dict):
        return

    cloakpipe_alias = direct_alias_cls(
        model=_CLOAKPIPE_MODEL_ID,
        provider="cloakpipe",
        base_url=_read_provider_base_url(),
    )
    builtin_direct_aliases.setdefault("cloakpipe", cloakpipe_alias)

    direct_aliases = getattr(model_switch, "DIRECT_ALIASES", None)
    if isinstance(direct_aliases, dict) and direct_aliases:
        direct_aliases.setdefault("cloakpipe", cloakpipe_alias)

    model_switch._cloakpipe_aliases_patched = True


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
            target_explicit_provider = str(explicit_provider or "").strip()
            switching_to_cloakpipe = _is_cloakpipe_provider(target_explicit_provider) or _is_cloakpipe_model_id(requested_model)
            if requested_model.startswith("cloakpipe/"):
                switching_to_cloakpipe = True

            if switching_to_cloakpipe:
                current_context_length = _resolve_model_context_length(
                    current_model,
                    provider=current_provider,
                    base_url=current_base_url,
                    api_key=current_api_key,
                    custom_providers=custom_providers,
                )
                _remember_upstream_selection(
                    current_provider,
                    current_model,
                    source="model_switch",
                    context_length=current_context_length,
                )
                target_explicit_provider = "cloakpipe"
                requested_model = _CLOAKPIPE_MODEL_ID

            result = original_switch_model(
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

            if switching_to_cloakpipe:
                startup_warning = _preflight_cloakpipe_activation(
                    requested_model=requested_model,
                    timeout=_coerce_timeout(os.environ.get("CLOAKPIPE_REQUEST_TIMEOUT"), _DEFAULT_READY_TIMEOUT),
                )
                if startup_warning:
                    _emit_startup_warning(startup_warning)
                    _set_result_warning_message(result, _summarize_startup_warning(startup_warning) or startup_warning)
                _set_result_warning_message(result, _activation_debug_summary_message(startup_warning))
            else:
                selection = _selection_from_mapping(result, source="model_switch")
                if selection is not None:
                    _remember_upstream_selection(selection["provider"], selection["model"], source="model_switch")

            return result

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
            self._send_json(
                HTTPStatus.OK,
                {
                    "object": "list",
                    "data": [_build_cloakpipe_model_card()],
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
                _emit_debug(f"Reusing in-process CloakPipe Hermes wrapper at {provider_base_url}")
                return
            _virtual_server.shutdown()
            _virtual_server.server_close()
            _virtual_server = None
            _virtual_server_thread = None

        host, port = _parse_http_binding(provider_base_url)
        _ensure_shutdown_hooks_registered()
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
        _emit_debug(f"Started in-process CloakPipe Hermes wrapper at {provider_base_url}")


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
        _emit_debug(f"Hermes wrapper already healthy at {provider_base_url}")
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
    _emit_debug(f"Hermes wrapper became healthy at {provider_base_url}")


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


def _select_proxy_upstream_url() -> str:
    for env_name in ("CLOAKPIPE_PROXY_UPSTREAM_URL", "CLOAKPIPE_UPSTREAM_URL"):
        override = os.environ.get(env_name, "").strip()
        if override:
            return override.rstrip("/")
    return _DEFAULT_UPSTREAM_URL


def _select_proxy_api_key_env() -> str:
    for env_name in ("CLOAKPIPE_PROXY_API_KEY_ENV", "CLOAKPIPE_UPSTREAM_API_KEY_ENV"):
        override = os.environ.get(env_name, "").strip()
        if override:
            return override

    for candidate in ("CLOAKPIPE_API_KEY", "OPENAI_API_KEY"):
        if os.environ.get(candidate):
            return candidate
    return "CLOAKPIPE_API_KEY"


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
    _ = model_id
    runtime_dir = _managed_runtime_dir()
    vault_path = runtime_dir / "vault.enc"

    lines = [
        "[proxy]",
        f"listen = {_toml_string(_to_listen_address(base_url))}",
        f"upstream = {_toml_string(_select_proxy_upstream_url())}",
        f"api_key_env = {_toml_string(_select_proxy_api_key_env())}",
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

    audit_dir = runtime_dir / "audit"
    lines.extend(
        [
            "",
            "[audit]",
            "enabled = true",
            f"log_path = {_toml_string(str(audit_dir))}",
            'format = "jsonl"',
            "retention_days = 90",
            "log_entities = true",
            "log_mappings = false",
        ]
    )

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


def _remember_startup_warning(message: str | None) -> None:
    global _startup_warning_text

    normalized = str(message or "").strip() or None
    if normalized is None:
        _clear_startup_warning()
        return

    with _startup_warning_lock:
        _startup_warning_text = normalized


def _get_startup_warning() -> str | None:
    with _startup_warning_lock:
        return _startup_warning_text


def _clear_startup_warning() -> None:
    global _startup_warning_text, _last_emitted_startup_warning

    with _startup_warning_lock:
        _startup_warning_text = None
        _last_emitted_startup_warning = None


def _summarize_startup_warning(message: str | None) -> str | None:
    normalized = str(message or "").strip()
    if not normalized:
        return None

    for line in normalized.splitlines():
        summary = line.strip()
        if summary:
            return _trim_output(summary, limit=180)

    return None


def _startup_warning_summary() -> str | None:
    return _summarize_startup_warning(_get_startup_warning())


def _combine_warning_messages(existing: Any, addition: str | None) -> str | None:
    normalized_addition = str(addition or "").strip()
    if not normalized_addition:
        normalized_existing = str(existing or "").strip()
        return normalized_existing or None

    normalized_existing = str(existing or "").strip()
    if not normalized_existing:
        return normalized_addition
    if normalized_addition in normalized_existing:
        return normalized_existing
    return f"{normalized_existing} | {normalized_addition}"


def _set_result_warning_message(result: Any, message: str | None) -> None:
    combined: str | None
    if isinstance(result, dict):
        combined = _combine_warning_messages(result.get("warning_message"), message)
        if combined is not None:
            result["warning_message"] = combined
        return

    try:
        existing = getattr(result, "warning_message", "")
    except Exception:
        return

    combined = _combine_warning_messages(existing, message)
    if combined is None:
        return

    try:
        setattr(result, "warning_message", combined)
    except Exception:
        return


def _activation_debug_summary_message(startup_warning: str | None) -> str | None:
    if not _is_debug_enabled():
        return None
    if startup_warning:
        return "CloakPipe debug: activation preflight ran and reported a warning."
    return "CloakPipe debug: activation preflight ran successfully."


def _emit_startup_warning(message: str) -> None:
    global _last_emitted_startup_warning

    normalized = str(message or "").strip()
    if not normalized:
        return

    summary = _summarize_startup_warning(normalized)

    try:
        import logging

        logging.getLogger(__name__).warning("CloakPipe activation preflight warning: %s", normalized)
    except Exception:
        pass

    if summary is None:
        return

    with _startup_warning_lock:
        if _last_emitted_startup_warning == summary:
            return
        _last_emitted_startup_warning = summary

    try:
        from hermes_cli.cli_output import print_warning
    except Exception:
        return

    print_warning(summary)


def _activation_preflight_ner_settings() -> dict[str, Any]:
    """Resolve env-driven NER settings for activation-time startup checks.

    Model-provider plugins bypass Hermes' general plugin manager, so they do
    not receive ``on_session_start``. Session-start hooks also do not carry the
    request/profile context needed for profile-specific NER backends. Activation
    preflight therefore honors only global/env settings; the request-time check
    still resolves the full context-aware NER configuration.
    """

    return _resolve_ner_settings({})


def _preflight_cloakpipe_activation(
    *,
    requested_model: str | None,
    timeout: float = _DEFAULT_READY_TIMEOUT,
) -> str | None:
    cloakpipe_base_url = _read_base_url()
    provider_base_url = _read_provider_base_url()
    activation_ner_settings = _activation_preflight_ner_settings()
    _emit_debug(
        f"Activation preflight started for model={requested_model or _CLOAKPIPE_MODEL_ID}, "
        f"cloakpipe_base_url={cloakpipe_base_url}, wrapper_base_url={provider_base_url}, "
        f"ner_backend={activation_ner_settings.get('backend')}, ner_enabled={activation_ner_settings.get('enabled')}"
    )

    try:
        _ensure_cloakpipe_ready(
            cloakpipe_base_url,
            timeout=timeout,
            requested_model=requested_model,
            ner_settings=activation_ner_settings,
        )
        _ensure_virtual_provider_ready(provider_base_url, cloakpipe_base_url, timeout=timeout)
    except CloakPipeUnavailableError as exc:
        warning = str(exc)
        _remember_startup_warning(warning)
        _emit_debug(f"Activation preflight failed: {warning}")
        return warning
    except Exception as exc:  # pragma: no cover - defensive fallback
        warning = f"CloakPipe activation preflight failed: {exc}"
        _remember_startup_warning(warning)
        _emit_debug(warning)
        return warning

    _clear_startup_warning()
    _emit_debug("Activation preflight completed successfully")
    return None


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
            _emit_debug(f"Reusing managed CloakPipe process (pid={_managed_process.pid})")
            return False, log_path

        _managed_process = None
        _ensure_shutdown_hooks_registered()
        with log_path.open("a", encoding="utf-8") as log_file:
            _managed_process = subprocess.Popen(
                [str(binary_path), "--config", str(config_path), "start"],
                cwd=str(config_path.parent),
                stdin=subprocess.DEVNULL,
                stdout=log_file,
                stderr=subprocess.STDOUT,
                close_fds=True,
            )
            _emit_debug(f"Started managed CloakPipe process pid={_managed_process.pid}; logs -> {log_path}")

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
            _emit_debug(f"Reusing managed CloakPipe NER process (pid={_managed_ner_process.pid})")
            return False, log_path

        _managed_ner_process = None
        _ensure_shutdown_hooks_registered()
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
            _emit_debug(f"Started managed CloakPipe NER process pid={_managed_ner_process.pid}; logs -> {log_path}")

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
        _emit_debug(f"Using existing cloakpipe binary at {binary_path}")
        return binary_path

    attempts.append("No cloakpipe executable was found on PATH or in ~/.cargo/bin")
    _emit_debug("No cloakpipe binary found on PATH or in ~/.cargo/bin")
    cargo_path = _find_cargo_binary()
    if cargo_path is None:
        attempts.append("Cargo was not available, so automatic CLI installation was skipped")
        _emit_debug("Cargo was not found; automatic CloakPipe installation cannot continue")
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
    _emit_debug(f"Attempting cargo install cloakpipe-cli via {cargo_path}")
    try:
        binary_path = _install_cloakpipe_with_cargo(cargo_path)
    except Exception as exc:
        attempts.append(str(exc))
        _emit_debug(f"Automatic CloakPipe installation failed: {exc}")
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
    _emit_debug(f"Installed cloakpipe successfully at {binary_path}")
    return binary_path


def _ensure_ner_ready(binary_path: Path, ner_settings: dict[str, Any], *, timeout: float) -> None:
    backend = _normalize_ner_backend(ner_settings.get("backend")) or _DEFAULT_NER_BACKEND
    ner_settings["backend"] = backend
    _emit_debug(
        "Checking NER readiness "
        f"(backend={backend}, enabled={bool(ner_settings.get('enabled'))}, "
        f"sidecar_url={ner_settings.get('sidecar_url')}, model_path={ner_settings.get('model_path')})"
    )

    if backend != "gliner_pii":
        attempts: list[str] = []
        configured_model_path = ner_settings.get("model_path")
        resolved_model_path: Path | None = None

        if isinstance(configured_model_path, str) and configured_model_path.strip():
            resolved_model_path = Path(configured_model_path).expanduser()
            ner_settings["model_path"] = str(resolved_model_path)
            if resolved_model_path.is_file():
                _emit_debug(f"Using configured built-in NER model at {resolved_model_path}")
                return
            attempts.append(f"Configured NER model path {resolved_model_path} was not present")
            _emit_debug(f"Configured built-in NER model path missing: {resolved_model_path}")

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
            _emit_debug(f"Built-in DistilBERT NER model already present at {expected_model_path}")
            return

        attempts.append(
            f"Built-in DistilBERT model was missing at {expected_model_path}; trying cloakpipe ner download"
        )
        try:
            downloaded_model_path = _download_ner_model_with_cloakpipe(binary_path, source_dir)
        except Exception as exc:
            attempts.append(str(exc))
            _emit_debug(f"Built-in NER model download failed: {exc}")
            raise CloakPipeUnavailableError(
                _format_distilbert_ner_unavailable_message(
                    attempts=attempts,
                    model_path=expected_model_path,
                )
            ) from exc

        ner_settings["model_path"] = str(downloaded_model_path)
        _emit_debug(f"Downloaded built-in DistilBERT NER model to {downloaded_model_path}")
        return

    sidecar_url = ner_settings["sidecar_url"]
    probe_timeout = min(_coerce_timeout(timeout, _DEFAULT_READY_TIMEOUT), _HEALTH_REQUEST_TIMEOUT)
    healthy, health_detail = _probe_health(sidecar_url, timeout=probe_timeout)
    if healthy:
        _emit_debug(f"NER sidecar already healthy at {sidecar_url}")
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
            _emit_debug(f"cloakpipe ner install failed: {exc}")
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
        _emit_debug(f"Failed to start managed NER sidecar: {exc}")
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
        _emit_debug(f"NER sidecar became healthy at {sidecar_url}")
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
    _emit_debug(
        f"Checking CloakPipe readiness for base_url={base_url}, requested_model={requested_model or ''}, "
        f"ner_enabled={bool((ner_settings or {}).get('enabled'))}"
    )
    probe_timeout = min(_coerce_timeout(timeout, _DEFAULT_READY_TIMEOUT), _HEALTH_REQUEST_TIMEOUT)
    healthy, health_detail = _probe_health(base_url, timeout=probe_timeout)
    resolved_ner = ner_settings or {"enabled": False}

    if healthy:
        _emit_debug(f"CloakPipe already healthy at {base_url}")
        if resolved_ner.get("enabled") and _is_local_base_url(base_url):
            binary_path = _find_cloakpipe_binary()
            if binary_path is None:
                attempts = []
                binary_path = _resolve_cloakpipe_binary(base_url, attempts)
            _ensure_ner_ready(binary_path, resolved_ner, timeout=timeout)
        return

    attempts = [f"Probed {_derive_health_url(base_url)} and {health_detail}"]
    _emit_debug(f"CloakPipe not healthy at {base_url}: {health_detail}")
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
    _emit_debug(f"Prepared managed CloakPipe config at {config_path}")

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
        _emit_debug(f"Managed CloakPipe process became healthy at {base_url}")
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

    def fetch_models(
        self,
        *,
        api_key: str | None = None,
        timeout: float = 8.0,
    ) -> list[str] | None:
        _ = api_key, timeout
        return [_CLOAKPIPE_MODEL_ID]

    def build_api_kwargs_extras(
        self,
        *,
        reasoning_config: dict | None = None,
        model: str | None = None,
        **context: Any,
    ) -> tuple[dict[str, Any], dict[str, Any]]:
        virtual_model = _normalize_requested_cloakpipe_model(model)
        upstream_selection = _latest_upstream_selection_or_raise()
        try:
            self._ensure_runtime_ready(
                timeout=_coerce_timeout(context.get("timeout"), _DEFAULT_READY_TIMEOUT),
                model=virtual_model,
                reasoning_config=reasoning_config or {},
                context=context,
            )
        except CloakPipeUnavailableError as exc:
            _remember_startup_warning(str(exc))
            raise
        _clear_startup_warning()
        return {_CLOAKPIPE_UPSTREAM_BODY_KEY: upstream_selection}, {"model": virtual_model}


_base_url = _read_provider_base_url()

cloakpipe = CloakPipeProfile(
    name="cloakpipe",
    aliases=("cloak", "cp"),
    display_name="CloakPipe",
    description="CloakPipe privacy wrapper for the latest selected Hermes LLM provider",
    signup_url="https://cloakpipe.co",
    env_vars=_PROVIDER_AUTH_ENV_VARS,
    base_url=_base_url,
    models_url=f"{_base_url}/models",
    auth_type="api_key",
    fallback_models=(_CLOAKPIPE_MODEL_ID,),
    default_headers={"X-Hermes-Provider": "cloakpipe"},
)

_patch_hermes_model_picker()
_patch_hermes_provider_resolution()
_patch_hermes_context_length_resolution()
_patch_cli_model_switch_display()
_patch_gateway_model_switch_display()
_patch_hermes_model_aliases()
_patch_hermes_model_switch()

register_provider(cloakpipe)
