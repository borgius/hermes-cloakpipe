<!-- markdownlint-disable-file -->

# Task Research Notes: Hermes CloakPipe direct API sanitize/rehydrate multi-provider planning

## Research Executed

### File Analysis

- /Users/admin/dev/cloakpipe/docs/api.md
  - Documents direct HTTP endpoints `/v1/pseudonymize`, `/v1/rehydrate`, `/v1/configure`, and states direct pseudonymize/rehydrate accept only `{"text": ...}`; docs also say `configure` rebuilds the detector for future requests and direct pseudonymize does not accept `session_id`.
- /Users/admin/dev/cloakpipe/crates/cloakpipe-proxy/src/server.rs
  - Confirms direct privacy endpoints and proxy routes share one `Router` and one `AppState`; no provider-specific direct endpoints exist.
- /Users/admin/dev/cloakpipe/crates/cloakpipe-proxy/src/handlers.rs
  - Shows `api_pseudonymize` and `api_rehydrate` use shared `state.vault`; `api_configure` mutates shared `detection_config`, `detector`, and `active_profile`; direct endpoints never read `state.api_key` or `config.proxy.upstream`.
- /Users/admin/dev/cloakpipe/crates/cloakpipe-proxy/src/state.rs
  - `AppState` stores one global `vault: Arc<Mutex<Vault>>`, one global `detector: Arc<RwLock<Detector>>`, and one `active_profile: Arc<RwLock<Option<String>>>`.
- /Users/admin/dev/cloakpipe/crates/cloakpipe-core/src/config.rs
  - `CloakPipeConfig.profile` is documented as `general, legal, healthcare, fintech`; `ProxyConfig.api_key_env` is part of required runtime config; no per-request profile field exists in direct endpoint schemas.
- /Users/admin/dev/cloakpipe/crates/cloakpipe-core/src/profiles.rs
  - Defines only runtime industry profiles `general`, `legal`, `healthcare`, `fintech` and aliases; these are code-defined, not file-backed.
- /Users/admin/dev/cloakpipe/crates/cloakpipe-core/src/vault.rs
  - Vault is a persistent global forward/reverse map with per-category counters; the same original value in the same vault reuses the same token.
- /Users/admin/dev/cloakpipe/crates/cloakpipe-core/src/rehydrator.rs
  - Rehydration replaces tokens using the vault reverse map; unknown tokens are left unchanged.
- /Users/admin/dev/cloakpipe/crates/cloakpipe-proxy/src/streaming.rs
  - Streaming rehydration relies on server-local chunk buffering logic that is not exposed as a direct HTTP endpoint.
- /Users/admin/dev/cloakpipe/crates/cloakpipe-cli/src/commands.rs
  - `start()` always loads `proxy.api_key_env` before starting the server, even if callers only plan to use direct privacy endpoints.
- /Users/admin/dev/cloakpipe/crates/cloakpipe-cli/src/presets.rs
  - Bundled preset names are `default`, `dpdp`, `gdpr`, `hipaa`, `pci-dss`, and `minimal`; preset selection is a startup/config concern, not `/v1/configure`.
- /Users/admin/dev/cloakpipe/policies/default.toml
  - Broad baseline TOML config with phones, IPs, and internal URLs enabled; NER disabled by default.
- /Users/admin/dev/cloakpipe/policies/dpdp.toml
  - India-focused preset with UPI, GSTIN, IFSC, and contextual bank-account rules; financial detection off; NER disabled.
- /Users/admin/dev/cloakpipe/policies/gdpr.toml
  - EU-focused preset with IBAN and VAT rules; financial, phone, IP, and URL detection enabled; NER disabled.
- /Users/admin/dev/cloakpipe/policies/hipaa.toml
  - PHI-focused preset with MRN, NPI, DEA, ICD10, and insurance ID rules; preserves FDA/CDC/WHO/NIH; NER disabled.
- /Users/admin/dev/cloakpipe/policies/minimal.toml
  - Low-noise preset with only high-confidence structured detections; financial, dates, IPs, and URLs off; NER disabled.
- /Users/admin/dev/cloakpipe/policies/pci-dss.toml
  - Payment-card preset with PAN, expiry, CVV, and track-data rules; NER disabled.
- /Users/admin/dev/cloakpipe/policies/README.md
  - Explicitly describes the TOML files as full `cloakpipe.toml`-compatible presets selected via `cloakpipe --config`.
- /Users/admin/dev/cloakpipe/crates/cloakpipe-proxy/tests/privacy_api.rs
  - Confirms direct pseudonymize → rehydrate round-trip works against one shared state object and confirms `/v1/configure` changes later detection behavior.
- /Users/admin/dev/hermes-cloakpipe/plugins/model-providers/cloakpipe/__init__.py
  - Current Hermes plugin is still a transport proxy provider: it registers provider `cloakpipe`, rewrites model IDs to `cloakpipe/<provider>-<model>`, auto-starts a local CloakPipe proxy, and only hooks `fetch_models()` plus `build_api_kwargs_extras()`.
- /Users/admin/dev/hermes-cloakpipe/tests/test_cloakpipe_provider.py
  - Tests confirm the plugin’s current contract is proxy-oriented: health probing, local process management, proxy model mapping, and request model remapping. No response rehydration hook exists in the local test surface.
- /Users/admin/dev/hermes-cloakpipe/README.md
  - Repository docs still describe the plugin as routing all traffic through a CloakPipe proxy and exposing `cloakpipe/<provider>-<model>` IDs.

### Code Search Results

- `IndustryProfile|from_name\(|detection_config\(|profile\b|policies/`
  - Terminal search across `/Users/admin/dev/cloakpipe` found runtime profile code in `crates/cloakpipe-core/src/profiles.rs`, `/v1/configure` handling in `crates/cloakpipe-proxy/src/handlers.rs`, preset installation in `crates/cloakpipe-cli/src/presets.rs`, and documentation for TOML presets in `README.md` and `policies/README.md`.
- `build_api_kwargs_extras|fetch_models|ProviderProfile|register_provider`
  - Workspace search in `/Users/admin/dev/hermes-cloakpipe` found only the single provider plugin and its tests; no local middleware, response hook, or alternate provider abstraction exists in this repo snapshot.
- `default.toml|dpdp.toml|gdpr.toml|hipaa.toml|minimal.toml|pci-dss.toml`
  - Matches are present in CloakPipe README/docs, CLI preset code, tests, and the policy files themselves, but not in `/v1/configure` runtime profile matching.
- `api_pseudonymize|api_rehydrate|api_configure|proxy_chat_completions`
  - Confirms the direct privacy endpoints and proxy endpoints coexist in the same server, but only the proxy chat path uses session-aware logic, upstream HTTP calls, and streaming rehydration.

### External Research

- #githubRepo:"borgius/cloakpipe direct API configure profiles presets"
  - Reviewed the checked-out upstream repository and its public GitHub README. The local source tree exposes direct privacy endpoints, runtime `configure` profiles, TOML presets, and `cloakpipe start`, while the public README still presents an older proxy-first `serve`/YAML/`cargo install cloakpipe` story.
- #fetch:https://raw.githubusercontent.com/borgius/cloakpipe/main/README.md
  - Public README still advertises `cargo install cloakpipe`, `cloakpipe serve --port 3100`, YAML policy files, and proxy-first integration examples. That does not match the current local source in `/Users/admin/dev/cloakpipe`.
- #fetch:https://raw.githubusercontent.com/borgius/cloakpipe/main/docs/api.md
  - Returned HTTP 404 during this session. The local `/Users/admin/dev/cloakpipe/docs/api.md` appears to be newer or not published at the same raw path.
- #fetch:https://raw.githubusercontent.com/borgius/cloakpipe/main/policies/README.md
  - Returned HTTP 404 during this session. The local `/Users/admin/dev/cloakpipe/policies/README.md` has content not mirrored at that public raw URL today.

### Project Conventions

- Standards referenced: `/Users/admin/dev/hermes-cloakpipe` is a small single-plugin Python repo with `unittest`-style tests and no local middleware framework in the checked-out files; `/Users/admin/dev/cloakpipe` is a Rust workspace with axum handlers, shared `AppState`, and config/preset separation between core profiles and CLI presets.
- Instructions followed: Task Researcher mode constraints, verified-findings-only requirement, and the `writing-clearly-and-concisely` skill.

## Key Discoveries

### Project Structure

The current Hermes plugin is architected as a **transport provider**, not as a general privacy sidecar. In `/Users/admin/dev/hermes-cloakpipe/plugins/model-providers/cloakpipe/__init__.py`, it registers one provider named `cloakpipe`, rewrites model IDs into a CloakPipe namespace, derives a CloakPipe base URL, prepares a CloakPipe config, and may auto-start a local CloakPipe process. The local repo exposes only two observable provider hooks: `fetch_models()` and `build_api_kwargs_extras()`.

The current CloakPipe server is a single axum router with one shared `AppState`. In `/Users/admin/dev/cloakpipe/crates/cloakpipe-proxy/src/server.rs`, both the direct privacy routes and the proxy routes are mounted on the same stateful server. In `/Users/admin/dev/cloakpipe/crates/cloakpipe-proxy/src/state.rs`, that state contains one detector, one detection config, one active-profile marker, one vault, one audit logger, one HTTP client, one API key, and one session manager.

That split matters for Hermes planning:

- The direct privacy API is provider-agnostic.
- The Hermes plugin is still provider-transport-specific.
- The local Hermes repo does not show a response post-processing hook or middleware layer that could rehydrate upstream responses after a native provider call.

### Implementation Patterns

The direct privacy endpoints are intentionally simple text tools:

- `/v1/pseudonymize` accepts only `{"text": "..."}` and writes new mappings into the shared vault.
- `/v1/rehydrate` accepts only `{"text": "..."}` and rehydrates using the same shared vault.
- `/v1/configure` mutates the server’s shared detector configuration for future requests.
- Direct endpoints do not accept a provider name, a `session_id`, or a per-request profile.

The proxy chat path is more capable than the direct privacy API. In `/Users/admin/dev/cloakpipe/crates/cloakpipe-proxy/src/handlers.rs`, `proxy_chat_completions` extracts session IDs, resolves coreferences, performs sensitivity escalation, proxies upstream, and applies response rehydration plus leaked-PII scanning. None of that richer request/response orchestration is exposed by `/v1/pseudonymize` or `/v1/rehydrate`.

The preset/config story is also split in two:

- Runtime `/v1/configure` uses code-defined `IndustryProfile` values in `crates/cloakpipe-core/src/profiles.rs`.
- CLI preset selection uses TOML files via `crates/cloakpipe-cli/src/presets.rs` and `policies/*.toml`.

### Complete Examples

```rust
// Source:
// - /Users/admin/dev/cloakpipe/crates/cloakpipe-proxy/src/state.rs
// - /Users/admin/dev/cloakpipe/crates/cloakpipe-proxy/src/handlers.rs
// - /Users/admin/dev/cloakpipe/crates/cloakpipe-core/src/profiles.rs

pub struct AppState {
    pub config: CloakPipeConfig,
    pub detector: Arc<RwLock<Detector>>,
    pub detection_config: Arc<RwLock<DetectionConfig>>,
    pub active_profile: Arc<RwLock<Option<String>>>,
    pub vault: Arc<Mutex<Vault>>,
    pub audit: AuditLogger,
    pub http_client: reqwest::Client,
    pub api_key: String,
    pub sessions: Arc<SessionManager>,
}

pub async fn api_pseudonymize(
    State(state): State<Arc<AppState>>,
    Json(params): Json<PseudonymizeRequest>,
) -> Result<impl IntoResponse, (StatusCode, String)> {
    let entities = detect_entities(&state, &params.text).await?;
    let response = {
        let mut vault = state.vault.lock().await;
        let result = Replacer::pseudonymize(&params.text, &entities, &mut vault)?;
        PseudonymizeResponse {
            text: result.text,
            entities_detected: entities.len(),
            categories: entity_categories(&entities),
        }
    };
    Ok(Json(response))
}

pub async fn api_configure(
    State(state): State<Arc<AppState>>,
    Json(params): Json<ConfigureRequest>,
) -> Result<impl IntoResponse, (StatusCode, String)> {
    let mut next_config = state.detection_config.read().await.clone();
    let mut next_active_profile = state.active_profile.read().await.clone();

    if let Some(ref profile_name) = params.profile {
        if let Some(profile) = IndustryProfile::from_name(profile_name) {
            next_config = profile.detection_config();
            next_active_profile = Some(profile.name().to_string());
        }
    }

    let new_detector = Detector::from_config(&next_config)?;
    *state.detection_config.write().await = next_config.clone();
    *state.detector.write().await = new_detector;
    *state.active_profile.write().await = next_active_profile.clone();
    Ok(Json(ConfigureResponse { active_profile: next_active_profile, /* ... */ }))
}

pub enum IndustryProfile {
    General,
    Legal,
    Healthcare,
    Fintech,
}
```

### API and Schema Documentation

- Direct privacy routes registered in `/Users/admin/dev/cloakpipe/crates/cloakpipe-proxy/src/server.rs`:
  - `POST /v1/pseudonymize`
  - `POST /v1/rehydrate`
  - `POST /v1/detect`
  - `GET|POST /v1/vault_stats`
  - `POST /v1/configure`
  - `POST /v1/session_context`
- Direct request shapes from `/Users/admin/dev/cloakpipe/crates/cloakpipe-proxy/src/handlers.rs`:
  - `PseudonymizeRequest { text: String }`
  - `RehydrateRequest { text: String }`
  - `ConfigureRequest { profile: Option<String>, enable: Option<Vec<String>>, disable: Option<Vec<String>> }`
- Runtime profile names from `/Users/admin/dev/cloakpipe/crates/cloakpipe-core/src/profiles.rs` and `/Users/admin/dev/cloakpipe/docs/mcp.md`:
  - canonical: `general`, `legal`, `healthcare`, `fintech`
  - aliases: `law`, `health`, `medical`, `finance`, `banking`
- Hermes plugin surface from `/Users/admin/dev/hermes-cloakpipe/plugins/model-providers/cloakpipe/__init__.py`:
  - `fetch_models()`
  - `build_api_kwargs_extras()`
  - provider registration via `register_provider(cloakpipe)`
  - no local response rehydration hook is visible in this repo snapshot

### Configuration Examples

```toml
# Source: /Users/admin/dev/cloakpipe/policies/dpdp.toml
[detection]
secrets = true
financial = false
dates = true
emails = true
phone_numbers = true
ip_addresses = true
urls_internal = false

[detection.custom]
patterns = [
  { name = "upi_id", regex = "\\b[A-Za-z0-9._-]{2,}@[A-Za-z][A-Za-z0-9._-]{1,63}\\b", category = "UPI_ID" },
  { name = "gstin", regex = "\\b\\d{2}[A-Z]{5}\\d{4}[A-Z][1-9A-Z]Z[0-9A-Z]\\b", category = "GSTIN" },
  { name = "ifsc", regex = "\\b[A-Z]{4}0[A-Z0-9]{6}\\b", category = "IFSC" },
]
```

```python
# Source: /Users/admin/dev/hermes-cloakpipe/plugins/model-providers/cloakpipe/__init__.py
class CloakPipeProfile(ProviderProfile):
    def fetch_models(self, *, api_key: str | None = None, timeout: float = 8.0):
        self._ensure_runtime_ready(timeout=...)
        upstream_models = super().fetch_models(api_key=api_key, timeout=timeout)
        return sorted(_to_cloakpipe_model(model_id) for model_id in upstream_models)

    def build_api_kwargs_extras(self, *, model: str | None = None, **context):
        self._ensure_runtime_ready(timeout=..., model=model)
        upstream_model = _to_upstream_model(model or "")
        return {}, {"model": upstream_model}
```

### Technical Requirements

- A direct-API sidecar design cannot rely on `/v1/configure` for per-request isolation, because `/v1/configure` mutates global server state and only affects future requests.
- A direct-API sidecar design must decide how to preserve or drop proxy-only features:
  - session-aware coreference resolution,
  - sensitivity escalation,
  - leaked-PII response scanning,
  - streaming token buffering/rehydration.
- A sidecar-only deployment still needs CloakPipe to start successfully. In current code, `cloakpipe start` still requires the environment variable named by `proxy.api_key_env`, even if callers never use the proxy routes.
- The current Hermes repo snapshot does not prove the existence of a response interception hook. Based on local evidence, moving to direct `/v1/pseudonymize` and `/v1/rehydrate` likely needs Hermes core changes or a new plugin abstraction, not only edits inside the current model-provider plugin.

## API and State Findings

### 1. Can one running CloakPipe instance serve many Hermes upstream providers through only `/v1/pseudonymize` and `/v1/rehydrate`?

**Yes, functionally yes — with shared-state caveats.**

Evidence:

- `/Users/admin/dev/cloakpipe/crates/cloakpipe-proxy/src/server.rs` mounts the direct privacy endpoints once on a shared router.
- `/Users/admin/dev/cloakpipe/crates/cloakpipe-proxy/src/handlers.rs` shows `api_pseudonymize` and `api_rehydrate` never call upstream providers and never read `state.api_key` or `config.proxy.upstream`.
- `/Users/admin/dev/cloakpipe/crates/cloakpipe-proxy/src/state.rs` uses `Arc` plus async locks, so concurrent callers share one server state safely at the Rust synchronization level.

What `yes` means in practice:

- Multiple Hermes providers can use the same CloakPipe instance as a shared sanitize/rehydrate service.
- The direct privacy API is upstream-provider-agnostic, so OpenAI, Anthropic, Azure, and local providers do not need separate direct endpoint shapes.

What the same `yes` does **not** mean:

- It does **not** provide provider isolation.
- It does **not** provide per-request detector isolation.
- It does **not** preserve proxy-only session features by itself.
- It does **not** remove the current CLI startup requirement for `proxy.api_key_env`.

### 2. Do `/v1/pseudonymize` and `/v1/rehydrate` rely on shared vault state that works across multiple providers?

**Yes. They both rely on the same shared vault, and that is exactly why cross-provider round-trip rehydration works.**

Evidence:

- `api_pseudonymize` in `/Users/admin/dev/cloakpipe/crates/cloakpipe-proxy/src/handlers.rs` acquires `let mut vault = state.vault.lock().await;` and calls `Replacer::pseudonymize(...)`.
- `api_rehydrate` in the same file acquires `let vault = state.vault.lock().await;` and calls `Rehydrator::rehydrate(...)`.
- `/Users/admin/dev/cloakpipe/crates/cloakpipe-core/src/vault.rs` stores:
  - a global `forward` map from original value to token,
  - a global `reverse` map from token to original,
  - per-category counters.
- Vault tests in `vault.rs` verify same original → same token in the same vault and token lookup → original round-trip.

Implications:

- A pseudonymized prompt sent through provider A can be rehydrated later by provider B if both use the same CloakPipe instance and the token text comes back.
- Shared vault state is helpful for consistency, but it also creates a shared token domain across providers, workspaces, tenants, and conversations unless Hermes intentionally runs separate instances or separate vault files.
- Direct endpoints do not accept `session_id`. The shared vault provides persistent mappings, but direct calls do not get the session-manager behavior that proxy chat requests use.

### 3. Is `/v1/configure` global mutable server state, and would per-request profile switching race?

**Yes. `/v1/configure` is global mutable server state, and Hermes should not use it for per-request profile switching.**

Evidence:

- `/Users/admin/dev/cloakpipe/crates/cloakpipe-proxy/src/handlers.rs` clones `state.detection_config` and `state.active_profile`, rebuilds a new `Detector`, then writes back:
  - `*state.detection_config.write().await = ...`
  - `*state.detector.write().await = ...`
  - `*state.active_profile.write().await = ...`
- `/Users/admin/dev/cloakpipe/docs/mcp.md` explicitly says the detector change affects **future** `detect` and `pseudonymize` calls.
- `/Users/admin/dev/cloakpipe/crates/cloakpipe-proxy/tests/privacy_api.rs` verifies that one `POST /v1/configure` call disabling email detection changes the outcome of a later `POST /v1/detect`.

Why this is unsafe for Hermes per-request routing:

- The profile is not passed as part of `POST /v1/pseudonymize`.
- Hermes would need two separate HTTP calls: configure first, then pseudonymize.
- Under concurrency, one request can reconfigure the detector between another request’s configure and pseudonymize calls.
- The locks prevent memory corruption, but they do not provide request-level isolation.

Recommendation from this finding:

- **Do not use `/v1/configure` for per-request profile switching.**
- If Hermes needs different privacy policies concurrently, run separate CloakPipe instances with fixed startup configuration, or extend CloakPipe with request-scoped profile parameters in a future upstream change.

### 4. Direct-API feature loss compared with the proxy path

Using only `/v1/pseudonymize` and `/v1/rehydrate` drops several behaviors that the current proxy path performs for Hermes today:

- Session-aware coreference and sensitivity escalation live in `proxy_chat_completions`, not in `api_pseudonymize`.
- Non-streaming leaked-PII response scanning also lives only in `proxy_chat_completions`.
- Streaming rehydration currently uses `/Users/admin/dev/cloakpipe/crates/cloakpipe-proxy/src/streaming.rs`, which depends on server-local chunk buffering and is not exposed as a direct endpoint.

That means Hermes would need to re-implement or deliberately drop those behaviors if it moves off the proxy route.

## Policy/Profile Findings

### 1. Runtime `/v1/configure` profiles are **not** backed by the TOML files in `policies/`

Evidence:

- `/Users/admin/dev/cloakpipe/crates/cloakpipe-core/src/profiles.rs` defines only `IndustryProfile::{General, Legal, Healthcare, Fintech}` and the aliases.
- `/Users/admin/dev/cloakpipe/crates/cloakpipe-proxy/src/handlers.rs` calls `IndustryProfile::from_name(profile_name)` inside `/v1/configure`.
- `/Users/admin/dev/cloakpipe/docs/mcp.md` documents the same canonical profile names and aliases.
- None of the `policies/*.toml` names (`dpdp`, `gdpr`, `hipaa`, `pci-dss`, `minimal`) appear in that runtime matching code.

### 2. The TOML files under `policies/` are startup presets, not runtime `configure` profiles

Evidence:

- `/Users/admin/dev/cloakpipe/policies/README.md` says the files are full `cloakpipe.toml`-compatible configs used with `cloakpipe --config policies/<name>.toml start`.
- `/Users/admin/dev/cloakpipe/crates/cloakpipe-cli/src/presets.rs` bundles and installs preset files named:
  - `default.toml`
  - `dpdp.toml`
  - `gdpr.toml`
  - `hipaa.toml`
  - `pci-dss.toml`
  - `minimal.toml`

### 3. Actual runtime profile names and semantics

| Runtime mechanism | Accepted names | Source of truth | Verified semantics |
| --- | --- | --- | --- |
| `/v1/configure` `profile` | `general` | `crates/cloakpipe-core/src/profiles.rs` | Broad defaults; phones, IPs, internal URLs, and GLiNER-PII NER enabled |
| `/v1/configure` `profile` | `legal`, alias `law` | `crates/cloakpipe-core/src/profiles.rs` | Adds legal custom patterns (`case_number`, `docket_number`, `bar_number`, `ssn`), preserves court names, disables IP/internal URLs, GLiNER-PII enabled |
| `/v1/configure` `profile` | `healthcare`, aliases `health`, `medical` | `crates/cloakpipe-core/src/profiles.rs` | Adds MRN/NPI/DEA/ICD patterns, preserves FDA/CDC/WHO/NIH, disables IP/internal URLs, GLiNER-PII enabled |
| `/v1/configure` `profile` | `fintech`, aliases `finance`, `banking` | `crates/cloakpipe-core/src/profiles.rs` | Adds SWIFT/ISIN/IBAN/routing-number patterns, IP/internal URLs on, phone numbers off, NER defaulted rather than GLiNER-PII |

### 4. Actual preset file names and semantics

| Startup preset file | Verified semantics from `policies/*.toml` |
| --- | --- |
| `default.toml` | Baseline broad config; phones, IPs, URLs on; NER disabled |
| `dpdp.toml` | India-focused: UPI, GSTIN, IFSC, contextual bank account; financial off; NER disabled |
| `gdpr.toml` | EU-focused: IBAN and VAT custom rules; financial, phones, IPs, URLs on; NER disabled |
| `hipaa.toml` | PHI-focused: MRN, NPI, DEA, ICD10, insurance IDs; preserves FDA/CDC/WHO/NIH; NER disabled |
| `minimal.toml` | Low-noise structured-only preset; financial/dates/IPs/URLs off; NER disabled |
| `pci-dss.toml` | Payment-card preset: PAN, expiry, CVV, track data; financial/dates/IPs/URLs off; NER disabled |

### 5. Important mismatch between the two profile systems

The runtime profile system and the preset-file system are not interchangeable:

- Runtime `healthcare` is not the same thing as preset `hipaa`.
- Runtime `fintech` is not the same thing as preset `pci-dss`.
- Runtime `general` enables GLiNER-PII by default, while `default.toml` leaves NER disabled.
- The preset files do not set `profile = "..."`, so `AppState.active_profile` starts as `None` when you boot from `dpdp.toml`, `gdpr.toml`, `hipaa.toml`, `pci-dss.toml`, or `minimal.toml`.

Operationally, that means Hermes should treat:

- `/v1/configure profile=...` as a code-defined runtime detector switch, and
- `--config <preset>.toml` as static instance configuration.

They are different knobs.

## Recommended Approach

Use CloakPipe as a **shared sanitize/rehydrate sidecar per fixed privacy policy and vault boundary**, not as a per-request reconfigured singleton proxy and not as one instance per upstream provider.

Selected direction:

1. **Keep provider selection in Hermes.**
   - Let Hermes keep native upstream providers such as OpenAI, Anthropic, Azure, Ollama, or local providers.
   - Stop encoding provider identity into `cloakpipe/<provider>-<model>` model IDs if Hermes adopts the direct-API design.

2. **Treat CloakPipe as a privacy middleware service, not the transport endpoint.**
   - Use `/v1/pseudonymize` before sending selected text fields upstream.
   - Use `/v1/rehydrate` on text returned from upstream.
   - Keep CloakPipe instances configured with a fixed detector/vault policy.

3. **Do not use `/v1/configure` for per-request profile changes.**
   - The code and tests show it mutates global state for future requests.
   - That design races under concurrent Hermes traffic.

4. **Run one CloakPipe instance per privacy-policy/vault-isolation boundary.**
   - If all Hermes providers can share one detector configuration and one vault, one instance is enough.
   - If different providers, tenants, or workspaces need different policies or separate token domains, run separate instances or separate vaults.
   - In practice, `one instance per profile` is the right default, and `per tenant/workspace` may be needed if shared rehydration across contexts is undesirable.

5. **Do not assume the current Hermes model-provider plugin alone can implement this.**
   - The local Hermes repo exposes no verified response hook or middleware layer.
   - A direct-API design likely needs Hermes core changes or a new plugin abstraction that can mutate outbound request bodies and inbound responses.

6. **Scope a first Hermes sidecar rollout to non-streaming text flows unless Hermes adds explicit streaming buffering.**
   - The direct HTTP API does not expose a streaming rehydration endpoint.
   - Current CloakPipe streaming logic is internal server code, not a public direct API.

In short:

- **Yes**, a single CloakPipe instance can serve many upstream providers when used only for sanitize/rehydrate.
- **No**, Hermes should not drive multiple concurrent policies through one instance by calling `/v1/configure` per request.
- **Recommended architecture:** fixed-profile sidecar instances shared across providers, with Hermes-native providers doing the real transport.

## Implementation Guidance

- **Objectives**: Support multiple Hermes upstream providers while keeping CloakPipe for reversible privacy transforms, avoid detector races, and make policy/vault boundaries explicit.
- **Key Tasks**:
  - Add or expose a Hermes request/response interception layer that can:
    - pseudonymize outbound text fields before upstream transport,
    - rehydrate inbound text fields after upstream responses,
    - optionally handle embeddings inputs separately from chat content.
  - Replace the current `cloakpipe/<provider>-<model>` transport-provider model with a privacy-service configuration model that points Hermes at one or more CloakPipe sidecars.
  - Represent CloakPipe instances by fixed configuration at startup, not by runtime `/v1/configure` flips.
  - Map Hermes privacy choices to sidecar instances by **policy + vault isolation boundary**, not by upstream provider name.
  - Decide the first supported content surface explicitly:
    - non-streaming chat message strings,
    - embeddings string inputs,
    - optionally defer multimodal arrays and streaming until Hermes has dedicated buffering/field-walking logic.
  - Plan for the current CloakPipe startup quirk: `cloakpipe start` still expects `proxy.api_key_env` to exist even if Hermes never uses proxy routes.
  - Update Hermes docs and tests so they describe sidecar privacy flows instead of proxy model remapping.
- **Dependencies**:
  - Hermes needs a verified pre-request and post-response hook, or a new plugin kind, because the checked-out provider plugin surface only shows request-prep and model enumeration hooks.
  - CloakPipe instances need fixed config files or presets plus isolated vault paths where isolation matters.
  - If streaming must be preserved, Hermes needs client-side chunk buffering equivalent to CloakPipe’s internal `streaming.rs` behavior, or CloakPipe needs a new direct streaming rehydration API.
- **Success Criteria**:
  - Hermes can send requests to multiple native upstream providers without routing transport through CloakPipe.
  - One CloakPipe sidecar can be shared across providers when they intentionally share the same detector profile and vault.
  - Different privacy policies or isolation domains use separate sidecar instances rather than `/v1/configure` races.
  - Non-streaming sanitize → upstream → rehydrate round-trips work reliably through `/v1/pseudonymize` and `/v1/rehydrate`.
  - The selected policy system is explicit: startup presets (`dpdp`, `gdpr`, `hipaa`, `pci-dss`, `minimal`, `default`) for fixed instances, or code-defined runtime profiles (`general`, `legal`, `healthcare`, `fintech`) only when no concurrency-sensitive switching is needed.
  - Documentation explains the trade-off that direct endpoints do not currently provide proxy-only session features or public streaming rehydration helpers.