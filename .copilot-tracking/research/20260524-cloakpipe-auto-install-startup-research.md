<!-- markdownlint-disable-file -->

# Task Research Notes: Hermes CloakPipe auto-install and startup planning

## Research Executed

### File Analysis

- /Users/admin/dev/hermes-cloakpipe/README.md
  - States the plugin only registers provider `cloakpipe`, defaults `CLOAKPIPE_BASE_URL` to `http://127.0.0.1:3100/v1`, maps model IDs, and links to `rohansx/cloakpipe`, not `borgius/cloakpipe`.
- /Users/admin/dev/hermes-cloakpipe/plugins/model-providers/cloakpipe/__init__.py
  - Contains only import-time provider registration, base-URL normalization, model ID mapping, and provider metadata. No health checks, no process management, no install logic, and no user-facing error formatting beyond whatever Hermes provides.
- /Users/admin/dev/hermes-cloakpipe/plugins/model-providers/cloakpipe/plugin.yaml
  - Contains only plugin metadata (`name`, `kind`, `version`, `description`, `author`). No runtime configuration or hooks.
- /Users/admin/dev/hermes-cloakpipe/tests/test_cloakpipe_provider.py
  - Covers only import-time registration, base URL trimming, model-list mapping, and upstream model remapping. No tests exercise network reachability, subprocess handling, install flows, or user guidance.

### Code Search Results

- `CLOAKPIPE|cloakpipe|ProviderProfile|register_provider`
  - 64 matches, all confined to `/README.md`, `/plugins/model-providers/cloakpipe/__init__.py`, and `/tests/test_cloakpipe_provider.py`; no hidden lifecycle helpers or shared install utilities exist in this repo.
- `v1/models`
  - No matches found in a cloned copy of `borgius/cloakpipe`; upstream source search found no `/v1/models` route implementation.
- `cloakpipe serve`
  - Found in upstream `README.md` only.
- `cloakpipe start`
  - Found in upstream `crates/cloakpipe-cli/src/commands.rs` and `docs/PLAN.md`; current CLI source implements `start`, not `serve`.
- `/health`
  - Found in upstream proxy source (`crates/cloakpipe-proxy/src/server.rs`, `crates/cloakpipe-proxy/src/handlers.rs`) and planning docs; current source exposes an HTTP health endpoint.

### External Research

- #githubRepo:"borgius/cloakpipe start serve health models install"
  - Upstream README documents `docker run -p 3100:3100 ghcr.io/cloakpipe/cloakpipe:latest`, `cargo install cloakpipe`, `curl -fsSL https://cloakpipe.co/install.sh | sh`, `cloakpipe serve --port 3100`, and `cloakpipe health`, but current CLI source implements `cloakpipe start` and the proxy source exposes `/health` without any `/v1/models` route.
- #githubRepo:"borgius/cloakpipe config defaults proxy listen vault"
  - Current source auto-creates `cloakpipe.toml` if the config path does not exist, requires the upstream API key from `OPENAI_API_KEY` by default, defaults `proxy.listen` to `127.0.0.1:8900`, and defaults the vault path to `./vault.enc`, all relative to the process working directory unless an explicit config path is used.
- #fetch:https://raw.githubusercontent.com/borgius/cloakpipe/main/README.md
  - Verified documented install and runtime commands, README default port `3100`, Docker example, shell-installer URL, health command example, and environment variables.
- #fetch:https://raw.githubusercontent.com/borgius/cloakpipe/main/crates/cloakpipe-cli/src/main.rs
  - Verified current CLI subcommands include `Start`, `Test`, `Stats`, `Init`, `Setup`, `Mcp`, `Tree`, `Vector`, `Sessions`, and `Scan`; no `Health` or `Serve` subcommand exists in this file.
- #fetch:https://raw.githubusercontent.com/borgius/cloakpipe/main/crates/cloakpipe-cli/src/commands.rs
  - Verified `start()` creates a default config file if missing, loads an API key from `config.proxy.api_key_env`, opens the vault, and calls `server::start(state)`. Verified `default_config()` sets `proxy.listen` to `127.0.0.1:8900`, `proxy.upstream` to `https://api.openai.com`, `api_key_env` to `OPENAI_API_KEY`, and `vault.path` to `./vault.enc`.
- #fetch:https://raw.githubusercontent.com/borgius/cloakpipe/main/crates/cloakpipe-proxy/src/server.rs
  - Verified upstream routes include `GET /health`, `POST /v1/chat/completions`, `POST /v1/embeddings`, and session/tree routes. No models route is defined here.
- #fetch:https://raw.githubusercontent.com/borgius/cloakpipe/main/crates/cloakpipe-proxy/src/handlers.rs
  - Verified `/health` returns JSON `{"status":"ok","service":"cloakpipe"}`.
- #fetch:https://raw.githubusercontent.com/borgius/cloakpipe/main/crates/cloakpipe-core/src/config.rs
  - Verified the current TOML schema fields needed for managed startup: `proxy.listen`, `proxy.upstream`, `proxy.api_key_env`, `vault.path`, `vault.key_env`, plus detection/audit/session sections.
- #fetch:https://raw.githubusercontent.com/borgius/cloakpipe/main/crates/cloakpipe-cli/Cargo.toml
  - Verified the published package name is `cloakpipe-cli` and it installs a binary target named `cloakpipe`.
- #fetch:https://api.github.com/repos/borgius/cloakpipe/releases
  - Returned an empty JSON array, so there are currently no published GitHub releases on `borgius/cloakpipe`.
- #fetch:https://crates.io/crates/cloakpipe
  - Returned `Crate "cloakpipe" not found`, matching `cargo info cloakpipe` failure.
- #fetch:https://docs.docker.com/desktop/setup/install/mac-install/
  - Verified Docker Desktop on macOS is a separate application install, can require password-confirmed configuration, and is started via `/Applications/Docker.app`.
- #fetch:https://docs.docker.com/desktop/setup/install/mac-permission-requirements/
  - Verified Docker Desktop installation on macOS may require privileged configuration, optional symlink creation, and `launchd` tasks; Docker Desktop itself runs as an unprivileged user after installation.
- #fetch:https://docs.docker.com/desktop/troubleshoot-and-support/faqs/general/#how-do-i-run-docker-desktop-without-administrator-privileges
  - Verified Docker Desktop on macOS still requires a specific install path (`/Applications/Docker.app/Contents/MacOS/install --user=<userid>`) to avoid later admin prompts for non-admin users.
- #fetch:https://www.rust-lang.org/tools/install
  - Verified Rust is installed via `rustup`, tools land in `~/.cargo/bin`, and PATH changes may not take effect until the console restarts or the user logs out.
- #fetch:https://docs.python.org/3/reference/import.html
  - Verified importing a regular package implicitly executes its `__init__.py`, which means process-spawning side effects in this repo would run at import time.
- #fetch:https://docs.python.org/3/library/subprocess.html
  - Verified Python recommends argument sequences over shell strings, recommends `subprocess.run()` for typical cases, recommends full executable paths or `shutil.which()`, documents timeout/error behavior, and warns that `preexec_fn` is unsafe in threaded applications.
- #fetch:https://docs.python.org/3/library/shutil.html
  - Verified `shutil.which()` reads `PATH` (falling back to `os.defpath`) and returns `None` when no executable would be run.

### Project Conventions

- Standards referenced: repository uses plain `unittest`, direct module loading with stubs, and import-time provider registration from a single plugin file.
- Instructions followed: Task Researcher mode constraints, verified-findings-only requirement, and the `writing-clearly-and-concisely` skill.

## Key Discoveries

### Project Structure

This repository is intentionally small. All plugin behavior lives in one Python file: `/Users/admin/dev/hermes-cloakpipe/plugins/model-providers/cloakpipe/__init__.py`. The plugin reads `CLOAKPIPE_BASE_URL`, transforms model IDs between Hermes-facing `cloakpipe/<provider>-<model>` format and upstream `<provider>/<model>` format, instantiates `CloakPipeProfile`, and calls `register_provider(cloakpipe)` at import time.

The only tests live in `/Users/admin/dev/hermes-cloakpipe/tests/test_cloakpipe_provider.py`. They stub `providers` and `providers.base.ProviderProfile`, then import the plugin module to assert registration and simple mapping behavior. There is no existing harness for HTTP probing, subprocess supervision, filesystem state, or user-facing error messages.

The repository does not contain `.github/instructions/`, `copilot/`, or existing tracking notes. No other provider plugins or shared utilities are present in this workspace snapshot.

### Implementation Patterns

The plugin currently assumes CloakPipe is already reachable at a fixed OpenAI-style base URL. `_read_base_url()` normalizes `CLOAKPIPE_BASE_URL` and otherwise returns `http://127.0.0.1:3100/v1`. `fetch_models()` delegates to the inherited provider implementation and then renames returned models. `build_api_kwargs_extras()` only remaps the model ID back to upstream format.

Because provider registration happens at module import time, any new side effects added to module top level would execute as soon as Hermes imports the plugin. Python’s import docs confirm that importing a regular package implicitly executes its `__init__.py`. In this repo, that makes import-time process management materially riskier than helper-based lazy checks.

### Complete Examples

```rust
// Source: borgius/cloakpipe main branch
// - crates/cloakpipe-cli/src/commands.rs
// - crates/cloakpipe-proxy/src/server.rs

pub async fn start(config_path: &str) -> Result<()> {
    let config = if std::path::Path::new(config_path).exists() {
        load_config(config_path)?
    } else {
        tracing::info!("No config found, creating {} with defaults", config_path);
        let config = default_config();
        let toml_str = toml::to_string_pretty(&config)?;
        std::fs::write(config_path, toml_str)?;
        config
    };

    let key = resolve_vault_key(&config)?;
    let detector = Detector::from_config(&config.detection)?;
    let vault = Vault::open(&config.vault.path, key)?;
    let api_key = std::env::var(config.proxy.api_key_env.as_str())
        .with_context(|| format!("Set {} with your API key", config.proxy.api_key_env))?;

    let state = AppState::new(config, detector, vault, audit, api_key);
    server::start(state).await
}

pub fn build_router(state: Arc<AppState>) -> Router {
    Router::new()
        .route("/health", get(handlers::health))
        .route("/v1/chat/completions", post(handlers::proxy_chat_completions))
        .route("/v1/embeddings", post(handlers::proxy_embeddings))
}
```

### API and Schema Documentation

- Local plugin API assumptions in `/Users/admin/dev/hermes-cloakpipe/plugins/model-providers/cloakpipe/__init__.py`:
  - Reads `CLOAKPIPE_BASE_URL`.
  - Defaults to `http://127.0.0.1:3100/v1`.
  - Sets `models_url` to `<base_url>/models`.
- Current upstream proxy routes from `crates/cloakpipe-proxy/src/server.rs`:
  - `GET /health`
  - `POST /v1/chat/completions`
  - `POST /v1/embeddings`
  - session and tree routes
- Current upstream CLI behavior from `crates/cloakpipe-cli/src/main.rs` and `commands.rs`:
  - Global `--config <path>` option exists and defaults to `cloakpipe.toml`.
  - Implemented startup subcommand is `start`.
  - Implemented helper subcommands include `init`, `setup`, `stats`, `scan`, and `sessions`.
  - No `health` or `serve` subcommand appears in the current CLI source.
- Current upstream config schema from `crates/cloakpipe-core/src/config.rs`:
  - `proxy.listen: String`
  - `proxy.upstream: String`
  - `proxy.api_key_env: String`
  - `vault.path: String`
  - `vault.key_env: Option<String>`
  - `detection`, `audit`, `session`, `tree`, `vectors`, and `local` sections

### Configuration Examples

```bash
# Source: upstream README.md environment variables section
CLOAKPIPE_PORT=3100
CLOAKPIPE_HOST=0.0.0.0
CLOAKPIPE_LOG_LEVEL=info

CLOAKPIPE_UPSTREAM_URL=https://api.openai.com
CLOAKPIPE_TIMEOUT=30

CLOAKPIPE_POLICY=policies/dpdp.yaml
CLOAKPIPE_MIN_CONFIDENCE=0.8

CLOAKPIPE_VAULT_PATH=./vault.db
CLOAKPIPE_VAULT_KEY=

CLOAKPIPE_CLOUD_TOKEN=
```

### Technical Requirements

Repository-side implications:

- The primary implementation file is `/Users/admin/dev/hermes-cloakpipe/plugins/model-providers/cloakpipe/__init__.py`.
- The primary test file is `/Users/admin/dev/hermes-cloakpipe/tests/test_cloakpipe_provider.py`.
- A documentation update in `/Users/admin/dev/hermes-cloakpipe/README.md` is likely needed once behavior changes are implemented, because the local README still references `rohansx/cloakpipe` and the current plugin behavior does not mention any auto-start or troubleshooting flow.

Upstream constraints that affect implementation design:

- The current upstream source is config-driven. `start()` will create `cloakpipe.toml` if the chosen config path does not exist, and `default_config()` uses `127.0.0.1:8900` plus relative paths like `./vault.enc`.
- The upstream README and current upstream source disagree on at least four key points: install package name (`cloakpipe` vs `cloakpipe-cli`), startup command (`serve` vs `start`), default port (`3100` vs `8900`), and health command availability (`cloakpipe health` in README vs no CLI health subcommand in source).
- There are no published GitHub releases on `borgius/cloakpipe`, and `https://cloakpipe.co/install.sh` currently resolves to an HTML sign-in page at `https://app.cloakpipe.co/install.sh`, not a shell script body.
- `cargo info cloakpipe` fails, while `cargo info cloakpipe-cli` succeeds. The verified Cargo package name is `cloakpipe-cli`, and its `[[bin]]` target is named `cloakpipe`.
- No `/v1/models` route was found in the upstream source tree, which creates a likely mismatch with this plugin’s current `models_url=f"{_base_url}/models"` assumption.

macOS-specific automation constraints verified from official docs:

- Docker Desktop on macOS is a separate app install, not just a CLI binary. Starting to use Docker typically involves downloading `Docker.dmg`, installing `/Applications/Docker.app`, launching it, accepting terms, and sometimes granting privileged configuration.
- Docker Desktop may require password-confirmed configuration for symlinks or first-run setup, and its non-admin mode still depends on a specific installer invocation (`install --user=<userid>`).
- Rust installation on Unix-like systems is via `rustup`, and Rust tools land in `~/.cargo/bin`; the Rust docs state PATH changes may not take effect until the console is restarted or the user logs out.

Evidence gaps / blockers that remain after research:

- This repo does not include Hermes’ real `ProviderProfile` implementation, request flow, or error rendering layer. It is therefore unknown where the best user-visible fallback message should surface in the host product, or whether Hermes always calls `fetch_models()` before dispatching requests.
- Because upstream source exposes no `/v1/models` route, current model discovery may already be incompatible with the real proxy. That cannot be fully verified from this repo alone.
- The public GHCR image is documented in the README, but image pullability was not fully runtime-verified in this session. The Docker Hub badge target returned 404, and the GHCR manifest endpoint returned an auth challenge.

## Recommended Approach

Use a lazy runtime lifecycle helper inside `/Users/admin/dev/hermes-cloakpipe/plugins/model-providers/cloakpipe/__init__.py`, not import-time process management.

Selected direction:

1. **Keep import-time work minimal.** Preserve only provider registration and static helper definitions at module import. Do not install software, write config files, or launch processes from module top level.
2. **Probe health only when the provider is actually used.** The safest trigger points in this repo are methods that already run during real use, such as `fetch_models()` and request-prep logic (`build_api_kwargs_extras()` or a small shared helper those methods call).
3. **Check reachability via `GET /health`, not `/v1/models`.** Upstream source verifies `/health`; upstream source does not verify `/v1/models`.
4. **Prefer a managed config path if the plugin starts CloakPipe.** Current upstream `start()` is config-driven and writes defaults relative to the working directory. A plugin-managed config directory avoids littering the current directory with `cloakpipe.toml`, `vault.enc`, and audit output.
5. **Auto-start only when a local binary is already available or can be installed non-interactively with an already-installed Cargo toolchain.** The only verified non-GUI install path in current upstream evidence is Cargo, and the correct package is `cloakpipe-cli`, not `cloakpipe`.
6. **Do not rely on the advertised shell installer.** The installer URL currently resolves to a sign-in HTML page, so it should not be used for automation.
7. **Do not attempt to install Docker Desktop from the plugin.** Official Docker docs show macOS installation is an app-level flow with license, app launch, and sometimes privileged steps. That is a poor fit for a Python provider plugin.
8. **If Cargo is missing or installation fails, stop and show explicit instructions.** The fallback should mention the verified manual options and the current upstream inconsistencies.

Practically, that means the first implementation should support this order:

- Health-check the configured base URL.
- If healthy, proceed.
- If unhealthy, look for `cloakpipe` with `shutil.which()` and also check `~/.cargo/bin/cloakpipe` because Rust docs warn PATH updates may lag after installation.
- If the binary is missing but `cargo` is available, run `cargo install cloakpipe-cli`.
- Start the binary with an explicit config path and poll `/health` with a short timeout until healthy or a failure boundary is reached.
- If none of that succeeds, raise a clear error that gives the user manual steps for Cargo and Docker.

Recommended user-facing fallback text should be concrete and version-aware. It should explain:

- which URL was probed,
- whether `cloakpipe` was found,
- whether `cargo` was found,
- why auto-install or auto-start was skipped or failed,
- verified manual next steps, for example:
  - `cargo install cloakpipe-cli` then run `cloakpipe --config <managed-or-user config> start`
  - or, if Docker Desktop is already installed and running, use the upstream documented container command `docker run -p 3100:3100 ghcr.io/cloakpipe/cloakpipe:latest`
- that upstream documentation is currently inconsistent, so the plugin is using the source-verified `start` flow internally.

## Implementation Guidance

- **Objectives**: Add reliable CloakPipe availability checks, start a local instance when the environment already supports it, avoid import-time side effects, and emit actionable guidance when automation cannot safely continue.
- **Key Tasks**:
  - Extend `/Users/admin/dev/hermes-cloakpipe/plugins/model-providers/cloakpipe/__init__.py` with helper functions for:
    - health probing (`GET /health`),
    - executable discovery (`shutil.which`, `~/.cargo/bin/cloakpipe`),
    - optional Cargo install (`cargo install cloakpipe-cli`),
    - managed config-file creation,
    - subprocess spawn and reuse,
    - structured fallback-instruction formatting.
  - Introduce lazy idempotence so repeated model fetches or requests do not spawn duplicate CloakPipe processes.
  - Revisit `models_url` and `fetch_models()` behavior because upstream source does not expose `/v1/models`; implementation may need a different readiness probe and possibly a model-list fallback or a clearer error when model enumeration is unsupported.
  - Expand `/Users/admin/dev/hermes-cloakpipe/tests/test_cloakpipe_provider.py` (or split a second test file if preferred) to cover:
    - healthy existing instance,
    - unhealthy instance with existing binary,
    - missing binary with Cargo available,
    - missing binary and missing Cargo,
    - Cargo install failure,
    - import without side effects,
    - config path generation outside the current working directory,
    - instruction text contents.
  - Update `/Users/admin/dev/hermes-cloakpipe/README.md` after implementation so local docs match actual plugin behavior and upstream repo ownership.
- **Dependencies**: Python standard library should be sufficient (`urllib.request` or equivalent for `/health`, `subprocess`, `shutil`, `pathlib`, `tempfile`, `time`, `threading` or module-level state as needed). No third-party dependency is required by the evidence collected here.
- **Success Criteria**:
  - Importing the plugin alone does not start processes or write files.
  - The provider detects a running CloakPipe instance via `/health` before trying to install or start anything.
  - If Cargo exists, the provider can install the verified package name `cloakpipe-cli` and find the `cloakpipe` binary afterward.
  - Any started CloakPipe process uses an explicit, managed config path rather than polluting the caller’s current working directory.
  - Failures produce clear user-facing instructions that mention verified manual paths and the most relevant captured failure reason.
  - Tests cover success, failure, and idempotence paths around health checks, install/start attempts, and fallback guidance.