from __future__ import annotations

import json
import os
import shutil
import socket
import subprocess
import tempfile
import threading
import time
import unittest
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit


REPO_ROOT = Path(__file__).resolve().parents[1]
PLUGIN_DIR = REPO_ROOT / "plugins" / "model-providers" / "cloakpipe"

FAKE_SECRET_TO_TOKEN = {
    "test-api-key-secret-00000000000000000000000000000000": "CP_SECRET_API_KEY",
    "password=FakePassw0rd!": "CP_SECRET_PASSWORD",
    "db://fake_user:fake_password@internal.invalid:5432/app": "CP_SECRET_DATABASE_URL",
}
FAKE_SECRETS = tuple(FAKE_SECRET_TO_TOKEN.keys())
FAKE_TOKENS = tuple(FAKE_SECRET_TO_TOKEN.values())


def _replace_all(text: str, mapping: dict[str, str]) -> str:
    transformed = text
    for source, target in mapping.items():
        transformed = transformed.replace(source, target)
    return transformed


def _send_json(handler: BaseHTTPRequestHandler, status: int, payload: dict[str, Any]) -> None:
    data = json.dumps(payload).encode("utf-8")
    handler.send_response(status)
    handler.send_header("Content-Type", "application/json")
    handler.send_header("Content-Length", str(len(data)))
    handler.end_headers()
    handler.wfile.write(data)


class _RequestCapture:
    def __init__(self) -> None:
        self.lock = threading.Lock()
        self.privacy_requests: list[dict[str, Any]] = []
        self.upstream_requests: list[dict[str, Any]] = []
        self.upstream_raw_bodies: list[str] = []


class _FakeCloakPipeHandler(BaseHTTPRequestHandler):
    audit_dir: Path
    capture: _RequestCapture

    def log_message(self, *_args: Any) -> None:
        return

    def do_GET(self) -> None:  # noqa: N802 - stdlib hook
        if urlsplit(self.path).path.rstrip("/") in {"/health", "/v1/health"}:
            _send_json(self, 200, {"status": "ok"})
            return
        _send_json(self, 404, {"error": "not found"})

    def do_POST(self) -> None:  # noqa: N802 - stdlib hook
        length = int(self.headers.get("Content-Length") or "0")
        body = json.loads(self.rfile.read(length).decode("utf-8") or "{}")
        path = urlsplit(self.path).path.rstrip("/")
        with self.capture.lock:
            self.capture.privacy_requests.append({"path": path, "body": body})

        text = str(body.get("text") or "")
        if path.endswith("/pseudonymize"):
            transformed = _replace_all(text, FAKE_SECRET_TO_TOKEN)
            self._write_audit_event("pseudonymize", text)
            _send_json(self, 200, {"text": transformed})
            return

        if path.endswith("/rehydrate"):
            transformed = _replace_all(text, {value: key for key, value in FAKE_SECRET_TO_TOKEN.items()})
            self._write_audit_event("rehydrate", text)
            _send_json(self, 200, {"text": transformed})
            return

        _send_json(self, 404, {"error": "not found"})

    def _write_audit_event(self, event: str, text: str) -> None:
        markers = FAKE_SECRETS if event == "pseudonymize" else FAKE_TOKENS
        count = sum(1 for marker in markers if marker in text)
        self.audit_dir.mkdir(parents=True, exist_ok=True)
        audit_file = self.audit_dir / f"audit-{datetime.now(timezone.utc).date().isoformat()}.jsonl"
        entry = {
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "event": event,
            "surface": "api",
            "request_id": "fake-integration-request",
            "entities_detected": count if event == "pseudonymize" else None,
            "entities_replaced": count if event == "pseudonymize" else None,
            "tokens_rehydrated": count if event == "rehydrate" else None,
            "categories": ["secret"] if count else [],
        }
        with audit_file.open("a", encoding="utf-8") as file:
            file.write(json.dumps(entry) + "\n")


class _FakeOpenAIHandler(BaseHTTPRequestHandler):
    capture: _RequestCapture

    def log_message(self, *_args: Any) -> None:
        return

    def do_GET(self) -> None:  # noqa: N802 - stdlib hook
        if urlsplit(self.path).path.rstrip("/") in {"/models", "/v1/models"}:
            _send_json(self, 200, {"object": "list", "data": [{"id": "fake-model", "object": "model"}]})
            return
        _send_json(self, 404, {"error": "not found"})

    def do_POST(self) -> None:  # noqa: N802 - stdlib hook
        length = int(self.headers.get("Content-Length") or "0")
        raw_body = self.rfile.read(length).decode("utf-8")
        request_body = json.loads(raw_body or "{}")
        with self.capture.lock:
            self.capture.upstream_raw_bodies.append(raw_body)
            self.capture.upstream_requests.append(request_body)

        if any(secret in raw_body for secret in FAKE_SECRETS):
            _send_json(self, 500, {"error": {"message": "raw fake secret leaked to upstream"}})
            return

        user_text = " ".join(
            str(message.get("content") or "")
            for message in request_body.get("messages", [])
            if isinstance(message, dict) and message.get("role") == "user"
        )
        token_echo = " ".join(token for token in FAKE_TOKENS if token in user_text)
        _send_json(
            self,
            200,
            {
                "id": "chatcmpl-fake-integration",
                "object": "chat.completion",
                "created": int(time.time()),
                "model": request_body.get("model", "fake-model"),
                "choices": [
                    {
                        "index": 0,
                        "message": {"role": "assistant", "content": f"RESTORED_CHECK {token_echo}"},
                        "finish_reason": "stop",
                    }
                ],
                "usage": {"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2},
            },
        )


def _start_server(handler_type: type[BaseHTTPRequestHandler], **attributes: Any) -> ThreadingHTTPServer:
    for key, value in attributes.items():
        setattr(handler_type, key, value)
    server = ThreadingHTTPServer(("127.0.0.1", 0), handler_type)
    thread = threading.Thread(target=server.serve_forever, name=handler_type.__name__, daemon=True)
    thread.start()
    return server


def _unused_local_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


@unittest.skipUnless(
    os.environ.get("HERMES_INTEGRATION") == "1",
    "set HERMES_INTEGRATION=1 to run the real Hermes CLI integration test",
)
class HermesCloakPipeIntegrationTests(unittest.TestCase):
    def test_hermes_oneshot_pseudonymizes_upstream_and_rehydrates_response(self) -> None:
        hermes_bin = Path(os.environ.get("HERMES_BIN") or shutil.which("hermes") or "")
        if not hermes_bin.is_file():
            self.fail("Hermes CLI was not found; set HERMES_BIN or install hermes on PATH")

        capture = _RequestCapture()

        with tempfile.TemporaryDirectory(prefix="cloakpipe-hermes-integration-") as temp_dir:
            temp_root = Path(temp_dir)
            audit_dir = temp_root / "audit"
            privacy_server = _start_server(_FakeCloakPipeHandler, audit_dir=audit_dir, capture=capture)
            upstream_server = _start_server(_FakeOpenAIHandler, capture=capture)
            self.addCleanup(privacy_server.server_close)
            self.addCleanup(upstream_server.server_close)
            self.addCleanup(privacy_server.shutdown)
            self.addCleanup(upstream_server.shutdown)

            hermes_home = temp_root / "hermes-home"
            user_plugin_dir = hermes_home / "plugins" / "model-providers"
            user_plugin_dir.mkdir(parents=True)
            (user_plugin_dir / "cloakpipe").symlink_to(PLUGIN_DIR)
            (hermes_home / "config.yaml").write_text(
                "model:\n  provider: cloakpipe\n  default: cloakpipe/latest\ntoolsets: []\n",
                encoding="utf-8",
            )

            prompt = "Return these exact fake secrets after sanitization: " + " ".join(FAKE_SECRETS)
            env = os.environ.copy()
            env.update(
                {
                    "HERMES_HOME": str(hermes_home),
                    "CLOAKPIPE_MANAGED_DIR": str(temp_root / "managed"),
                    "CLOAKPIPE_BASE_URL": f"http://127.0.0.1:{privacy_server.server_port}/v1",
                    "CLOAKPIPE_HERMES_BASE_URL": f"http://127.0.0.1:{_unused_local_port()}/v1",
                    "CLOAKPIPE_UPSTREAM_PROVIDER": "openai",
                    "CLOAKPIPE_UPSTREAM_MODEL": "fake-model",
                    "CLOAKPIPE_REQUEST_TIMEOUT": "5",
                    "OPENAI_BASE_URL": f"http://127.0.0.1:{upstream_server.server_port}/v1",
                    "OPENAI_API_KEY": "fake-openai-key-for-local-integration",
                    "CLOAKPIPE_API_KEY": "fake-cloakpipe-key-for-local-integration",
                    "CLOAKPIPE_VAULT_KEY": "0" * 64,
                    "HERMES_DISABLE_ANALYTICS": "1",
                }
            )

            result = subprocess.run(
                [
                    str(hermes_bin),
                    "--ignore-rules",
                    "--provider",
                    "cloakpipe",
                    "--model",
                    "cloakpipe/latest",
                    "-z",
                    prompt,
                ],
                cwd=str(REPO_ROOT),
                env=env,
                capture_output=True,
                text=True,
                timeout=60,
                check=False,
            )

            self.assertEqual(result.returncode, 0, result.stderr[-2000:])
            for fake_secret in FAKE_SECRETS:
                self.assertIn(fake_secret, result.stdout)

            self.assertEqual(1, len(capture.upstream_requests))
            upstream_raw = capture.upstream_raw_bodies[0]
            for fake_secret in FAKE_SECRETS:
                self.assertNotIn(fake_secret, upstream_raw)
            for token in FAKE_TOKENS:
                self.assertIn(token, upstream_raw)
            self.assertNotIn("_cloakpipe_upstream", upstream_raw)

            audit_files = sorted(audit_dir.glob("audit-*.jsonl"))
            self.assertTrue(audit_files, "expected fake CloakPipe audit JSONL output")
            audit_text = "".join(path.read_text(encoding="utf-8") for path in audit_files)
            for fake_secret in FAKE_SECRETS:
                self.assertNotIn(fake_secret, audit_text)
            for token in FAKE_TOKENS:
                self.assertNotIn(token, audit_text)

            audit_entries = [json.loads(line) for line in audit_text.splitlines() if line.strip()]
            self.assertTrue(
                any(entry.get("event") == "pseudonymize" and entry.get("entities_replaced") for entry in audit_entries),
                audit_entries,
            )
            self.assertTrue(
                any(entry.get("event") == "rehydrate" and entry.get("tokens_rehydrated") for entry in audit_entries),
                audit_entries,
            )


if __name__ == "__main__":
    unittest.main()