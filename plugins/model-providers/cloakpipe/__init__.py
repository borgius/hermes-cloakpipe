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
_HEALTH_REQUEST_TIMEOUT = 2.0
_CARGO_INSTALL_TIMEOUT = 300.0
_STARTUP_TIMEOUT = 15.0
_POLL_INTERVAL_SECONDS = 0.25

_process_lock = threading.Lock()
_managed_process: subprocess.Popen[Any] | None = None


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


def _toml_string(value: str) -> str:
    escaped = value.replace("\\", "\\\\").replace('"', '\\"')
    return f'"{escaped}"'


def _render_managed_config(base_url: str, model_id: str | None) -> str:
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
        "",
    ]
    return "\n".join(lines)


def _write_managed_config(base_url: str, model_id: str | None) -> Path:
    runtime_dir = _managed_runtime_dir()
    runtime_dir.mkdir(parents=True, exist_ok=True)

    config_path = runtime_dir / "cloakpipe.toml"
    config_body = _render_managed_config(base_url, model_id)
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


def _ensure_cloakpipe_ready(base_url: str, *, timeout: float = _DEFAULT_READY_TIMEOUT, requested_model: str | None = None) -> None:
    probe_timeout = min(_coerce_timeout(timeout, _DEFAULT_READY_TIMEOUT), _HEALTH_REQUEST_TIMEOUT)
    healthy, health_detail = _probe_health(base_url, timeout=probe_timeout)
    if healthy:
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

    binary_path = _find_cloakpipe_binary()
    if binary_path is None:
        attempts.append("No cloakpipe executable was found on PATH or in ~/.cargo/bin")
        cargo_path = _find_cargo_binary()
        if cargo_path is None:
            attempts.append("Cargo was not available, so automatic CLI installation was skipped")
            config_path = _write_managed_config(base_url, requested_model)
            raise CloakPipeUnavailableError(
                _format_unavailable_message(
                    base_url=base_url,
                    model_id=requested_model,
                    attempts=attempts,
                    health_detail=health_detail,
                    config_path=config_path,
                    log_path=None,
                )
            )

        attempts.append(f"Found Cargo at {cargo_path}; trying cargo install cloakpipe-cli")
        try:
            binary_path = _install_cloakpipe_with_cargo(cargo_path)
        except Exception as exc:
            attempts.append(str(exc))
            config_path = _write_managed_config(base_url, requested_model)
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

        attempts.append(f"Installed cloakpipe and found the binary at {binary_path}")
    else:
        attempts.append(f"Found cloakpipe executable at {binary_path}")

    config_path = _write_managed_config(base_url, requested_model)
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

    def _ensure_runtime_ready(self, *, timeout: float, model: str | None = None) -> None:
        _ensure_cloakpipe_ready(self.base_url, timeout=timeout, requested_model=model)

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
        _ = reasoning_config
        self._ensure_runtime_ready(timeout=_coerce_timeout(context.get("timeout"), _DEFAULT_READY_TIMEOUT), model=model)
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
