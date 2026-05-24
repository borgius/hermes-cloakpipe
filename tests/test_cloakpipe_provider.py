from __future__ import annotations

import importlib.util
import os
import sys
import types
import unittest
from pathlib import Path


PLUGIN_PATH = Path(__file__).resolve().parents[1] / "plugins" / "model-providers" / "cloakpipe" / "__init__.py"


class _StubProviderProfile:
    _fetch_return: list[str] | None = None

    def __init__(self, **kwargs):
        for key, value in kwargs.items():
            setattr(self, key, value)

    def fetch_models(self, *, api_key: str | None = None, timeout: float = 8.0):
        _ = api_key, timeout
        return list(self._fetch_return) if self._fetch_return is not None else None


class CloakPipeProviderTests(unittest.TestCase):
    def _load_plugin(self, fetch_return: list[str] | None):
        register_calls = []
        _StubProviderProfile._fetch_return = fetch_return

        providers_module = types.ModuleType("providers")

        def _register_provider(profile):
            register_calls.append(profile)

        providers_module.register_provider = _register_provider

        providers_base_module = types.ModuleType("providers.base")
        providers_base_module.ProviderProfile = _StubProviderProfile

        old_modules = {
            "providers": sys.modules.get("providers"),
            "providers.base": sys.modules.get("providers.base"),
            "test_cloakpipe_provider_module": sys.modules.get("test_cloakpipe_provider_module"),
        }

        try:
            sys.modules["providers"] = providers_module
            sys.modules["providers.base"] = providers_base_module

            spec = importlib.util.spec_from_file_location("test_cloakpipe_provider_module", PLUGIN_PATH)
            module = importlib.util.module_from_spec(spec)
            sys.modules["test_cloakpipe_provider_module"] = module
            assert spec and spec.loader
            spec.loader.exec_module(module)
            return module, register_calls
        finally:
            for key, value in old_modules.items():
                if value is None:
                    sys.modules.pop(key, None)
                else:
                    sys.modules[key] = value

    def test_registers_cloakpipe_profile(self):
        old_base = os.environ.get("CLOAKPIPE_BASE_URL")
        os.environ["CLOAKPIPE_BASE_URL"] = "http://localhost:9090/v1/"
        try:
            _, register_calls = self._load_plugin(fetch_return=[])
        finally:
            if old_base is None:
                os.environ.pop("CLOAKPIPE_BASE_URL", None)
            else:
                os.environ["CLOAKPIPE_BASE_URL"] = old_base

        self.assertEqual(1, len(register_calls))
        profile = register_calls[0]
        self.assertEqual("cloakpipe", profile.name)
        self.assertEqual("http://localhost:9090/v1", profile.base_url)

    def test_fetch_models_transforms_provider_model_ids(self):
        _, register_calls = self._load_plugin(
            fetch_return=[
                "openai/gpt-4o",
                "anthropic/claude-3-7-sonnet",
                "cloakpipe/google-gemini-2.5-pro",
            ]
        )

        profile = register_calls[0]
        models = profile.fetch_models(api_key="dummy")

        self.assertEqual(
            [
                "cloakpipe/anthropic-claude-3-7-sonnet",
                "cloakpipe/google-gemini-2.5-pro",
                "cloakpipe/openai-gpt-4o",
            ],
            models,
        )

    def test_build_api_kwargs_extras_maps_back_to_upstream_model(self):
        _, register_calls = self._load_plugin(fetch_return=[])
        profile = register_calls[0]

        _, kwargs = profile.build_api_kwargs_extras(model="cloakpipe/openai-gpt-4o")
        self.assertEqual({"model": "openai/gpt-4o"}, kwargs)


if __name__ == "__main__":
    unittest.main()
