<!-- markdownlint-disable-file -->

# Task Details: Hermes CloakPipe auto-install and startup

## Research Reference

**Source Research**: #file:../research/20260524-cloakpipe-auto-install-startup-research.md

## Phase 1: Runtime lifecycle foundation

### Task 1.1: Add lazy readiness gate

Implement runtime-only readiness enforcement in `plugins/model-providers/cloakpipe/__init__.py` so the provider probes CloakPipe only when provider methods are used, not during import. Add a shared helper invoked from `fetch_models()` and request-preparation flow, and use `GET /health` instead of `/v1/models` for readiness checks.

- **Files**:
  - `plugins/model-providers/cloakpipe/__init__.py` - add lazy health probing, derive a health endpoint from the configured base URL, and wire a shared readiness helper into provider entry points.
- **Success**:
  - Importing the plugin alone still only registers the provider.
  - Provider entry points verify CloakPipe readiness before model fetch or request dispatch.
- **Research References**:
  - #file:../research/20260524-cloakpipe-auto-install-startup-research.md (Lines 77-89) - Current repo structure and import-time behavior.
  - #file:../research/20260524-cloakpipe-auto-install-startup-research.md (Lines 127-149) - Verified upstream `/health` route and missing `/v1/models` route.
  - #file:../research/20260524-cloakpipe-auto-install-startup-research.md (Lines 201-221) - Recommended lazy runtime flow.
- **Dependencies**:
  - Preserve current model-ID mapping behavior.

### Task 1.2: Add safe install/start helpers and fallback instructions

Implement helpers that discover an existing `cloakpipe` binary, optionally install it with `cargo install cloakpipe-cli` when Cargo is already present, generate and use an explicit managed config path, start the proxy with the source-verified `start` command, and format user-facing fallback instructions when automation cannot continue.

- **Files**:
  - `plugins/model-providers/cloakpipe/__init__.py` - add executable discovery, Cargo install, managed config generation, subprocess spawn/reuse, and fallback message formatting.
- **Success**:
  - Automation never relies on the broken shell installer URL.
  - Startup uses a managed config path rather than the caller's current working directory.
  - Failure messages explain what was tried and what manual steps the user should run next.
- **Research References**:
  - #file:../research/20260524-cloakpipe-auto-install-startup-research.md (Lines 31-68) - Verified install/start evidence and tooling constraints.
  - #file:../research/20260524-cloakpipe-auto-install-startup-research.md (Lines 179-191) - Source/documentation mismatches and macOS automation constraints.
  - #file:../research/20260524-cloakpipe-auto-install-startup-research.md (Lines 223-232) - Recommended fallback instruction content.
- **Dependencies**:
  - Task 1.1 completion.
  - Python standard library process and path utilities.

## Phase 2: Compatibility and test coverage

### Task 2.1: Reconcile model discovery and idempotence

Decide how the provider should behave when upstream does not expose `/v1/models`. Either add a safe model-enumeration fallback or surface a targeted error while keeping health checks and process reuse idempotent across repeated calls.

- **Files**:
  - `plugins/model-providers/cloakpipe/__init__.py` - adjust `models_url` assumptions, model-fetch fallback/error behavior, and shared process-state reuse.
- **Success**:
  - Repeated provider calls do not spawn duplicate local CloakPipe processes.
  - Model discovery behavior is explicit and aligned with verified upstream routes.
- **Research References**:
  - #file:../research/20260524-cloakpipe-auto-install-startup-research.md (Lines 129-149) - Current plugin assumptions versus verified upstream routes.
  - #file:../research/20260524-cloakpipe-auto-install-startup-research.md (Lines 181-185) - `/v1/models` mismatch and config-default risks.
  - #file:../research/20260524-cloakpipe-auto-install-startup-research.md (Lines 245-246) - Required idempotence and model-discovery follow-up.
- **Dependencies**:
  - Phase 1 completion.

### Task 2.2: Expand unit coverage for lifecycle branches

Extend `tests/test_cloakpipe_provider.py` to cover healthy-instance short-circuiting, binary discovery, Cargo-assisted install, missing-tool guidance, failure formatting, import without side effects, and managed config-path behavior using stubs and mocks that match the current `unittest` style.

- **Files**:
  - `tests/test_cloakpipe_provider.py` - add lifecycle, subprocess, filesystem, and error-message coverage.
- **Success**:
  - Tests cover the main success path and the most likely failure branches.
  - No test depends on a real CloakPipe binary, Docker Desktop, or networked installation.
- **Research References**:
  - #file:../research/20260524-cloakpipe-auto-install-startup-research.md (Lines 79-83) - Existing unittest/stub harness constraints.
  - #file:../research/20260524-cloakpipe-auto-install-startup-research.md (Lines 237-257) - Suggested helper and test coverage areas.
- **Dependencies**:
  - Phase 1 completion.
  - Follow the repository's existing `unittest` and stub-module pattern.

## Phase 3: Documentation and verification

### Task 3.1: Document the new behavior and operator escape hatches

Update `README.md` to describe when the plugin auto-checks CloakPipe, when it can auto-install/start locally, what it will not attempt automatically on macOS, and the exact manual commands users can run if automation is unavailable or fails.

- **Files**:
  - `README.md` - document behavior, correct the upstream repository link, and add manual startup/install troubleshooting.
- **Success**:
  - README matches implemented behavior and points to `borgius/cloakpipe`.
  - Manual instructions reference `cargo install cloakpipe-cli`, `cloakpipe --config <path> start`, and the documented Docker container path where appropriate.
- **Research References**:
  - #file:../research/20260524-cloakpipe-auto-install-startup-research.md (Lines 9-16) - Current README drift and existing local scope.
  - #file:../research/20260524-cloakpipe-auto-install-startup-research.md (Lines 179-191) - macOS automation constraints.
  - #file:../research/20260524-cloakpipe-auto-install-startup-research.md (Lines 223-232) - Expected manual fallback messaging.
- **Dependencies**:
  - Task 1.2 completion.
  - Task 2.1 completion.

## Dependencies

- Python standard library only.
- Existing `unittest` harness and stub-module test style.
- Cargo is optional for the automated install path; Docker Desktop is only a documented manual fallback.

## Success Criteria

- CloakPipe availability is checked lazily and safely, not at import time.
- Auto-install and auto-start only run when the environment supports them without interactive system setup.
- Failures produce actionable instructions grounded in verified upstream commands and macOS constraints.
- Tests and docs cover the new lifecycle behavior.
