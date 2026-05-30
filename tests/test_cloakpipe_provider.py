from __future__ import annotations

import asyncio
import importlib.util
import os
import subprocess
import sys
import tempfile
import types
import unittest
from dataclasses import dataclass
from pathlib import Path
from unittest import mock


PLUGIN_PATH = Path(__file__).resolve().parents[1] / "plugins" / "model-providers" / "cloakpipe" / "__init__.py"


class _StubProviderProfile:
    _fetch_return: list[str] | None = None

    def __init__(self, **kwargs):
        for key, value in kwargs.items():
            setattr(self, key, value)

    def fetch_models(self, *, api_key: str | None = None, timeout: float = 8.0):
        _ = api_key, timeout
        return list(self._fetch_return) if self._fetch_return is not None else None


@dataclass
class _StubResolvedProviderDef:
    id: str
    name: str
    transport: str
    api_key_env_vars: tuple[str, ...]
    base_url: str = ""
    base_url_env_var: str = ""
    is_aggregator: bool = False
    auth_type: str = "api_key"
    doc: str = ""
    source: str = ""


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
        old_provider_base = os.environ.get("CLOAKPIPE_HERMES_BASE_URL")
        os.environ["CLOAKPIPE_BASE_URL"] = "http://localhost:9090/v1/"
        os.environ["CLOAKPIPE_HERMES_BASE_URL"] = "http://localhost:9091/v1/"
        try:
            _, register_calls = self._load_plugin(fetch_return=[])
        finally:
            if old_base is None:
                os.environ.pop("CLOAKPIPE_BASE_URL", None)
            else:
                os.environ["CLOAKPIPE_BASE_URL"] = old_base
            if old_provider_base is None:
                os.environ.pop("CLOAKPIPE_HERMES_BASE_URL", None)
            else:
                os.environ["CLOAKPIPE_HERMES_BASE_URL"] = old_provider_base

        self.assertEqual(1, len(register_calls))
        profile = register_calls[0]
        self.assertEqual("cloakpipe", profile.name)
        self.assertEqual("http://localhost:9091/v1", profile.base_url)

    def test_import_has_no_runtime_side_effects(self):
        with (
            mock.patch("urllib.request.urlopen", side_effect=AssertionError("unexpected network probe during import")),
            mock.patch("subprocess.run", side_effect=AssertionError("unexpected cargo install during import")),
            mock.patch("subprocess.Popen", side_effect=AssertionError("unexpected process start during import")),
            mock.patch("shutil.which", side_effect=AssertionError("unexpected binary lookup during import")),
        ):
            _, register_calls = self._load_plugin(fetch_return=[])

        self.assertEqual(1, len(register_calls))

    def test_emit_debug_prints_when_enabled(self):
        module, _ = self._load_plugin(fetch_return=[])

        with (
            mock.patch.dict(os.environ, {"CLOAKPIPE_DEBUG": "1"}, clear=False),
            mock.patch.object(module.logger, "info") as logger_info,
            mock.patch("builtins.print") as print_mock,
        ):
            module._emit_debug("health check ran")

        logger_info.assert_called_once_with("CloakPipe debug: %s", "health check ran")
        print_mock.assert_called_once_with("[CloakPipe debug] health check ran", flush=True)

    def test_probe_health_emits_debug_messages(self):
        module, _ = self._load_plugin(fetch_return=[])

        class _Response:
            status = 200

            def __enter__(self):
                return self

            def __exit__(self, *_args):
                return False

            def read(self):
                return b'{"status":"ok"}'

        with (
            mock.patch.object(module, "_emit_debug") as emit_debug,
            mock.patch("urllib.request.urlopen", return_value=_Response()),
        ):
            healthy, detail = module._probe_health("http://127.0.0.1:3100/v1", timeout=1.0)

        self.assertTrue(healthy)
        self.assertEqual("responded with HTTP 200", detail)
        debug_messages = [call.args[0] for call in emit_debug.call_args_list]
        self.assertTrue(any(message.startswith("Health check: http://127.0.0.1:3100/health") for message in debug_messages))
        self.assertTrue(any("Health check OK: http://127.0.0.1:3100/health -> HTTP 200" in message for message in debug_messages))

    def test_shutdown_hooks_register_once_and_cleanup_managed_runtime(self):
        module, _ = self._load_plugin(fetch_return=[])
        cloakpipe_process = mock.Mock(pid=111)
        cloakpipe_process.poll.return_value = None
        cloakpipe_process.wait.return_value = 0
        cloakpipe_process.returncode = 0

        ner_process = mock.Mock(pid=222)
        ner_process.poll.return_value = None
        ner_process.wait.return_value = 0
        ner_process.returncode = 0

        server = mock.Mock()
        thread = mock.Mock()
        thread.is_alive.return_value = False

        with (
            mock.patch.object(module, "_shutdown_hooks_registered", False),
            mock.patch.object(module.atexit, "register") as register_atexit,
            mock.patch.object(module, "_managed_process", cloakpipe_process),
            mock.patch.object(module, "_managed_ner_process", ner_process),
            mock.patch.object(module, "_virtual_server", server),
            mock.patch.object(module, "_virtual_server_thread", thread),
            mock.patch.object(module, "_emit_debug"),
        ):
            module._ensure_shutdown_hooks_registered()
            module._ensure_shutdown_hooks_registered()
            module._shutdown_managed_runtime()

            register_atexit.assert_called_once_with(module._shutdown_managed_runtime)
            cloakpipe_process.terminate.assert_called_once()
            cloakpipe_process.wait.assert_called_once_with(timeout=5.0)
            ner_process.terminate.assert_called_once()
            ner_process.wait.assert_called_once_with(timeout=5.0)
            server.shutdown.assert_called_once()
            server.server_close.assert_called_once()
            self.assertIsNone(module._managed_process)
            self.assertIsNone(module._managed_ner_process)
            self.assertIsNone(module._virtual_server)
            self.assertIsNone(module._virtual_server_thread)

    def test_derive_health_url_strips_v1_suffix(self):
        module, _ = self._load_plugin(fetch_return=[])

        self.assertEqual("http://127.0.0.1:3100/health", module._derive_health_url("http://127.0.0.1:3100/v1"))
        self.assertEqual(
            "http://127.0.0.1:3100/proxy/health",
            module._derive_health_url("http://127.0.0.1:3100/proxy/v1/anthropic"),
        )

    def test_fetch_models_returns_single_stable_model_without_starting_runtime(self):
        module, register_calls = self._load_plugin(fetch_return=[])

        profile = register_calls[0]
        with (
            mock.patch.object(module, "_ensure_cloakpipe_ready", side_effect=AssertionError("runtime should not start")),
            mock.patch.object(module, "_ensure_virtual_provider_ready", side_effect=AssertionError("wrapper should not start")),
        ):
            models = profile.fetch_models(api_key="dummy")

        self.assertEqual(["cloakpipe/latest"], models)

    def test_fetch_models_ignores_legacy_env_models_and_active_catalog(self):
        module, register_calls = self._load_plugin(fetch_return=[])
        profile = register_calls[0]

        hermes_module = types.ModuleType("hermes_cli")
        model_switch_module = types.ModuleType("hermes_cli.model_switch")
        model_switch_module.list_picker_providers = mock.Mock(side_effect=AssertionError("catalog should not be mirrored"))
        hermes_module.model_switch = model_switch_module

        with (
            mock.patch.dict(os.environ, {"CLOAKPIPE_MODELS": "openai/gpt-4o"}, clear=False),
            mock.patch.dict(
                sys.modules,
                {
                    "hermes_cli": hermes_module,
                    "hermes_cli.model_switch": model_switch_module,
                },
                clear=False,
            ),
        ):
            models = profile.fetch_models(api_key="dummy")

        self.assertEqual(["cloakpipe/latest"], models)
        model_switch_module.list_picker_providers.assert_not_called()

    def test_picker_patch_injects_cloakpipe_row(self):
        module, _ = self._load_plugin(fetch_return=[])

        hermes_module = types.ModuleType("hermes_cli")
        model_switch_module = types.ModuleType("hermes_cli.model_switch")

        def _list_authenticated_providers(**_kwargs):
            return [
                {
                    "slug": "anthropic",
                    "name": "Anthropic",
                    "is_current": False,
                    "is_user_defined": False,
                    "models": ["claude-sonnet-4-6"],
                    "total_models": 1,
                    "source": "hermes",
                },
                {
                    "slug": "cloakpipe",
                    "name": "CloakPipe",
                    "is_current": False,
                    "is_user_defined": False,
                    "models": [],
                    "total_models": 0,
                    "source": "canonical",
                },
            ]

        def _list_picker_providers(**_kwargs):
            return [
                {
                    "slug": "anthropic",
                    "name": "Anthropic",
                    "is_current": False,
                    "is_user_defined": False,
                    "models": ["claude-sonnet-4-6"],
                    "total_models": 1,
                    "source": "hermes",
                },
            ]

        model_switch_module.list_authenticated_providers = _list_authenticated_providers
        model_switch_module.list_picker_providers = _list_picker_providers
        hermes_module.model_switch = model_switch_module

        with tempfile.TemporaryDirectory() as temp_dir:
            with (
                mock.patch.object(module, "_managed_runtime_dir", return_value=Path(temp_dir)),
                mock.patch.object(module, "_preflight_cloakpipe_activation", return_value=None),
                mock.patch.dict(
                    sys.modules,
                    {
                        "hermes_cli": hermes_module,
                        "hermes_cli.model_switch": model_switch_module,
                    },
                    clear=False,
                ),
            ):
                module._remember_upstream_selection("anthropic", "claude-sonnet-4-6", source="test")
                module._patch_hermes_model_picker()

                authenticated_rows = model_switch_module.list_authenticated_providers(max_models=4)
                picker_rows = model_switch_module.list_picker_providers(max_models=4)

        auth_cloakpipe = next(row for row in authenticated_rows if row["slug"] == "cloakpipe")
        picker_cloakpipe = next(row for row in picker_rows if row["slug"] == "cloakpipe")

        self.assertEqual(["cloakpipe/latest"], auth_cloakpipe["models"])
        self.assertEqual(1, auth_cloakpipe["total_models"])
        self.assertEqual("CloakPipe: anthropic/claude-sonnet-4-6", auth_cloakpipe["name"])
        self.assertEqual(["cloakpipe/latest"], picker_cloakpipe["models"])
        self.assertEqual("CloakPipe: anthropic/claude-sonnet-4-6", picker_cloakpipe["name"])

    def test_picker_patch_uses_env_fallback_label_when_no_state(self):
        module, _ = self._load_plugin(fetch_return=[])

        hermes_module = types.ModuleType("hermes_cli")
        model_switch_module = types.ModuleType("hermes_cli.model_switch")

        def _list_authenticated_providers(**_kwargs):
            return [
                {
                    "slug": "openrouter",
                    "name": "OpenRouter",
                    "is_current": False,
                    "is_user_defined": False,
                    "models": ["moonshotai/kimi-k2.6"],
                    "total_models": 1,
                    "source": "built-in",
                },
            ]

        model_switch_module.list_authenticated_providers = _list_authenticated_providers
        model_switch_module.list_picker_providers = _list_authenticated_providers
        hermes_module.model_switch = model_switch_module

        with tempfile.TemporaryDirectory() as temp_dir:
            with (
                mock.patch.object(module, "_managed_runtime_dir", return_value=Path(temp_dir)),
                mock.patch.dict(
                    os.environ,
                    {
                        "CLOAKPIPE_UPSTREAM_PROVIDER": "openrouter",
                        "CLOAKPIPE_UPSTREAM_MODEL": "moonshotai/kimi-k2.6",
                    },
                    clear=False,
                ),
                mock.patch.dict(
                    sys.modules,
                    {
                        "hermes_cli": hermes_module,
                        "hermes_cli.model_switch": model_switch_module,
                    },
                    clear=False,
                ),
            ):
                module._patch_hermes_model_picker()
                authenticated_rows = model_switch_module.list_authenticated_providers(max_models=50)

        cloakpipe_row = next(row for row in authenticated_rows if row["slug"] == "cloakpipe")
        self.assertEqual(1, cloakpipe_row["total_models"])
        self.assertEqual(["cloakpipe/latest"], cloakpipe_row["models"])
        self.assertEqual("CloakPipe: openrouter/moonshotai/kimi-k2.6", cloakpipe_row["name"])

    def test_picker_patch_surfaces_recorded_startup_warning(self):
        module, _ = self._load_plugin(fetch_return=[])

        hermes_module = types.ModuleType("hermes_cli")
        model_switch_module = types.ModuleType("hermes_cli.model_switch")

        def _list_authenticated_providers(**_kwargs):
            return [
                {
                    "slug": "anthropic",
                    "name": "Anthropic",
                    "is_current": False,
                    "is_user_defined": False,
                    "models": ["claude-sonnet-4-6"],
                    "total_models": 1,
                    "source": "hermes",
                },
            ]

        model_switch_module.list_authenticated_providers = _list_authenticated_providers
        model_switch_module.list_picker_providers = _list_authenticated_providers
        hermes_module.model_switch = model_switch_module

        with tempfile.TemporaryDirectory() as temp_dir:
            with (
                mock.patch.object(module, "_managed_runtime_dir", return_value=Path(temp_dir)),
                mock.patch.object(module, "_preflight_cloakpipe_activation", return_value=None),
                mock.patch.dict(
                    sys.modules,
                    {
                        "hermes_cli": hermes_module,
                        "hermes_cli.model_switch": model_switch_module,
                    },
                    clear=False,
                ),
            ):
                module._remember_startup_warning(
                    "CloakPipe is not ready at http://127.0.0.1:3100/v1.\n"
                    "Health check http://127.0.0.1:3100/health: connection refused"
                )
                module._patch_hermes_model_picker()
                authenticated_rows = model_switch_module.list_authenticated_providers(max_models=50)

        cloakpipe_row = next(row for row in authenticated_rows if row["slug"] == "cloakpipe")
        self.assertEqual("CloakPipe is not ready at http://127.0.0.1:3100/v1.", cloakpipe_row["warning"])

    def test_switch_patch_routes_cloakpipe_selection_to_stable_model_and_remembers_current_upstream(self):
        module, _ = self._load_plugin(fetch_return=[])

        hermes_module = types.ModuleType("hermes_cli")
        model_switch_module = types.ModuleType("hermes_cli.model_switch")
        captured = {}

        def _switch_model(**kwargs):
            captured.update(kwargs)
            return kwargs

        model_switch_module.switch_model = _switch_model
        hermes_module.model_switch = model_switch_module

        with tempfile.TemporaryDirectory() as temp_dir:
            with (
                mock.patch.object(module, "_managed_runtime_dir", return_value=Path(temp_dir)),
                mock.patch.dict(
                    sys.modules,
                    {
                        "hermes_cli": hermes_module,
                        "hermes_cli.model_switch": model_switch_module,
                    },
                    clear=False,
                ),
            ):
                module._patch_hermes_model_switch()
                model_switch_module.switch_model(
                    raw_input="cloakpipe/openrouter-moonshotai/kimi-k2.6",
                    current_provider="openrouter",
                    current_model="anthropic/claude-opus-4.7",
                )
                saved = module._read_saved_upstream_selection()

        self.assertEqual("cloakpipe", captured["explicit_provider"])
        self.assertEqual("cloakpipe/latest", captured["raw_input"])
        self.assertEqual("openrouter", saved["provider"])
        self.assertEqual("anthropic/claude-opus-4.7", saved["model"])

    def test_switch_patch_normalizes_explicit_cloakpipe_provider(self):
        module, _ = self._load_plugin(fetch_return=[])

        hermes_module = types.ModuleType("hermes_cli")
        model_switch_module = types.ModuleType("hermes_cli.model_switch")
        captured = {}

        def _switch_model(**kwargs):
            captured.update(kwargs)
            return kwargs

        model_switch_module.switch_model = _switch_model
        hermes_module.model_switch = model_switch_module

        with tempfile.TemporaryDirectory() as temp_dir:
            with (
                mock.patch.object(module, "_managed_runtime_dir", return_value=Path(temp_dir)),
                mock.patch.dict(
                    sys.modules,
                    {
                        "hermes_cli": hermes_module,
                        "hermes_cli.model_switch": model_switch_module,
                    },
                    clear=False,
                ),
            ):
                module._patch_hermes_model_switch()
                model_switch_module.switch_model(
                    raw_input="moonshotai/kimi-k2.6",
                    current_provider="openrouter",
                    current_model="anthropic/claude-opus-4.7",
                    explicit_provider="cloakpipe",
                )
                saved = module._read_saved_upstream_selection()

        self.assertEqual("cloakpipe", captured["explicit_provider"])
        self.assertEqual("cloakpipe/latest", captured["raw_input"])
        self.assertEqual("openrouter", saved["provider"])
        self.assertEqual("anthropic/claude-opus-4.7", saved["model"])

    def test_switch_patch_remembers_current_upstream_context_length(self):
        module, _ = self._load_plugin(fetch_return=[])

        hermes_module = types.ModuleType("hermes_cli")
        model_switch_module = types.ModuleType("hermes_cli.model_switch")
        captured = {}

        def _switch_model(**kwargs):
            captured.update(kwargs)
            return kwargs

        model_switch_module.switch_model = _switch_model
        hermes_module.model_switch = model_switch_module

        agent_module = types.ModuleType("agent")
        model_metadata_module = types.ModuleType("agent.model_metadata")
        context_calls = []

        def _get_model_context_length(
            model,
            base_url="",
            api_key="",
            config_context_length=None,
            provider="",
            custom_providers=None,
        ):
            context_calls.append(
                {
                    "model": model,
                    "base_url": base_url,
                    "api_key": api_key,
                    "config_context_length": config_context_length,
                    "provider": provider,
                    "custom_providers": custom_providers,
                }
            )
            return 1_048_576

        model_metadata_module.get_model_context_length = _get_model_context_length
        agent_module.model_metadata = model_metadata_module

        with tempfile.TemporaryDirectory() as temp_dir:
            with (
                mock.patch.object(module, "_managed_runtime_dir", return_value=Path(temp_dir)),
                mock.patch.object(module, "_preflight_cloakpipe_activation", return_value=None),
                mock.patch.dict(
                    sys.modules,
                    {
                        "agent": agent_module,
                        "agent.model_metadata": model_metadata_module,
                        "hermes_cli": hermes_module,
                        "hermes_cli.model_switch": model_switch_module,
                    },
                    clear=False,
                ),
            ):
                module._patch_hermes_model_switch()
                model_switch_module.switch_model(
                    raw_input="cloakpipe/latest",
                    current_provider="openrouter",
                    current_model="moonshotai/kimi-k2.6",
                    current_base_url="https://openrouter.ai/api/v1",
                    current_api_key="test-openrouter-key",
                )
                saved = module._read_saved_upstream_selection()

        self.assertEqual("cloakpipe", captured["explicit_provider"])
        self.assertEqual("cloakpipe/latest", captured["raw_input"])
        self.assertEqual("openrouter", saved["provider"])
        self.assertEqual("moonshotai/kimi-k2.6", saved["model"])
        self.assertEqual(1_048_576, saved["context_length"])
        self.assertEqual(
            {
                "model": "moonshotai/kimi-k2.6",
                "base_url": "https://openrouter.ai/api/v1",
                "api_key": "test-openrouter-key",
                "config_context_length": None,
                "provider": "openrouter",
                "custom_providers": None,
            },
            context_calls[0],
        )

    def test_switch_patch_preflights_runtime_and_emits_warning_when_activation_fails(self):
        module, _ = self._load_plugin(fetch_return=[])

        hermes_module = types.ModuleType("hermes_cli")
        model_switch_module = types.ModuleType("hermes_cli.model_switch")

        def _switch_model(**kwargs):
            return kwargs

        model_switch_module.switch_model = _switch_model
        hermes_module.model_switch = model_switch_module

        with tempfile.TemporaryDirectory() as temp_dir:
            with (
                mock.patch.object(module, "_managed_runtime_dir", return_value=Path(temp_dir)),
                mock.patch.object(
                    module,
                    "_preflight_cloakpipe_activation",
                    return_value="CloakPipe is not ready at http://127.0.0.1:3100/v1.",
                ) as preflight,
                mock.patch.object(module, "_emit_startup_warning") as emit_warning,
                mock.patch.dict(
                    sys.modules,
                    {
                        "hermes_cli": hermes_module,
                        "hermes_cli.model_switch": model_switch_module,
                    },
                    clear=False,
                ),
            ):
                module._patch_hermes_model_switch()
                model_switch_module.switch_model(
                    raw_input="cloakpipe/latest",
                    current_provider="openrouter",
                    current_model="moonshotai/kimi-k2.6",
                )

        preflight.assert_called_once_with(requested_model="cloakpipe/latest", timeout=8.0)
        emit_warning.assert_called_once_with("CloakPipe is not ready at http://127.0.0.1:3100/v1.")

    def test_switch_patch_surfaces_activation_warning_in_result_message(self):
        module, _ = self._load_plugin(fetch_return=[])

        hermes_module = types.ModuleType("hermes_cli")
        model_switch_module = types.ModuleType("hermes_cli.model_switch")

        result = types.SimpleNamespace(success=True, warning_message="")

        def _switch_model(**_kwargs):
            return result

        model_switch_module.switch_model = _switch_model
        hermes_module.model_switch = model_switch_module

        with tempfile.TemporaryDirectory() as temp_dir:
            with (
                mock.patch.object(module, "_managed_runtime_dir", return_value=Path(temp_dir)),
                mock.patch.object(
                    module,
                    "_preflight_cloakpipe_activation",
                    return_value="CloakPipe is not ready at http://127.0.0.1:3100/v1.\nHealth check failed",
                ),
                mock.patch.object(module, "_emit_startup_warning"),
                mock.patch.dict(
                    sys.modules,
                    {
                        "hermes_cli": hermes_module,
                        "hermes_cli.model_switch": model_switch_module,
                    },
                    clear=False,
                ),
            ):
                module._patch_hermes_model_switch()
                returned = model_switch_module.switch_model(
                    raw_input="cloakpipe/latest",
                    current_provider="openrouter",
                    current_model="moonshotai/kimi-k2.6",
                )

        self.assertIs(returned, result)
        self.assertIn("CloakPipe is not ready at http://127.0.0.1:3100/v1.", returned.warning_message)

    def test_switch_patch_surfaces_visible_debug_message_when_enabled(self):
        module, _ = self._load_plugin(fetch_return=[])

        hermes_module = types.ModuleType("hermes_cli")
        model_switch_module = types.ModuleType("hermes_cli.model_switch")

        result = types.SimpleNamespace(success=True, warning_message="")

        def _switch_model(**_kwargs):
            return result

        model_switch_module.switch_model = _switch_model
        hermes_module.model_switch = model_switch_module

        with tempfile.TemporaryDirectory() as temp_dir:
            with (
                mock.patch.object(module, "_managed_runtime_dir", return_value=Path(temp_dir)),
                mock.patch.object(module, "_preflight_cloakpipe_activation", return_value=None),
                mock.patch.dict(os.environ, {"CLOAKPIPE_DEBUG": "1"}, clear=False),
                mock.patch.dict(
                    sys.modules,
                    {
                        "hermes_cli": hermes_module,
                        "hermes_cli.model_switch": model_switch_module,
                    },
                    clear=False,
                ),
            ):
                module._patch_hermes_model_switch()
                returned = model_switch_module.switch_model(
                    raw_input="cloakpipe/latest",
                    current_provider="openrouter",
                    current_model="moonshotai/kimi-k2.6",
                )

        self.assertIs(returned, result)
        self.assertIn("CloakPipe debug: activation preflight ran successfully.", returned.warning_message)

    def test_context_length_patch_uses_latest_upstream_model(self):
        module, _ = self._load_plugin(fetch_return=[])

        agent_module = types.ModuleType("agent")
        model_metadata_module = types.ModuleType("agent.model_metadata")
        context_calls = []

        def _get_model_context_length(
            model,
            base_url="",
            api_key="",
            config_context_length=None,
            provider="",
            custom_providers=None,
        ):
            context_calls.append(
                {
                    "model": model,
                    "base_url": base_url,
                    "api_key": api_key,
                    "config_context_length": config_context_length,
                    "provider": provider,
                    "custom_providers": custom_providers,
                }
            )
            if model == "moonshotai/kimi-k2.6":
                return 1_048_576
            return 262_144

        model_metadata_module.get_model_context_length = _get_model_context_length
        agent_module.model_metadata = model_metadata_module

        hermes_module = types.ModuleType("hermes_cli")
        runtime_provider_module = types.ModuleType("hermes_cli.runtime_provider")
        resolve_calls = []

        def _resolve_runtime_provider(*, requested=None, explicit_api_key=None, explicit_base_url=None, target_model=None):
            resolve_calls.append(
                {
                    "requested": requested,
                    "explicit_api_key": explicit_api_key,
                    "explicit_base_url": explicit_base_url,
                    "target_model": target_model,
                }
            )
            return {
                "provider": "openrouter",
                "base_url": "https://openrouter.ai/api/v1",
                "api_key": "resolved-openrouter-key",
                "api_mode": "chat_completions",
            }

        runtime_provider_module.resolve_runtime_provider = _resolve_runtime_provider
        hermes_module.runtime_provider = runtime_provider_module

        with tempfile.TemporaryDirectory() as temp_dir:
            with (
                mock.patch.object(module, "_managed_runtime_dir", return_value=Path(temp_dir)),
                mock.patch.dict(
                    sys.modules,
                    {
                        "agent": agent_module,
                        "agent.model_metadata": model_metadata_module,
                        "hermes_cli": hermes_module,
                        "hermes_cli.runtime_provider": runtime_provider_module,
                    },
                    clear=False,
                ),
            ):
                module._remember_upstream_selection("openrouter", "moonshotai/kimi-k2.6", source="test")
                module._patch_hermes_context_length_resolution()
                context_length = model_metadata_module.get_model_context_length(
                    "cloakpipe/latest",
                    provider="cloakpipe",
                    base_url="http://127.0.0.1:3199/v1",
                )

        self.assertEqual(1_048_576, context_length)
        self.assertEqual(
            {
                "requested": "openrouter",
                "explicit_api_key": None,
                "explicit_base_url": None,
                "target_model": "moonshotai/kimi-k2.6",
            },
            resolve_calls[0],
        )
        self.assertEqual(
            {
                "model": "moonshotai/kimi-k2.6",
                "base_url": "https://openrouter.ai/api/v1",
                "api_key": "resolved-openrouter-key",
                "config_context_length": None,
                "provider": "openrouter",
                "custom_providers": None,
            },
            context_calls[0],
        )

    def test_cloakpipe_model_card_reports_saved_context_length(self):
        module, _ = self._load_plugin(fetch_return=[])

        with tempfile.TemporaryDirectory() as temp_dir:
            with mock.patch.object(module, "_managed_runtime_dir", return_value=Path(temp_dir)):
                module._remember_upstream_selection(
                    "openrouter",
                    "moonshotai/kimi-k2.6",
                    source="test",
                    context_length=1_048_576,
                )
                model_card = module._build_cloakpipe_model_card()

        self.assertEqual("cloakpipe/latest", model_card["id"])
        self.assertEqual(1_048_576, model_card["context_length"])
        self.assertEqual(1_048_576, model_card["context_window"])

    def test_format_cloakpipe_switch_label_includes_latest_upstream_model(self):
        module, _ = self._load_plugin(fetch_return=[])

        with tempfile.TemporaryDirectory() as temp_dir:
            with mock.patch.object(module, "_managed_runtime_dir", return_value=Path(temp_dir)):
                module._remember_upstream_selection(
                    "openrouter",
                    "moonshotai/kimi-k2.6",
                    source="test",
                )

                label = module._format_cloakpipe_switch_label("cloakpipe/latest")

        self.assertEqual("cloakpipe/latest (moonshotai/kimi-k2.6)", label)

    def test_cli_switch_display_patch_rewrites_cloakpipe_label(self):
        module, _ = self._load_plugin(fetch_return=[])
        printed = []

        cli_module = types.ModuleType("cli")

        def _cprint(text):
            printed.append(text)

        cli_module._cprint = _cprint

        with tempfile.TemporaryDirectory() as temp_dir:
            with (
                mock.patch.object(module, "_managed_runtime_dir", return_value=Path(temp_dir)),
                mock.patch.dict(sys.modules, {"cli": cli_module}, clear=False),
            ):
                module._remember_upstream_selection(
                    "openrouter",
                    "moonshotai/kimi-k2.6",
                    source="test",
                )
                module._patch_cli_model_switch_display()
                cli_module._cprint("  ✓ Model switched: cloakpipe/latest")
                cli_module._cprint("    Provider: CloakPipe")

        self.assertEqual(
            "  ✓ Model switched: cloakpipe/latest (moonshotai/kimi-k2.6)",
            printed[0],
        )
        self.assertEqual("    Provider: CloakPipe", printed[1])

    def test_gateway_switch_display_patch_rewrites_cloakpipe_label(self):
        module, _ = self._load_plugin(fetch_return=[])

        gateway_package = types.ModuleType("gateway")
        gateway_run_module = types.ModuleType("gateway.run")

        class _GatewayRunner:
            async def _handle_model_command(self, _event):
                return "✓ Model switched: cloakpipe/latest\nProvider: CloakPipe"

        gateway_run_module.GatewayRunner = _GatewayRunner
        gateway_package.run = gateway_run_module

        with tempfile.TemporaryDirectory() as temp_dir:
            with (
                mock.patch.object(module, "_managed_runtime_dir", return_value=Path(temp_dir)),
                mock.patch.dict(
                    sys.modules,
                    {
                        "gateway": gateway_package,
                        "gateway.run": gateway_run_module,
                    },
                    clear=False,
                ),
            ):
                module._remember_upstream_selection(
                    "openrouter",
                    "moonshotai/kimi-k2.6",
                    source="test",
                )
                module._patch_gateway_model_switch_display()
                output = asyncio.run(gateway_run_module.GatewayRunner()._handle_model_command(object()))

        self.assertEqual(
            "✓ Model switched: cloakpipe/latest (moonshotai/kimi-k2.6)\nProvider: CloakPipe",
            output,
        )

    def test_model_alias_patch_adds_cloakpipe_to_slash_model_completions(self):
        hermes_module = types.ModuleType("hermes_cli")
        model_switch_module = types.ModuleType("hermes_cli.model_switch")
        commands_module = types.ModuleType("hermes_cli.commands")

        @dataclass
        class _DirectAlias:
            model: str
            provider: str
            base_url: str

        class _Completion:
            def __init__(self, text, start_position, display, display_meta):
                self.text = text
                self.start_position = start_position
                self.display_text = display
                self.display_meta_text = display_meta

        def _ensure_direct_aliases():
            if not model_switch_module.DIRECT_ALIASES:
                model_switch_module.DIRECT_ALIASES.update(model_switch_module._BUILTIN_DIRECT_ALIASES)

        class _SlashCommandCompleter:
            def _model_completions(self, sub_text, sub_lower):
                from hermes_cli.model_switch import DIRECT_ALIASES, MODEL_ALIASES, _ensure_direct_aliases

                _ensure_direct_aliases()
                seen = set()
                for name, direct_alias in DIRECT_ALIASES.items():
                    if name.startswith(sub_lower) and name != sub_lower:
                        seen.add(name)
                        yield _Completion(
                            name,
                            start_position=-len(sub_text),
                            display=name,
                            display_meta=f"{direct_alias.model} ({direct_alias.provider})",
                        )
                for name in sorted(MODEL_ALIASES.keys()):
                    if name in seen:
                        continue
                    if name.startswith(sub_lower) and name != sub_lower:
                        yield _Completion(
                            name,
                            start_position=-len(sub_text),
                            display=name,
                            display_meta="built-in alias",
                        )

        model_switch_module.DirectAlias = _DirectAlias
        model_switch_module._BUILTIN_DIRECT_ALIASES = {}
        model_switch_module.DIRECT_ALIASES = {}
        model_switch_module.MODEL_ALIASES = {"gemini": object()}
        model_switch_module._ensure_direct_aliases = _ensure_direct_aliases
        commands_module.SlashCommandCompleter = _SlashCommandCompleter
        hermes_module.model_switch = model_switch_module
        hermes_module.commands = commands_module

        with mock.patch.dict(
            sys.modules,
            {
                "hermes_cli": hermes_module,
                "hermes_cli.model_switch": model_switch_module,
                "hermes_cli.commands": commands_module,
            },
            clear=False,
        ):
            self._load_plugin(fetch_return=[])
            completions = list(commands_module.SlashCommandCompleter()._model_completions("", ""))

        cloakpipe_completion = next((completion for completion in completions if completion.text == "cloakpipe"), None)
        self.assertIsNotNone(cloakpipe_completion)
        self.assertEqual("cloakpipe/latest (cloakpipe)", cloakpipe_completion.display_meta_text)

    def test_provider_resolution_patch_bridges_plugin_provider(self):
        module, _ = self._load_plugin(fetch_return=[])

        providers_module = types.ModuleType("providers")

        def _get_provider_profile(name):
            if name == "cloakpipe":
                return types.SimpleNamespace(
                    name="cloakpipe",
                    display_name="CloakPipe",
                    env_vars=("CLOAKPIPE_API_KEY",),
                    base_url="http://127.0.0.1:3199/v1",
                    auth_type="api_key",
                    aliases=("cloak", "cp"),
                )
            return None

        providers_module.get_provider_profile = _get_provider_profile
        providers_module.list_providers = lambda: []

        hermes_module = types.ModuleType("hermes_cli")
        providers_bridge_module = types.ModuleType("hermes_cli.providers")
        providers_bridge_module.ProviderDef = _StubResolvedProviderDef
        providers_bridge_module.get_provider = lambda _name: None
        hermes_module.providers = providers_bridge_module

        with mock.patch.dict(
            sys.modules,
            {
                "providers": providers_module,
                "hermes_cli": hermes_module,
                "hermes_cli.providers": providers_bridge_module,
            },
            clear=False,
        ):
            module._patch_hermes_provider_resolution()
            resolved = providers_bridge_module.get_provider("cloakpipe")

        self.assertEqual("cloakpipe", resolved.id)
        self.assertEqual("CloakPipe", resolved.name)
        self.assertEqual(("CLOAKPIPE_API_KEY",), resolved.api_key_env_vars)

    def test_upstream_selection_state_round_trips_and_rejects_cloakpipe_recursion(self):
        module, _ = self._load_plugin(fetch_return=[])

        with tempfile.TemporaryDirectory() as temp_dir:
            with mock.patch.object(module, "_managed_runtime_dir", return_value=Path(temp_dir)):
                selection = module._remember_upstream_selection("openrouter", "moonshotai/kimi-k2.6", source="test")
                saved = module._read_saved_upstream_selection()
                rejected = module._remember_upstream_selection("cloakpipe", "cloakpipe/latest", source="test")

        self.assertEqual("openrouter", selection["provider"])
        self.assertEqual("moonshotai/kimi-k2.6", selection["model"])
        self.assertEqual(selection, saved)
        self.assertIsNone(rejected)

    def test_latest_upstream_selection_uses_env_when_state_missing(self):
        module, _ = self._load_plugin(fetch_return=[])

        with tempfile.TemporaryDirectory() as temp_dir:
            with (
                mock.patch.object(module, "_managed_runtime_dir", return_value=Path(temp_dir)),
                mock.patch.dict(
                    os.environ,
                    {
                        "CLOAKPIPE_UPSTREAM_PROVIDER": "anthropic",
                        "CLOAKPIPE_UPSTREAM_MODEL": "claude-sonnet-4-6",
                    },
                    clear=False,
                ),
            ):
                selection = module._latest_upstream_selection()

        self.assertEqual("anthropic", selection["provider"])
        self.assertEqual("claude-sonnet-4-6", selection["model"])
        self.assertEqual("env", selection["source"])

    def test_build_api_kwargs_extras_injects_upstream_metadata_for_wrapper(self):
        module, register_calls = self._load_plugin(fetch_return=[])
        profile = register_calls[0]

        with tempfile.TemporaryDirectory() as temp_dir:
            with (
                mock.patch.object(module, "_managed_runtime_dir", return_value=Path(temp_dir)),
                mock.patch.object(module, "_ensure_cloakpipe_ready") as ensure_ready,
                mock.patch.object(module, "_ensure_virtual_provider_ready") as ensure_wrapper,
            ):
                module._remember_upstream_selection("openrouter", "moonshotai/kimi-k2.6", source="test")
                extra_body, kwargs = profile.build_api_kwargs_extras(model="cloakpipe/latest")

        ensure_ready.assert_called_once()
        ensure_wrapper.assert_called_once()
        self.assertEqual("http://127.0.0.1:3100/v1", ensure_ready.call_args.args[0])
        self.assertEqual(profile.base_url, ensure_wrapper.call_args.args[0])
        self.assertEqual(8.0, ensure_ready.call_args.kwargs["timeout"])
        self.assertEqual("cloakpipe/latest", ensure_ready.call_args.kwargs["requested_model"])
        self.assertEqual({"model": "cloakpipe/latest"}, kwargs)
        self.assertEqual("openrouter", extra_body["_cloakpipe_upstream"]["provider"])
        self.assertEqual("moonshotai/kimi-k2.6", extra_body["_cloakpipe_upstream"]["model"])

    def test_build_api_kwargs_extras_requires_upstream_selection(self):
        module, register_calls = self._load_plugin(fetch_return=[])
        profile = register_calls[0]

        with tempfile.TemporaryDirectory() as temp_dir:
            with (
                mock.patch.object(module, "_managed_runtime_dir", return_value=Path(temp_dir)),
                mock.patch.dict(os.environ, {"CLOAKPIPE_UPSTREAM_PROVIDER": "", "CLOAKPIPE_UPSTREAM_MODEL": ""}, clear=False),
            ):
                with self.assertRaises(module.CloakPipeRequestError) as context:
                    profile.build_api_kwargs_extras(model="cloakpipe/latest")

        self.assertEqual("upstream_model_not_selected", context.exception.code)

    def test_build_api_kwargs_extras_records_runtime_failure_warning(self):
        module, register_calls = self._load_plugin(fetch_return=[])
        profile = register_calls[0]

        with tempfile.TemporaryDirectory() as temp_dir:
            with (
                mock.patch.object(module, "_managed_runtime_dir", return_value=Path(temp_dir)),
                mock.patch.object(
                    profile,
                    "_ensure_runtime_ready",
                    side_effect=module.CloakPipeUnavailableError(
                        "CloakPipe is not ready at http://127.0.0.1:3100/v1."
                    ),
                ),
            ):
                module._remember_upstream_selection("openrouter", "moonshotai/kimi-k2.6", source="test")
                with self.assertRaises(module.CloakPipeUnavailableError):
                    profile.build_api_kwargs_extras(model="cloakpipe/latest")

        self.assertEqual("CloakPipe is not ready at http://127.0.0.1:3100/v1.", module._startup_warning_summary())

    def test_build_api_kwargs_extras_clears_recorded_runtime_warning_after_success(self):
        module, register_calls = self._load_plugin(fetch_return=[])
        profile = register_calls[0]

        with tempfile.TemporaryDirectory() as temp_dir:
            with (
                mock.patch.object(module, "_managed_runtime_dir", return_value=Path(temp_dir)),
                mock.patch.object(profile, "_ensure_runtime_ready"),
            ):
                module._remember_startup_warning("CloakPipe is not ready at http://127.0.0.1:3100/v1.")
                module._remember_upstream_selection("openrouter", "moonshotai/kimi-k2.6", source="test")
                profile.build_api_kwargs_extras(model="cloakpipe/latest")

        self.assertIsNone(module._startup_warning_summary())

    def test_privacy_api_uses_direct_pseudonymize_and_rehydrate_endpoints(self):
        module, _ = self._load_plugin(fetch_return=[])
        calls = []

        class _Response:
            def __init__(self, payload):
                self.payload = payload

            def __enter__(self):
                return self

            def __exit__(self, *_args):
                return False

            def read(self):
                return self.payload

        def _fake_urlopen(request, timeout):
            calls.append((request.full_url, request.data, timeout))
            if request.full_url.endswith("/pseudonymize"):
                return _Response(b'{"text":"PERSON_1"}')
            if request.full_url.endswith("/rehydrate"):
                return _Response(b'{"text":"Alice"}')
            raise AssertionError(f"unexpected URL {request.full_url}")

        with mock.patch("urllib.request.urlopen", side_effect=_fake_urlopen):
            pseudonymized = module._pseudonymize_text("http://127.0.0.1:3100/v1", "Alice", timeout=1.0)
            rehydrated = module._rehydrate_text("http://127.0.0.1:3100/v1", "PERSON_1", timeout=1.0)

        self.assertEqual("PERSON_1", pseudonymized)
        self.assertEqual("Alice", rehydrated)
        self.assertEqual("http://127.0.0.1:3100/v1/pseudonymize", calls[0][0])
        self.assertEqual(b'{"text": "Alice"}', calls[0][1])
        self.assertEqual("http://127.0.0.1:3100/v1/rehydrate", calls[1][0])

    def test_privacy_transforms_do_not_call_configure(self):
        module, _ = self._load_plugin(fetch_return=[])
        calls = []

        def _fake_post_json(url, payload, *, timeout):
            calls.append(url)
            return {"text": payload["text"]}

        with mock.patch.object(module, "_post_json", side_effect=_fake_post_json):
            module._pseudonymize_chat_body(
                {"messages": [{"role": "user", "content": "Alice"}]},
                base_url="http://127.0.0.1:3100/v1",
                timeout=1.0,
            )
            module._rehydrate_chat_response(
                {"choices": [{"message": {"role": "assistant", "content": "PERSON_1"}}]},
                base_url="http://127.0.0.1:3100/v1",
                timeout=1.0,
            )

        self.assertTrue(any(url.endswith("/pseudonymize") for url in calls))
        self.assertTrue(any(url.endswith("/rehydrate") for url in calls))
        self.assertFalse(any("configure" in url for url in calls))

    def test_dispatch_pseudonymizes_routes_to_selected_provider_and_rehydrates(self):
        module, _ = self._load_plugin(fetch_return=[])
        captured = {}

        class _Completions:
            def create(self, **kwargs):
                captured.update(kwargs)
                return {
                    "id": "chatcmpl-test",
                    "object": "chat.completion",
                    "choices": [
                        {
                            "index": 0,
                            "message": {"role": "assistant", "content": "Hello PERSON_1"},
                            "finish_reason": "stop",
                        }
                    ],
                }

        fake_client = types.SimpleNamespace(chat=types.SimpleNamespace(completions=_Completions()))

        with (
            mock.patch.object(module, "_pseudonymize_text", side_effect=lambda _base, text, timeout: text.replace("Alice", "PERSON_1")),
            mock.patch.object(module, "_rehydrate_text", side_effect=lambda _base, text, timeout: text.replace("PERSON_1", "Alice")),
            mock.patch.object(module, "_resolve_upstream_client", return_value=(fake_client, "gpt-4o-mini", "openai")) as resolve_client,
        ):
            payload = module._dispatch_chat_completion(
                {
                    "model": "cloakpipe/latest",
                    "_cloakpipe_upstream": {"provider": "openai", "model": "gpt-4o-mini"},
                    "messages": [{"role": "user", "content": "Hello Alice"}],
                    "stream": True,
                    "stream_options": {"include_usage": True},
                },
                cloakpipe_base_url="http://127.0.0.1:3100/v1",
                timeout=1.0,
            )

        resolve_client.assert_called_once_with("openai", "gpt-4o-mini")
        self.assertEqual("gpt-4o-mini", captured["model"])
        self.assertEqual("Hello PERSON_1", captured["messages"][0]["content"])
        self.assertNotIn("_cloakpipe_upstream", captured)
        self.assertNotIn("stream", captured)
        self.assertNotIn("stream_options", captured)
        self.assertEqual("cloakpipe/latest", payload["model"])
        self.assertEqual("Hello Alice", payload["choices"][0]["message"]["content"])

    def test_dispatch_reports_openrouter_privacy_restriction_clearly(self):
        module, _ = self._load_plugin(fetch_return=[])

        class _Completions:
            def create(self, **_kwargs):
                raise RuntimeError(
                    "Error code: 404 - {'error': {'message': 'No endpoints available matching your guardrail restrictions and data policy. Configure: https://openrouter.ai/settings/privacy', 'code': 404}}"
                )

        fake_client = types.SimpleNamespace(chat=types.SimpleNamespace(completions=_Completions()))

        with (
            mock.patch.object(module, "_pseudonymize_chat_body", return_value={"model": "qwen/qwen3.6-plus", "messages": []}),
            mock.patch.object(module, "_resolve_upstream_client", return_value=(fake_client, "qwen/qwen3.6-plus", "openrouter")),
        ):
            with self.assertRaises(module.CloakPipeRequestError) as context:
                module._dispatch_chat_completion(
                    {
                        "model": "cloakpipe/latest",
                        "_cloakpipe_upstream": {"provider": "openrouter", "model": "qwen/qwen3.6-plus"},
                        "messages": [],
                    },
                    cloakpipe_base_url="http://127.0.0.1:3100/v1",
                    timeout=1.0,
                )

        self.assertEqual("openrouter_privacy_restricted", context.exception.code)
        self.assertIn("settings/privacy", str(context.exception))

    def test_build_api_kwargs_extras_enables_ner_from_profile(self):
        module, register_calls = self._load_plugin(fetch_return=[])
        profile = register_calls[0]

        with tempfile.TemporaryDirectory() as temp_dir:
            with (
                mock.patch.object(module, "_managed_runtime_dir", return_value=Path(temp_dir)),
                mock.patch.object(module, "_ensure_cloakpipe_ready") as ensure_ready,
                mock.patch.object(module, "_ensure_virtual_provider_ready"),
            ):
                module._remember_upstream_selection("openai", "gpt-4o", source="test")
                profile.build_api_kwargs_extras(
                    model="cloakpipe/latest",
                    profile="healthcare",
                )

        self.assertTrue(ensure_ready.call_args.kwargs["ner_settings"]["enabled"])
        self.assertEqual("distilbert_pii", ensure_ready.call_args.kwargs["ner_settings"]["backend"])
        self.assertIsNone(ensure_ready.call_args.kwargs["ner_settings"]["model_path"])

    def test_resolve_ner_settings_prefers_explicit_nested_detection_config(self):
        module, _ = self._load_plugin(fetch_return=[])

        settings = module._resolve_ner_settings(
            {
                "profile": "fintech",
                "detection": {
                    "ner": {
                        "enabled": True,
                        "backend": "distilbert_pii",
                        "model": "/tmp/distilbert-pii.onnx",
                        "confidence_threshold": 0.55,
                    }
                },
            }
        )

        self.assertEqual(
            {
                "enabled": True,
                "backend": "distilbert_pii",
                "sidecar_url": "http://127.0.0.1:9111",
                "threshold": 0.55,
                "model_path": "/tmp/distilbert-pii.onnx",
            },
            settings,
        )

    def test_resolve_ner_settings_infers_gliner_backend_from_explicit_sidecar_url(self):
        module, _ = self._load_plugin(fetch_return=[])

        settings = module._resolve_ner_settings(
            {
                "detection": {
                    "ner": {
                        "enabled": True,
                        "sidecar_url": "http://127.0.0.1:9222",
                    }
                },
            }
        )

        self.assertTrue(settings["enabled"])
        self.assertEqual("gliner_pii", settings["backend"])
        self.assertEqual("http://127.0.0.1:9222", settings["sidecar_url"])

    def test_find_cloakpipe_source_dir_discovers_adjacent_repo_checkout(self):
        module, _ = self._load_plugin(fetch_return=[])

        with tempfile.TemporaryDirectory() as temp_dir:
            base_dir = Path(temp_dir)
            hermes_repo = base_dir / "hermes-cloakpipe"
            cloakpipe_repo = base_dir / "cloakpipe"
            hermes_repo.mkdir()
            (cloakpipe_repo / "tools").mkdir(parents=True)
            (cloakpipe_repo / "tools" / "download_model.sh").write_text("#!/bin/sh\n", encoding="utf-8")

            with (
                mock.patch.dict(os.environ, {"CLOAKPIPE_SOURCE_DIR": ""}),
                mock.patch.object(module.Path, "cwd", return_value=hermes_repo),
            ):
                source_dir = module._find_cloakpipe_source_dir()

        self.assertEqual(cloakpipe_repo, source_dir)

    def test_ensure_ready_reuses_local_process_across_repeated_calls(self):
        module, _ = self._load_plugin(fetch_return=[])

        with (
            mock.patch.object(module, "_probe_health", side_effect=[(False, "connection refused"), (True, "ok")]),
            mock.patch.object(module, "_find_cloakpipe_binary", return_value=Path("/tmp/cloakpipe")),
            mock.patch.object(module, "_write_managed_config", return_value=Path("/tmp/cloakpipe.toml")),
            mock.patch.object(module, "_start_local_cloakpipe", return_value=(True, Path("/tmp/cloakpipe.log"))) as start_local,
            mock.patch.object(module, "_wait_for_health", return_value=(True, "ok")),
        ):
            module._ensure_cloakpipe_ready("http://127.0.0.1:3100/v1", timeout=1.0, requested_model="cloakpipe/latest")
            module._ensure_cloakpipe_ready("http://127.0.0.1:3100/v1", timeout=1.0, requested_model="cloakpipe/latest")

        start_local.assert_called_once()

    def test_ensure_ready_installs_with_cargo_when_binary_is_missing(self):
        module, _ = self._load_plugin(fetch_return=[])

        with (
            mock.patch.object(module, "_probe_health", return_value=(False, "connection refused")),
            mock.patch.object(module, "_find_cloakpipe_binary", return_value=None),
            mock.patch.object(module, "_find_cargo_binary", return_value=Path("/tmp/cargo")),
            mock.patch.object(module, "_install_cloakpipe_with_cargo", return_value=Path("/tmp/cloakpipe")) as install_helper,
            mock.patch.object(module, "_write_managed_config", return_value=Path("/tmp/cloakpipe.toml")),
            mock.patch.object(module, "_start_local_cloakpipe", return_value=(True, Path("/tmp/cloakpipe.log"))),
            mock.patch.object(module, "_wait_for_health", return_value=(True, "ok")),
        ):
            module._ensure_cloakpipe_ready("http://127.0.0.1:3100/v1", timeout=1.0, requested_model="cloakpipe/latest")

        install_helper.assert_called_once_with(Path("/tmp/cargo"))

    def test_ensure_ready_starts_ner_when_enabled(self):
        module, _ = self._load_plugin(fetch_return=[])

        with (
            mock.patch.object(module, "_probe_health", return_value=(False, "connection refused")),
            mock.patch.object(module, "_find_cloakpipe_binary", return_value=Path("/tmp/cloakpipe")),
            mock.patch.object(module, "_ensure_ner_ready") as ensure_ner,
            mock.patch.object(module, "_write_managed_config", return_value=Path("/tmp/cloakpipe.toml")),
            mock.patch.object(module, "_start_local_cloakpipe", return_value=(True, Path("/tmp/cloakpipe.log"))),
            mock.patch.object(module, "_wait_for_health", return_value=(True, "ok")),
        ):
            module._ensure_cloakpipe_ready(
                "http://127.0.0.1:3100/v1",
                timeout=1.0,
                requested_model="cloakpipe/latest",
                ner_settings={
                    "enabled": True,
                    "backend": "distilbert_pii",
                    "model_path": None,
                    "sidecar_url": "http://127.0.0.1:9111",
                    "threshold": 0.4,
                },
            )

        ensure_ner.assert_called_once_with(
            Path("/tmp/cloakpipe"),
            {
                "enabled": True,
                "backend": "distilbert_pii",
                "model_path": None,
                "sidecar_url": "http://127.0.0.1:9111",
                "threshold": 0.4,
            },
            timeout=1.0,
        )

    def test_ensure_ner_ready_downloads_distilbert_model_when_missing(self):
        module, _ = self._load_plugin(fetch_return=[])

        with tempfile.TemporaryDirectory() as temp_dir:
            source_dir = Path(temp_dir)
            model_path = source_dir / "models" / "distilbert-pii" / "quantized" / "model_quantized.onnx"
            ner_settings = {
                "enabled": True,
                "backend": "distilbert_pii",
                "model_path": None,
                "sidecar_url": "http://127.0.0.1:9111",
                "threshold": 0.4,
            }

            with (
                mock.patch.object(module, "_find_cloakpipe_source_dir", return_value=source_dir),
                mock.patch.object(module, "_download_ner_model_with_cloakpipe", return_value=model_path) as download_ner,
            ):
                module._ensure_ner_ready(
                    Path("/tmp/cloakpipe"),
                    ner_settings,
                    timeout=1.0,
                )

        download_ner.assert_called_once_with(Path("/tmp/cloakpipe"), source_dir)
        self.assertEqual(str(model_path), ner_settings["model_path"])

    def test_ensure_ner_ready_installs_and_starts_gliner_sidecar_when_requested(self):
        module, _ = self._load_plugin(fetch_return=[])

        with tempfile.TemporaryDirectory() as temp_dir:
            marker_path = Path(temp_dir) / "ner-installed"
            with (
                mock.patch.object(module, "_probe_health", return_value=(False, "connection refused")),
                mock.patch.object(module, "_ner_install_marker_path", return_value=marker_path),
                mock.patch.object(module, "_install_ner_with_cloakpipe") as install_ner,
                mock.patch.object(module, "_start_local_ner", return_value=(True, Path("/tmp/cloakpipe-ner.log"))) as start_ner,
                mock.patch.object(module, "_wait_for_ner_health", return_value=(True, "ok")),
            ):
                module._ensure_ner_ready(
                    Path("/tmp/cloakpipe"),
                    {
                        "enabled": True,
                        "backend": "gliner_pii",
                        "model_path": None,
                        "sidecar_url": "http://127.0.0.1:9111",
                        "threshold": 0.4,
                    },
                    timeout=1.0,
                )

        install_ner.assert_called_once_with(Path("/tmp/cloakpipe"))
        start_ner.assert_called_once_with(Path("/tmp/cloakpipe"), "http://127.0.0.1:9111", threshold=0.4)

    def test_install_helper_uses_verified_cargo_package_name(self):
        module, _ = self._load_plugin(fetch_return=[])
        completed = subprocess.CompletedProcess(
            ["/tmp/cargo", "install", "cloakpipe-cli"],
            0,
            stdout="installed",
            stderr="",
        )

        with (
            mock.patch("subprocess.run", return_value=completed) as cargo_install,
            mock.patch.object(module, "_find_cloakpipe_binary", return_value=Path("/tmp/cloakpipe")),
        ):
            binary_path = module._install_cloakpipe_with_cargo(Path("/tmp/cargo"), timeout=12.0)

        self.assertEqual(Path("/tmp/cloakpipe"), binary_path)
        self.assertEqual(["/tmp/cargo", "install", "cloakpipe-cli"], cargo_install.call_args.args[0])

    def test_install_ner_helper_uses_cloakpipe_cli(self):
        module, _ = self._load_plugin(fetch_return=[])
        completed = subprocess.CompletedProcess(
            ["/tmp/cloakpipe", "ner", "install"],
            0,
            stdout="installed",
            stderr="",
        )

        with tempfile.TemporaryDirectory() as temp_dir:
            marker_path = Path(temp_dir) / "ner-installed"
            with (
                mock.patch("subprocess.run", return_value=completed) as ner_install,
                mock.patch.object(module, "_ner_install_marker_path", return_value=marker_path),
            ):
                module._install_ner_with_cloakpipe(Path("/tmp/cloakpipe"), timeout=12.0)

            self.assertTrue(marker_path.exists())
            self.assertEqual(["/tmp/cloakpipe", "ner", "install"], ner_install.call_args.args[0])

    def test_download_ner_model_helper_uses_cloakpipe_cli(self):
        module, _ = self._load_plugin(fetch_return=[])
        completed = subprocess.CompletedProcess(
            ["/tmp/cloakpipe", "ner", "download"],
            0,
            stdout="downloaded",
            stderr="",
        )

        with tempfile.TemporaryDirectory() as temp_dir:
            source_dir = Path(temp_dir)
            model_path = source_dir / "models" / "distilbert-pii" / "quantized" / "model_quantized.onnx"
            model_path.parent.mkdir(parents=True, exist_ok=True)
            model_path.write_text("ok", encoding="utf-8")

            with mock.patch("subprocess.run", return_value=completed) as ner_download:
                resolved_model_path = module._download_ner_model_with_cloakpipe(
                    Path("/tmp/cloakpipe"),
                    source_dir,
                    timeout=12.0,
                )

        self.assertEqual(model_path, resolved_model_path)
        self.assertEqual(["/tmp/cloakpipe", "ner", "download"], ner_download.call_args.args[0])
        self.assertEqual(str(source_dir), ner_download.call_args.kwargs["cwd"])

    def test_missing_tools_raise_manual_guidance(self):
        module, _ = self._load_plugin(fetch_return=[])

        with tempfile.TemporaryDirectory() as temp_dir:
            managed_dir = Path(temp_dir)
            with (
                mock.patch.object(module, "_managed_runtime_dir", return_value=managed_dir),
                mock.patch.object(module, "_probe_health", return_value=(False, "connection refused")),
                mock.patch.object(module, "_find_cloakpipe_binary", return_value=None),
                mock.patch.object(module, "_find_cargo_binary", return_value=None),
            ):
                with self.assertRaises(module.CloakPipeUnavailableError) as context:
                    module._ensure_cloakpipe_ready(
                        "http://127.0.0.1:3100/v1",
                        timeout=1.0,
                        requested_model="cloakpipe/latest",
                    )

        message = str(context.exception)
        self.assertIn("cargo install cloakpipe-cli", message)
        self.assertIn("cloakpipe --config", message)
        self.assertIn("docker run -p 3100:3100 ghcr.io/cloakpipe/cloakpipe:latest", message)
        self.assertIn("sign-in page", message)

    def test_non_local_base_url_skips_local_automation(self):
        module, _ = self._load_plugin(fetch_return=[])

        with mock.patch.object(module, "_probe_health", return_value=(False, "timed out")):
            with self.assertRaises(module.CloakPipeUnavailableError) as context:
                module._ensure_cloakpipe_ready(
                    "https://remote.example.com/v1",
                    timeout=1.0,
                    requested_model="cloakpipe/latest",
                )

        self.assertIn("non-local host", str(context.exception))

    def test_write_managed_config_uses_explicit_runtime_paths(self):
        module, _ = self._load_plugin(fetch_return=[])

        with tempfile.TemporaryDirectory() as temp_dir:
            managed_dir = Path(temp_dir)
            with mock.patch.object(module, "_managed_runtime_dir", return_value=managed_dir):
                config_path = module._write_managed_config("http://127.0.0.1:3100/v1", "cloakpipe/latest")

            contents = config_path.read_text(encoding="utf-8")

        self.assertEqual(managed_dir / "cloakpipe.toml", config_path)
        self.assertIn('listen = "127.0.0.1:3100"', contents)
        self.assertIn(f'path = "{managed_dir / "vault.enc"}"', contents)
        self.assertIn("[audit]", contents)
        self.assertIn("enabled = true", contents)
        self.assertIn(f'log_path = "{managed_dir / "audit"}"', contents)
        self.assertIn('format = "jsonl"', contents)
        self.assertIn("log_mappings = false", contents)

    def test_write_managed_config_includes_ner_settings_when_enabled(self):
        module, _ = self._load_plugin(fetch_return=[])

        with tempfile.TemporaryDirectory() as temp_dir:
            managed_dir = Path(temp_dir)
            with mock.patch.object(module, "_managed_runtime_dir", return_value=managed_dir):
                config_path = module._write_managed_config(
                    "http://127.0.0.1:3100/v1",
                    "cloakpipe/latest",
                    {
                        "enabled": True,
                        "backend": "distilbert_pii",
                        "model_path": "/tmp/distilbert-pii.onnx",
                        "sidecar_url": "http://127.0.0.1:9111",
                        "threshold": 0.4,
                    },
                )

            contents = config_path.read_text(encoding="utf-8")

        self.assertIn("[detection.ner]", contents)
        self.assertIn('backend = "distilbert_pii"', contents)
        self.assertIn('model = "/tmp/distilbert-pii.onnx"', contents)
        self.assertNotIn('sidecar_url = "http://127.0.0.1:9111"', contents)

    def test_write_managed_config_keeps_sidecar_url_for_gliner_backend(self):
        module, _ = self._load_plugin(fetch_return=[])

        with tempfile.TemporaryDirectory() as temp_dir:
            managed_dir = Path(temp_dir)
            with mock.patch.object(module, "_managed_runtime_dir", return_value=managed_dir):
                config_path = module._write_managed_config(
                    "http://127.0.0.1:3100/v1",
                    "cloakpipe/latest",
                    {
                        "enabled": True,
                        "backend": "gliner_pii",
                        "model_path": None,
                        "sidecar_url": "http://127.0.0.1:9111",
                        "threshold": 0.4,
                    },
                )

            contents = config_path.read_text(encoding="utf-8")

        self.assertIn('backend = "gliner_pii"', contents)
        self.assertIn('sidecar_url = "http://127.0.0.1:9111"', contents)


if __name__ == "__main__":
    unittest.main()
