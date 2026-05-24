# hermes-cloakpipe

Hermes model-provider plugin for routing LLM traffic through [CloakPipe](https://github.com/rohansx/cloakpipe).

## Provider plugin files

- `/tmp/workspace/borgius/hermes-cloakpipe/plugins/model-providers/cloakpipe/__init__.py`
- `/tmp/workspace/borgius/hermes-cloakpipe/plugins/model-providers/cloakpipe/plugin.yaml`

## Behavior

- Registers provider name `cloakpipe`.
- Uses `CLOAKPIPE_BASE_URL` (default: `http://127.0.0.1:3100/v1`).
- Sends all model calls through CloakPipe.
- Exposes models as `cloakpipe/<provider>-<model>`.
- Maps selected model IDs back to upstream format (`<provider>/<model>`) before request dispatch.
