<!-- markdownlint-disable-file -->

# Task Details: Hermes CloakPipe direct API sanitize/rehydrate multi-provider

## Research Reference

**Source Research**: #file:../research/20260524-cloakpipe-api-sanitize-rehydrate-multi-provider-research.md

## Phase 1: Establish virtual-provider integration contract

### Task 1.1: Verify Hermes hook limits and keep the provider wrapper boundary

Confirm that Hermes hooks are not enough for a safe privacy transform. `pre_llm_call` injects context into the current user message instead of replacing the raw prompt, and `post_llm_call` ignores return values. `transform_llm_output` can replace final display text, but it does not wrap the actual provider transport or tool-loop response object. Therefore the CloakPipe integration must stay a virtual model provider that owns the request/response boundary.

- **Files**:
  - `plugins/model-providers/cloakpipe/__init__.py` - keep registering provider `cloakpipe`, but replace CloakPipe-as-transport behavior with a virtual wrapper that calls CloakPipe privacy endpoints and then dispatches to the selected real provider.
  - Hermes host integration point outside this repository - use for evidence only; do not depend on `post_llm_call` for response rehydration.
- **Success**:
  - The implementation keeps the `cloakpipe/<provider>-<model>` virtual namespace.
  - The provider wrapper, not the hook system, owns outbound pseudonymization, upstream dispatch, and inbound rehydration.
- **Research References**:
  - #file:../research/20260524-cloakpipe-api-sanitize-rehydrate-multi-provider-research.md (Lines 83-109) - Current plugin shape versus direct sidecar API shape.
  - #file:../research/20260524-cloakpipe-api-sanitize-rehydrate-multi-provider-research.md (Lines 177-197) - Direct endpoint schemas and current Hermes plugin surface.
  - #file:../research/20260524-cloakpipe-api-sanitize-rehydrate-multi-provider-research.md (Lines 236-243) - Technical requirement that Hermes likely needs a new hook or plugin kind.
  - #file:../research/20260524-cloakpipe-api-sanitize-rehydrate-multi-provider-research.md (Lines 408-410) - Recommendation not to assume the current model-provider plugin can implement the redesign alone.
- **Dependencies**:
  - Verified provider-wrapper path for outbound and inbound text transforms.

### Task 1.2: Define virtual model routing plus sidecar instance mapping

Keep provider/model selection in the virtual model ID: `cloakpipe/<provider>-<model>`. Use that prefix to choose the real upstream provider and the remainder as the upstream model. Separately, choose the CloakPipe privacy sidecar by fixed policy and vault boundary. One CloakPipe instance may be shared across many upstream providers when they intentionally share the same detector configuration and vault, but separate instances are required when policies or token domains must differ.

- **Files**:
  - `plugins/model-providers/cloakpipe/__init__.py` - parse virtual IDs robustly, route sanitized requests to the selected real provider, and stop treating provider identity as the same thing as CloakPipe instance selection.
  - `README.md` - document virtual ID routing and how sidecar instances map to privacy policy and isolation boundaries.
- **Success**:
  - The design explicitly depends on `cloakpipe/<provider>-<model>` IDs to choose upstream transport.
  - Instance selection is defined by fixed policy/vault boundaries, not by per-request `/v1/configure` calls.
- **Research References**:
  - #file:../research/20260524-cloakpipe-api-sanitize-rehydrate-multi-provider-research.md (Lines 247-322) - Shared-state behavior, shared vault implications, and `/v1/configure` race risk.
  - #file:../research/20260524-cloakpipe-api-sanitize-rehydrate-multi-provider-research.md (Lines 324-382) - Distinction between runtime profiles and startup preset files.
  - #file:../research/20260524-cloakpipe-api-sanitize-rehydrate-multi-provider-research.md (Lines 384-420) - Recommended sidecar architecture and instance boundaries.
- **Dependencies**:
  - Task 1.1 completion.
  - Clear operator decision on whether providers, workspaces, or tenants may share a vault.

## Phase 2: Rework the plugin and service contract

### Task 2.1: Replace CloakPipe proxy transport with a virtual wrapper

Redesign the Hermes-side integration so Hermes still selects provider `cloakpipe`, but the plugin behaves as an OpenAI-compatible virtual wrapper. The first supported surface should be chat-completions text fields: pseudonymize outbound prompt text with `/v1/pseudonymize`, send the sanitized payload to the real provider/model encoded by `cloakpipe/<provider>-<model>`, then rehydrate returned assistant text and tool-call arguments with `/v1/rehydrate`. CloakPipe must not receive `/v1/chat/completions` traffic and must not own upstream provider/model selection.

- **Files**:
  - `plugins/model-providers/cloakpipe/__init__.py` - replace proxy-specific model remapping with a local virtual wrapper, direct CloakPipe privacy API client helpers, and upstream provider dispatch.
  - `README.md` - document the shift from CloakPipe proxy transport to virtual-provider sanitize/rehydrate wrapper usage.
- **Success**:
  - Provider/model choice comes from `cloakpipe/<provider>-<model>`, not from CloakPipe startup config.
  - The selected model is routed to the real provider after text sanitization; CloakPipe is used only for `/v1/pseudonymize` and `/v1/rehydrate`.
  - The first implementation scope is explicit: chat-completions text content and tool-call arguments, with multimodal image bytes and true live streaming deferred unless the wrapper adds buffering/field walking.
- **Research References**:
  - #file:../research/20260524-cloakpipe-api-sanitize-rehydrate-multi-provider-research.md (Lines 97-104) - Direct endpoint limitations versus richer proxy behavior.
  - #file:../research/20260524-cloakpipe-api-sanitize-rehydrate-multi-provider-research.md (Lines 179-189) - Request schemas for pseudonymize, rehydrate, and configure.
  - #file:../research/20260524-cloakpipe-api-sanitize-rehydrate-multi-provider-research.md (Lines 314-322) - Features lost when leaving the proxy route.
  - #file:../research/20260524-cloakpipe-api-sanitize-rehydrate-multi-provider-research.md (Lines 394-420) - Recommended sidecar-oriented architecture.
  - #file:../research/20260524-cloakpipe-api-sanitize-rehydrate-multi-provider-research.md (Lines 424-449) - Implementation objectives, dependencies, and success criteria.
- **Dependencies**:
  - Phase 1 completion.
  - A verified virtual wrapper path that can synthesize OpenAI-compatible non-streaming and streaming responses for Hermes.

### Task 2.2: Model profile selection as fixed startup configuration, not runtime mutation

Choose one of the fixed preset files or one fixed runtime profile per CloakPipe instance, then keep that instance stable for all requests that share the same privacy boundary. Document the mismatch between runtime `general`/`legal`/`healthcare`/`fintech` profiles and startup preset files such as `dpdp.toml` and `hipaa.toml`, and explicitly prohibit per-request `/v1/configure` switching.

- **Files**:
  - `README.md` - explain the difference between startup presets and runtime industry profiles.
  - Sidecar deployment/config documentation or examples in this repository - show how to point Hermes at one or more fixed-profile instances.
- **Success**:
  - The design never depends on `/v1/configure` between live requests.
  - Operators can choose the correct CloakPipe startup preset or fixed runtime profile for each sidecar instance.
- **Research References**:
  - #file:../research/20260524-cloakpipe-api-sanitize-rehydrate-multi-provider-research.md (Lines 289-312) - Why `/v1/configure` races under concurrency.
  - #file:../research/20260524-cloakpipe-api-sanitize-rehydrate-multi-provider-research.md (Lines 326-382) - Profile/preset names, semantics, and mismatch.
  - #file:../research/20260524-cloakpipe-api-sanitize-rehydrate-multi-provider-research.md (Lines 403-406) - Recommended fixed instance boundaries.
  - #file:../research/20260524-cloakpipe-api-sanitize-rehydrate-multi-provider-research.md (Lines 446-448) - Success criteria for explicit policy selection.
- **Dependencies**:
  - Phase 1 completion.
  - Operator agreement on policy-to-instance mapping.

## Phase 3: Cover limitations, tests, and migration docs

### Task 3.1: Add tests for direct sanitize/rehydrate flows and documented limits

Expand the repository’s tests so they cover direct HTTP client behavior rather than proxy transport assumptions. Test successful sanitize → provider → rehydrate round trips, shared-vault cross-provider semantics where intended, refusal to use `/v1/configure` for request-scoped policy switching, and clear handling of unsupported streaming or multimodal cases.

- **Files**:
  - `tests/test_cloakpipe_provider.py` - replace or extend proxy-oriented tests with sidecar-oriented request/response coverage.
  - Optional new sidecar-focused test module if the redesign no longer fits the current single-file test layout.
- **Success**:
  - Tests do not require a real CloakPipe or real upstream provider; they mock direct HTTP requests to `/v1/pseudonymize` and `/v1/rehydrate`.
  - The test suite proves that shared-vault behavior and unsupported-profile-switch behavior are intentional and documented.
- **Research References**:
  - #file:../research/20260524-cloakpipe-api-sanitize-rehydrate-multi-provider-research.md (Lines 269-287) - Shared vault behavior across providers.
  - #file:../research/20260524-cloakpipe-api-sanitize-rehydrate-multi-provider-research.md (Lines 289-322) - Configure race risk and feature-loss constraints.
  - #file:../research/20260524-cloakpipe-api-sanitize-rehydrate-multi-provider-research.md (Lines 424-449) - Implementation goals and success criteria.
- **Dependencies**:
  - Phase 2 completion.
  - A stable Hermes integration surface for request and response interception.

### Task 3.2: Update migration and operator documentation

Rewrite the repository documentation so it describes CloakPipe as a privacy sidecar instead of a transport proxy. The docs should explain multi-provider support, fixed sidecar instances, preset versus runtime profile differences, sidecar startup requirements, and the trade-offs of leaving the proxy path.

- **Files**:
  - `README.md` - update the main usage story, migration guidance, and limitations.
- **Success**:
  - Documentation explains how to run one or more fixed-profile CloakPipe sidecars and connect Hermes-native providers to them.
  - Documentation states that direct endpoints do not currently expose proxy-only session enrichment or public streaming rehydration helpers.
  - Documentation notes the startup quirk that `cloakpipe start` still expects `proxy.api_key_env` to exist even in sidecar-only usage.
- **Research References**:
  - #file:../research/20260524-cloakpipe-api-sanitize-rehydrate-multi-provider-research.md (Lines 27-30) - Preset/startup behavior and config assumptions.
  - #file:../research/20260524-cloakpipe-api-sanitize-rehydrate-multi-provider-research.md (Lines 314-322) - Features lost outside the proxy route.
  - #file:../research/20260524-cloakpipe-api-sanitize-rehydrate-multi-provider-research.md (Lines 384-420) - Recommended architecture and scope.
  - #file:../research/20260524-cloakpipe-api-sanitize-rehydrate-multi-provider-research.md (Lines 437-449) - Startup quirk and final success criteria.
- **Dependencies**:
  - Phase 2 completion.

## Dependencies

- A virtual provider wrapper that can receive Hermes chat-completions requests, call CloakPipe direct privacy endpoints, dispatch to the selected real provider, and return OpenAI-compatible responses.
- One or more CloakPipe sidecar instances with fixed config files or fixed profiles and intentionally scoped vault paths.
- A conscious decision to support non-streaming text flows first, or extra Hermes/CloakPipe work for streaming.

## Success Criteria

- Hermes exposes provider `cloakpipe` with `cloakpipe/<provider>-<model>` virtual IDs; the wrapper dispatches sanitized traffic to the selected provider/model.
- CloakPipe handles reversible privacy transforms only through `/v1/pseudonymize` and `/v1/rehydrate`.
- One CloakPipe sidecar can be shared across providers only when sharing the same detector policy and vault is acceptable.
- Different privacy policies or isolation boundaries are modeled as separate sidecar instances, not as `/v1/configure` races.
- Tests and docs make the direct-endpoint limitations explicit, especially around streaming, multimodal content, and lost proxy-only session behavior.
