"""CloakPipe model-provider plugin for Hermes.

Routes all LLM requests through a CloakPipe proxy and exposes model IDs as:
``cloakpipe/<provider>-<model>``.
"""

from __future__ import annotations

import os
import shlex
import shutil
import subprocess
import threading
import time
from pathlib import Path
from typing import Any
from urllib import error as urllib_error
from urllib import parse as urllib_parse
from urllib import request as urllib_request

from providers import register_provider
from providers.base import ProviderProfile

_DEFAULT_BASE_URL = "http://127.0.0.1:3100/v1"
_DEFAULT_UPSTREAM_URL = "https://api.openai.com"
_DEFAULT_READY_TIMEOUT = 8.0
_DEFAULT_NER_SIDECAR_URL = "http://127.0.0.1:9111"
_HEALTH_REQUEST_TIMEOUT = 2.0
_CARGO_INSTALL_TIMEOUT = 300.0
_NER_INSTALL_TIMEOUT = 300.0
_STARTUP_TIMEOUT = 15.0
_NER_STARTUP_TIMEOUT = 45.0
_POLL_INTERVAL_SECONDS = 0.25
_NER_ENABLED_PROFILES = {"general", "legal", "healthcare"}

_process_lock = threading.Lock()
_ner_process_lock = threading.Lock()
_managed_process: subprocess.Popen[Any] | None = None
_managed_ner_process: subprocess.Popen[Any] | None = None


class CloakPipeUnavailableError(RuntimeError):
    """Raised when CloakPipe cannot be reached or started."""


def _read_base_url() -> str:
    override = os.environ.get("CLOAKPIPE_BASE_URL", "").strip()
    if override:
        return override.rstrip("/")
    return _DEFAULT_BASE_URL


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
        "sidecar_url": _resolve_ner_sidecar_url(*sources),
        "threshold": _resolve_ner_threshold(*sources),
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

    if override:
        candidates.append(Path(override).expanduser())

    current_dir = Path.cwd()
    candidates.extend([current_dir, *current_dir.parents])

    if binary_path is not None:
        candidates.extend([binary_path.parent, *binary_path.parent.parents])

    for candidate in candidates:
        if (candidate / "tools" / "gliner-pii-server.py").exists():
            return candidate

    return None


def _requested_provider(model_id: str | None) -> str:
    upstream_model = _to_upstream_model(model_id or "")
    if "/" not in upstream_model:
        return ""
    provider, _ = upstream_model.split("/", 1)
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
        lines.extend(
            [
                "",
                "[detection.ner]",
                "enabled = true",
                'backend = "gliner-pii"',
                f"confidence_threshold = {ner_settings['threshold']}",
                f"sidecar_url = {_toml_string(ner_settings['sidecar_url'])}",
            ]
        )

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
        attempts.append(f"Verified the NER sidecar at {resolved_ner['sidecar_url']}")

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
    return f"cloakpipe/{raw}"


def _to_upstream_model(model_id: str) -> str:
    raw = (model_id or "").strip()
    if not raw.startswith("cloakpipe/"):
        return raw

    remainder = raw[len("cloakpipe/") :]
    if "-" not in remainder:
        return remainder

    provider, model = remainder.split("-", 1)
    if not provider or not model:
        return remainder
    return f"{provider}/{model}"


class CloakPipeProfile(ProviderProfile):
    """Provider profile that proxies via CloakPipe."""

    def _ensure_runtime_ready(
        self,
        *,
        timeout: float,
        model: str | None = None,
        reasoning_config: dict[str, Any] | None = None,
        context: dict[str, Any] | None = None,
    ) -> None:
        ner_settings = _resolve_ner_settings(self, reasoning_config or {}, context or {})
        _ensure_cloakpipe_ready(
            self.base_url,
            timeout=timeout,
            requested_model=model,
            ner_settings=ner_settings,
        )

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
        self._ensure_runtime_ready(timeout=_coerce_timeout(timeout, _DEFAULT_READY_TIMEOUT))

        try:
            upstream_models = super().fetch_models(api_key=api_key, timeout=timeout)
        except Exception:
            upstream_models = None

        if not upstream_models:
            fallback_models = self._fallback_model_list()
            return fallback_models or None

        mapped = {_to_cloakpipe_model(model_id) for model_id in upstream_models}
        return sorted(model_id for model_id in mapped if model_id)

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
        upstream_model = _to_upstream_model(model or "")
        if not upstream_model:
            return {}, {}
        return {}, {"model": upstream_model}


_base_url = _read_base_url()

cloakpipe = CloakPipeProfile(
    name="cloakpipe",
    aliases=("cloak", "cp"),
    display_name="CloakPipe",
    description="CloakPipe privacy proxy for OpenAI-compatible LLM providers",
    signup_url="https://cloakpipe.co",
    env_vars=("CLOAKPIPE_API_KEY",),
    base_url=_base_url,
    models_url=f"{_base_url}/models",
    auth_type="api_key",
    fallback_models=("cloakpipe/openai-gpt-4o-mini",),
    default_headers={"X-Hermes-Provider": "cloakpipe"},
)

register_provider(cloakpipe)
