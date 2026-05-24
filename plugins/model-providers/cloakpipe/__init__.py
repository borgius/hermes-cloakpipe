"""CloakPipe model-provider plugin for Hermes.

Routes all LLM requests through a CloakPipe proxy and exposes model IDs as:
``cloakpipe/<provider>-<model>``.
"""

from __future__ import annotations

import os
from typing import Any

from providers import register_provider
from providers.base import ProviderProfile

_DEFAULT_BASE_URL = "http://127.0.0.1:3100/v1"


def _read_base_url() -> str:
    override = os.environ.get("CLOAKPIPE_BASE_URL", "").strip()
    if override:
        return override.rstrip("/")
    return _DEFAULT_BASE_URL


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

    def fetch_models(
        self,
        *,
        api_key: str | None = None,
        timeout: float = 8.0,
    ) -> list[str] | None:
        upstream_models = super().fetch_models(api_key=api_key, timeout=timeout)
        if upstream_models is None:
            return None

        mapped = {_to_cloakpipe_model(model_id) for model_id in upstream_models}
        return sorted(model_id for model_id in mapped if model_id)

    def build_api_kwargs_extras(
        self,
        *,
        reasoning_config: dict | None = None,
        model: str | None = None,
        **context: Any,
    ) -> tuple[dict[str, Any], dict[str, Any]]:
        _ = reasoning_config, context
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
