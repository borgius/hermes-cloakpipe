# hermes-cloakpipe

Hermes model-provider plugin for routing LLM traffic through [CloakPipe](https://github.com/borgius/cloakpipe).

## Provider plugin files

- `plugins/model-providers/cloakpipe/__init__.py`
- `plugins/model-providers/cloakpipe/plugin.yaml`

## Behavior

- Registers provider name `cloakpipe`.
- Uses `CLOAKPIPE_BASE_URL` (default: `http://127.0.0.1:3100/v1`).
- Keeps import-time behavior side-effect free. The plugin does not install software, write config files, or start processes while Hermes is importing it.
- Probes CloakPipe lazily with `GET /health` when Hermes fetches models or prepares a request.
- Exposes models as `cloakpipe/<provider>-<model>` and maps selected model IDs back to upstream format (`<provider>/<model>`) before request dispatch.
- If `/v1/models` is unavailable, falls back to the provider's configured fallback model list instead of assuming upstream model enumeration works.

## Automatic local setup

When `CLOAKPIPE_BASE_URL` points at a local loopback address and the health check fails, the plugin can try a safe local recovery flow:

1. Reuse an existing `cloakpipe` binary when one is already available.
2. If `cloakpipe` is missing but Cargo is already installed, run `cargo install cloakpipe-cli`.
3. Write a managed config to `~/.hermes-cloakpipe/cloakpipe.toml` (or `CLOAKPIPE_MANAGED_DIR` if you override it).
4. Start the source-verified CLI command: `cloakpipe --config ~/.hermes-cloakpipe/cloakpipe.toml start`.

When NER is enabled, the plugin can also run `cloakpipe ner download` once to prepare the local NER model. It does not start a separate NER sidecar process because newer CloakPipe versions use that model internally.

The managed config keeps CloakPipe files out of the caller's working directory and listens on the same host and port as `CLOAKPIPE_BASE_URL`.

## What the plugin will not automate

- It does not use `https://app.cloakpipe.co/install.sh`, because that URL currently resolves to a sign-in page instead of a shell installer.
- It does not try to install Docker Desktop on macOS. Docker Desktop is an app-level install flow that can require downloading `Docker.dmg`, accepting license terms, configuring symlinks, and granting privileged setup.
- It does not auto-start a local CloakPipe instance when `CLOAKPIPE_BASE_URL` points at a non-local host.

## Manual setup and recovery

If automatic setup cannot continue, use one of these verified manual paths:

1. Install the CLI:
   - `cargo install cloakpipe-cli`
2. Start CloakPipe with the managed config path:
   - `cloakpipe --config ~/.hermes-cloakpipe/cloakpipe.toml start`
3. If Docker Desktop is already installed and running, start the published container instead:
   - `docker run -p 3100:3100 ghcr.io/cloakpipe/cloakpipe:latest`

After CloakPipe is healthy, point Hermes at it with `CLOAKPIPE_BASE_URL=http://127.0.0.1:3100/v1`.
