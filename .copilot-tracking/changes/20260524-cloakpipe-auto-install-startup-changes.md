---
description: 'Implementation log for Hermes CloakPipe auto-install and startup changes'
---

<!-- markdownlint-disable-file -->

# 20260524 CloakPipe auto-install and startup changes

## Status

- [x] Phase 1: Runtime lifecycle foundation
- [x] Phase 2: Compatibility and test coverage
- [x] Phase 3: Documentation and verification

## Checklist

- [x] Created tracking file.
- [x] Added lazy readiness checks.
- [x] Added safe local install/start helpers.
- [x] Reconciled model discovery behavior.
- [x] Expanded lifecycle test coverage.
- [x] Updated README guidance.
- [x] Ran validation.
- [x] Cleaned up prompt artifact.

## Change log

- Created this tracking file.
- Added lazy `/health` readiness checks that only run when Hermes uses the provider.
- Added local binary discovery, optional `cargo install cloakpipe-cli`, managed config generation, process reuse, and actionable fallback messaging.
- Switched model discovery to fall back to configured fallback models when `/v1/models` is unavailable.
- Expanded `unittest` coverage for import-time safety, health URL derivation, local startup/install branches, managed config generation, and fallback guidance.
- Updated the README to describe the new lifecycle behavior, the verified upstream commands, and the manual Cargo/Docker recovery paths.
- Added a local `.env` file with placeholder runtime variables and updated `.gitignore` to keep it private.
- Verified the implementation with `python3 -m unittest discover -s tests -v` after confirming editor diagnostics were clean.
- Deleted the prompt artifact after the implementation was complete.
