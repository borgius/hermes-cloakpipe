from __future__ import annotations

import importlib.util
import os
import subprocess
import sys
import tempfile
import types
import unittest
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

    def test_import_has_no_runtime_side_effects(self):
        with (
            mock.patch("urllib.request.urlopen", side_effect=AssertionError("unexpected network probe during import")),
            mock.patch("subprocess.run", side_effect=AssertionError("unexpected cargo install during import")),
            mock.patch("subprocess.Popen", side_effect=AssertionError("unexpected process start during import")),
            mock.patch("shutil.which", side_effect=AssertionError("unexpected binary lookup during import")),
        ):
            _, register_calls = self._load_plugin(fetch_return=[])

        self.assertEqual(1, len(register_calls))

    def test_derive_health_url_strips_v1_suffix(self):
        module, _ = self._load_plugin(fetch_return=[])

        self.assertEqual("http://127.0.0.1:3100/health", module._derive_health_url("http://127.0.0.1:3100/v1"))
        self.assertEqual(
            "http://127.0.0.1:3100/proxy/health",
            module._derive_health_url("http://127.0.0.1:3100/proxy/v1/anthropic"),
        )

    def test_fetch_models_transforms_provider_model_ids_when_healthy(self):
        module, register_calls = self._load_plugin(
            fetch_return=[
                "openai/gpt-4o",
                "anthropic/claude-3-7-sonnet",
                "cloakpipe/google-gemini-2.5-pro",
            ]
        )

        profile = register_calls[0]
        with mock.patch.object(module, "_probe_health", return_value=(True, "ok")):
            models = profile.fetch_models(api_key="dummy")

        self.assertEqual(
            [
                "cloakpipe/anthropic-claude-3-7-sonnet",
                "cloakpipe/google-gemini-2.5-pro",
                "cloakpipe/openai-gpt-4o",
            ],
            models,
        )

    def test_fetch_models_falls_back_when_upstream_model_list_is_unavailable(self):
        module, register_calls = self._load_plugin(fetch_return=None)
        profile = register_calls[0]

        with mock.patch.object(module, "_probe_health", return_value=(True, "ok")):
            models = profile.fetch_models(api_key="dummy")

        self.assertEqual(["cloakpipe/openai-gpt-4o-mini"], models)

    def test_build_api_kwargs_extras_maps_back_to_upstream_model(self):
        module, register_calls = self._load_plugin(fetch_return=[])
        profile = register_calls[0]

        with mock.patch.object(module, "_ensure_cloakpipe_ready") as ensure_ready:
            _, kwargs = profile.build_api_kwargs_extras(model="cloakpipe/openai-gpt-4o")

        ensure_ready.assert_called_once()
        self.assertEqual({"model": "openai/gpt-4o"}, kwargs)

    def test_build_api_kwargs_extras_enables_ner_from_profile(self):
        module, register_calls = self._load_plugin(fetch_return=[])
        profile = register_calls[0]

        with mock.patch.object(module, "_ensure_cloakpipe_ready") as ensure_ready:
            profile.build_api_kwargs_extras(
                model="cloakpipe/openai-gpt-4o",
                profile="healthcare",
            )

        self.assertTrue(ensure_ready.call_args.kwargs["ner_settings"]["enabled"])
        self.assertEqual("http://127.0.0.1:9111", ensure_ready.call_args.kwargs["ner_settings"]["sidecar_url"])

    def test_resolve_ner_settings_prefers_explicit_nested_detection_config(self):
        module, _ = self._load_plugin(fetch_return=[])

        settings = module._resolve_ner_settings(
            {
                "profile": "fintech",
                "detection": {
                    "ner": {
                        "enabled": True,
                        "sidecar_url": "http://127.0.0.1:9222",
                        "confidence_threshold": 0.55,
                    }
                },
            }
        )

        self.assertEqual(
            {"enabled": True, "sidecar_url": "http://127.0.0.1:9222", "threshold": 0.55},
            settings,
        )

    def test_ensure_ready_reuses_local_process_across_repeated_calls(self):
        module, _ = self._load_plugin(fetch_return=[])

        with (
            mock.patch.object(module, "_probe_health", side_effect=[(False, "connection refused"), (True, "ok")]),
            mock.patch.object(module, "_find_cloakpipe_binary", return_value=Path("/tmp/cloakpipe")),
            mock.patch.object(module, "_write_managed_config", return_value=Path("/tmp/cloakpipe.toml")),
            mock.patch.object(module, "_start_local_cloakpipe", return_value=(True, Path("/tmp/cloakpipe.log"))) as start_local,
            mock.patch.object(module, "_wait_for_health", return_value=(True, "ok")),
        ):
            module._ensure_cloakpipe_ready("http://127.0.0.1:3100/v1", timeout=1.0, requested_model="cloakpipe/openai-gpt-4o")
            module._ensure_cloakpipe_ready("http://127.0.0.1:3100/v1", timeout=1.0, requested_model="cloakpipe/openai-gpt-4o")

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
            module._ensure_cloakpipe_ready("http://127.0.0.1:3100/v1", timeout=1.0, requested_model="cloakpipe/openai-gpt-4o")

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
                requested_model="cloakpipe/openai-gpt-4o",
                ner_settings={"enabled": True, "sidecar_url": "http://127.0.0.1:9111", "threshold": 0.4},
            )

        ensure_ner.assert_called_once_with(
            Path("/tmp/cloakpipe"),
            {"enabled": True, "sidecar_url": "http://127.0.0.1:9111", "threshold": 0.4},
            timeout=1.0,
        )

    def test_ensure_ner_ready_installs_and_starts_sidecar(self):
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
                    {"enabled": True, "sidecar_url": "http://127.0.0.1:9111", "threshold": 0.4},
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
                        requested_model="cloakpipe/openai-gpt-4o",
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
                    requested_model="cloakpipe/openai-gpt-4o",
                )

        self.assertIn("non-local host", str(context.exception))

    def test_write_managed_config_uses_explicit_runtime_paths(self):
        module, _ = self._load_plugin(fetch_return=[])

        with tempfile.TemporaryDirectory() as temp_dir:
            managed_dir = Path(temp_dir)
            with mock.patch.object(module, "_managed_runtime_dir", return_value=managed_dir):
                config_path = module._write_managed_config("http://127.0.0.1:3100/v1", "cloakpipe/openai-gpt-4o")

            contents = config_path.read_text(encoding="utf-8")

        self.assertEqual(managed_dir / "cloakpipe.toml", config_path)
        self.assertIn('listen = "127.0.0.1:3100"', contents)
        self.assertIn(f'path = "{managed_dir / "vault.enc"}"', contents)

    def test_write_managed_config_includes_ner_settings_when_enabled(self):
        module, _ = self._load_plugin(fetch_return=[])

        with tempfile.TemporaryDirectory() as temp_dir:
            managed_dir = Path(temp_dir)
            with mock.patch.object(module, "_managed_runtime_dir", return_value=managed_dir):
                config_path = module._write_managed_config(
                    "http://127.0.0.1:3100/v1",
                    "cloakpipe/openai-gpt-4o",
                    {"enabled": True, "sidecar_url": "http://127.0.0.1:9111", "threshold": 0.4},
                )

            contents = config_path.read_text(encoding="utf-8")

        self.assertIn("[detection.ner]", contents)
        self.assertIn('backend = "gliner-pii"', contents)
        self.assertIn('sidecar_url = "http://127.0.0.1:9111"', contents)


if __name__ == "__main__":
    unittest.main()
