# CloakPipe API sanitize/rehydrate multi-provider changes

## Decisions

- Hooks-only design is rejected for this work.
  - `pre_llm_call` does not replace the raw API payload safely.
  - `post_llm_call` return values are ignored.
- Keep Hermes provider `cloakpipe` and the virtual model schema `cloakpipe/<provider>-<model>`.
- Parse the virtual model ID to choose the real upstream provider/model.
- Use CloakPipe only through direct privacy endpoints:
  - `POST /v1/pseudonymize`
  - `POST /v1/rehydrate`
- Do not call `/v1/configure` per request; detector policy and vault scope are startup/config boundaries.
- Do not send LLM transport traffic to CloakPipe `/v1/chat/completions`.

## Implementation notes

- Added a local OpenAI-compatible wrapper endpoint for Hermes provider `cloakpipe` because `ProviderProfile` cannot transform responses directly.
- The wrapper pseudonymizes outbound chat text, dispatches the sanitized payload to the selected real provider/model, then rehydrates assistant text before returning the response to Hermes.
- Streaming requests are buffered: the wrapper calls upstream non-streaming, rehydrates the full response, then emits OpenAI-compatible SSE chunks back to Hermes.
- The wrapper is in-process for the active Hermes command/session; a one-shot command exits and takes the wrapper endpoint with it.
- `CLOAKPIPE_BASE_URL` remains the CloakPipe privacy sidecar URL.
- `CLOAKPIPE_HERMES_BASE_URL` / `CLOAKPIPE_WRAPPER_BASE_URL` controls the Hermes wrapper URL and defaults to `http://127.0.0.1:3199/v1`.
- `CLOAKPIPE_MODELS` is now only an explicit override. When unset, the plugin mirrors Hermes' active provider/model rows and re-exposes them as `cloakpipe/<provider>-<model>`.
- The plugin now patches Hermes picker helpers so the interactive `/model` picker also shows a populated `cloakpipe` row instead of an empty canonical entry.

## Validation log

- `python3 -m unittest discover -s tests -v` passed: 30 tests.
- After the picker-mirroring update, `python3 -m unittest discover -s tests -v` passed: 33 tests.
- Hermes E2E smoke passed with provider `cloakpipe` and model `cloakpipe/openai-gpt-4o-mini`; prompt `Reply with exactly OK.` returned `OK`.
- Hermes E2E rehydration smoke passed with fake email text; prompt requesting `alice@example.com` returned `alice@example.com` after the wrapper path.
- CloakPipe sidecar health check after E2E returned `200 {"service":"cloakpipe","status":"ok"}`. The wrapper health endpoint was not expected to persist after the one-shot Hermes process exited.
- Live Hermes runtime validation now shows `provider_model_ids('cloakpipe')` returning 87 mirrored models and both `list_authenticated_providers()` and `list_picker_providers()` including a populated `cloakpipe` row.
