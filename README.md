# hermes-cloakpipe

Hermes virtual model-provider plugin for using [CloakPipe](https://github.com/borgius/cloakpipe) as a privacy tool.

Hermes still selects provider `cloakpipe`, but CloakPipe no longer acts as the upstream LLM transport. The plugin starts a local OpenAI-compatible wrapper, calls CloakPipe only for direct privacy transforms, sends the sanitized request to the selected real provider/model, then rehydrates the response before Hermes continues.

## Provider plugin files

- `plugins/model-providers/cloakpipe/__init__.py`
- `plugins/model-providers/cloakpipe/plugin.yaml`

## Attaching to a local Hermes install

Hermes discovers model-provider plugins lazily from `~/.hermes/plugins/model-providers/<name>/`.
It does not import them through the general plugin manager, so `hermes plugins enable ...` is not required for provider discovery.
This repo's actual plugin root is `plugins/model-providers/cloakpipe/`, not the repo root.
Because of that, installing the whole repository with `hermes plugins install` will not attach the provider as-is.

For local development, symlink the nested plugin directory into Hermes:

```bash
mkdir -p ~/.hermes/plugins/model-providers
ln -sfn /path/to/hermes-cloakpipe/plugins/model-providers/cloakpipe \
   ~/.hermes/plugins/model-providers/cloakpipe
```

After that, Hermes can resolve provider `cloakpipe` and its aliases `cloak` and `cp`.
`hermes plugins list` may still show `model-providers/cloakpipe` as `not enabled`; that status does not block provider discovery.

## Required environment

When the plugin starts a managed local CloakPipe process, these variables matter:

- `CLOAKPIPE_VAULT_KEY` must be a 64-char hex string (32 bytes).
- `CLOAKPIPE_BASE_URL` points at the CloakPipe privacy sidecar and defaults to `http://127.0.0.1:3100/v1`.
- `CLOAKPIPE_HERMES_BASE_URL` points at the local Hermes wrapper and defaults to `http://127.0.0.1:3199/v1`.
- Real upstream provider keys still use their normal Hermes environment variables, such as `OPENAI_API_KEY`, `OPENROUTER_API_KEY`, or `ANTHROPIC_API_KEY`.
- `CLOAKPIPE_API_KEY` is still accepted as an OpenAI fallback for compatibility with older setups.

Generate a valid vault key with:

```bash
export CLOAKPIPE_VAULT_KEY="$(openssl rand -hex 32)"
```

## Behavior

- Registers provider name `cloakpipe`.
- Uses `CLOAKPIPE_HERMES_BASE_URL` for the wrapper endpoint Hermes calls.
- Uses `CLOAKPIPE_BASE_URL` for CloakPipe direct privacy endpoints.
- Keeps import-time behavior side-effect free. The plugin does not install software, write config files, or start processes while Hermes is importing it.
- Probes CloakPipe lazily with `GET /health` when Hermes fetches models or prepares a request.
- Starts a local OpenAI-compatible wrapper on loopback when needed.
- The wrapper is in-process for the active Hermes command/session; it may exit after a one-shot command finishes.
- Exposes models as `cloakpipe/<provider>-<model>` for Hermes-side selection.
- By default, mirrors Hermes' currently active provider rows and re-exposes those models under the `cloakpipe/...` namespace.
- Parses `<provider>` as the real upstream provider and `<model>` as the real upstream model.
- Calls `POST /v1/pseudonymize` before upstream transport.
- Calls `POST /v1/rehydrate` on returned assistant content and tool-call arguments.
- Never calls `/v1/configure` per request.
- Never sends LLM requests to CloakPipe `/v1/chat/completions`.
- If live model enumeration is unavailable, falls back to `CLOAKPIPE_MODELS` or the provider's configured fallback model list.
- NER defaults to CloakPipe's built-in `distilbert_pii` backend. When the ONNX model is missing, the plugin runs `cloakpipe ner download` from `CLOAKPIPE_SOURCE_DIR` or a sibling `../cloakpipe` checkout. No separate NER sidecar start is required for this default path.
- If you explicitly select backend `gliner_pii`, the plugin keeps the sidecar flow with `cloakpipe ner install` and `cloakpipe ner start`.

## Request flow

When a user sends a prompt with provider `cloakpipe` and model `cloakpipe/openai-gpt-4o-mini`, the flow is:

1. Hermes builds a normal chat-completions request for provider `cloakpipe`.
2. The plugin ensures CloakPipe is healthy at `CLOAKPIPE_BASE_URL`.
3. The plugin ensures the local wrapper is healthy at `CLOAKPIPE_HERMES_BASE_URL`.
4. Hermes sends the request to the local wrapper, not to CloakPipe.
5. The wrapper parses `cloakpipe/openai-gpt-4o-mini` into upstream provider `openai` and upstream model `gpt-4o-mini`.
6. The wrapper sends each supported text field to CloakPipe `POST /v1/pseudonymize`.
7. The wrapper sends the sanitized request to the selected upstream provider.
8. The wrapper sends returned assistant text/tool-call arguments to CloakPipe `POST /v1/rehydrate`.
9. Hermes receives the rehydrated response and continues the tool loop or displays it to the user.

Streaming requests are buffered by the wrapper today: the wrapper calls the upstream provider non-streaming, rehydrates the full response, then emits a short OpenAI-compatible stream back to Hermes. This avoids leaking pseudonymized deltas before rehydration.

## Model IDs

Use this schema:

- `cloakpipe/openai-gpt-4o-mini`
- `cloakpipe/openrouter-openai/gpt-4o-mini`
- `cloakpipe/anthropic-claude-sonnet-4-5`
- `cloakpipe/openai-codex-gpt-5.1-codex`

Raw model IDs without a provider, such as `gpt-4o-mini`, default to `cloakpipe/openai-gpt-4o-mini`.

To control what the model picker shows, set `CLOAKPIPE_MODELS` to a comma-separated list. Entries may be either virtual IDs or normal `provider/model` IDs:

- `CLOAKPIPE_MODELS=cloakpipe/openai-gpt-4o-mini,openrouter/openai/gpt-4o-mini,anthropic/claude-sonnet-4-5`

If `CLOAKPIPE_MODELS` is unset, the plugin asks Hermes for the active provider/model rows it already knows how to show, then mirrors that set as `cloakpipe/<provider>-<model>`. An explicit `CLOAKPIPE_MODELS` value still wins as a manual override.

## Automatic local setup

When `CLOAKPIPE_BASE_URL` points at a local loopback address and the health check fails, the plugin can try a safe local recovery flow:

1. Reuse an existing `cloakpipe` binary when one is already available.
2. If `cloakpipe` is missing but Cargo is already installed, run `cargo install cloakpipe-cli`.
3. Write a managed CloakPipe config to `~/.hermes-cloakpipe/cloakpipe.toml` (or `CLOAKPIPE_MANAGED_DIR` if you override it).
4. Start the source-verified CLI command: `cloakpipe --config ~/.hermes-cloakpipe/cloakpipe.toml start`.
5. Start an in-process local Hermes wrapper on `CLOAKPIPE_HERMES_BASE_URL`.

The managed config keeps CloakPipe files out of the caller's working directory and listens on the same host and port as `CLOAKPIPE_BASE_URL`. The wrapper listens separately on `CLOAKPIPE_HERMES_BASE_URL`.

## What the plugin will not automate

- It does not use `https://app.cloakpipe.co/install.sh`, because that URL currently resolves to a sign-in page instead of a shell installer.
- It does not try to install Docker Desktop on macOS. Docker Desktop is an app-level install flow that can require downloading `Docker.dmg`, accepting license terms, configuring symlinks, and granting privileged setup.
- It does not auto-start a local CloakPipe instance when `CLOAKPIPE_BASE_URL` points at a non-local host.
- It does not auto-start the Hermes wrapper when `CLOAKPIPE_HERMES_BASE_URL` points at a non-local host.
- It does not reconfigure CloakPipe detector policy per request. Use separate sidecar instances when policies or vault boundaries differ.
- It does not transform binary multimodal payloads. Text parts in chat-completions messages are supported first.

## Manual setup and recovery

If automatic setup cannot continue, use one of these verified manual paths:

1. Set the required env vars:
   - `export CLOAKPIPE_VAULT_KEY="$(openssl rand -hex 32)"`
   - `export OPENAI_API_KEY=your-upstream-api-key`
2. Install the CLI:
   - `cargo install cloakpipe-cli`
3. Start CloakPipe with the managed config path:
   - `cloakpipe --config ~/.hermes-cloakpipe/cloakpipe.toml start`
4. If Docker Desktop is already installed and running, start the published container instead:
   - `docker run -p 3100:3100 ghcr.io/cloakpipe/cloakpipe:latest`

After CloakPipe is healthy, keep `CLOAKPIPE_BASE_URL=http://127.0.0.1:3100/v1` and let the plugin expose the Hermes wrapper at `CLOAKPIPE_HERMES_BASE_URL=http://127.0.0.1:3199/v1`.
