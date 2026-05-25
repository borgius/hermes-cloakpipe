---
applyTo: ".copilot-tracking/changes/20260524-cloakpipe-auto-install-startup-changes.md"
---

<!-- markdownlint-disable-file -->

# Task Checklist: Hermes CloakPipe auto-install and startup

## Overview

Plan the implementation needed for the Hermes CloakPipe provider plugin to verify CloakPipe availability, auto-install and start it when the local environment safely supports that workflow, and otherwise present clear manual instructions to the user.

## Objectives

- Add lazy CloakPipe readiness checks without introducing import-time side effects.
- Support safe local install/start automation only when the machine already has the required non-interactive tooling.
- Document and test the fallback paths for environments where automatic setup is not possible.

## Research Summary

### Project Files

- `plugins/model-providers/cloakpipe/__init__.py` - single provider implementation file and the main integration point for lifecycle logic.
- `tests/test_cloakpipe_provider.py` - existing `unittest` harness that should absorb lifecycle/install/start coverage.
- `README.md` - local documentation that needs behavior and upstream-link updates.

### External References

- #file:../research/20260524-cloakpipe-auto-install-startup-research.md - validated repo and upstream findings, including verified install/start commands, CLI mismatches, and macOS constraints.
- #fetch:https://github.com/borgius/cloakpipe - upstream repository page with documented Docker, cargo, and proxy usage examples.
- #fetch:https://app.cloakpipe.co/install.sh - confirms the advertised installer URL currently resolves to a sign-in page, so automation must not depend on it.
- #fetch:https://docs.docker.com/desktop/setup/install/mac-install/ - official macOS Docker Desktop install constraints relevant to manual fallback guidance.

## Implementation Checklist

### [ ] Phase 1: Runtime lifecycle foundation

- [ ] Task 1.1: Add lazy readiness gate
  - Details: .copilot-tracking/details/20260524-cloakpipe-auto-install-startup-details.md (Lines 11-25)

- [ ] Task 1.2: Add safe install/start helpers and fallback instructions
  - Details: .copilot-tracking/details/20260524-cloakpipe-auto-install-startup-details.md (Lines 27-43)

### [ ] Phase 2: Compatibility and test coverage

- [ ] Task 2.1: Reconcile model discovery and idempotence
  - Details: .copilot-tracking/details/20260524-cloakpipe-auto-install-startup-details.md (Lines 47-61)

- [ ] Task 2.2: Expand unit coverage for lifecycle branches
  - Details: .copilot-tracking/details/20260524-cloakpipe-auto-install-startup-details.md (Lines 63-77)

### [ ] Phase 3: Documentation and verification

- [ ] Task 3.1: Document the new behavior and operator escape hatches
  - Details: .copilot-tracking/details/20260524-cloakpipe-auto-install-startup-details.md (Lines 81-96)

## Dependencies

- Python standard library process, filesystem, and HTTP utilities.
- Existing `unittest`-based test harness in this repository.
- Optional Cargo toolchain for the automated install path.
- Docker Desktop only as a documented manual fallback, not an automated dependency.

## Success Criteria

- The provider checks CloakPipe availability lazily instead of performing install/start work at import time.
- Automatic install/start only runs when the environment already supports a non-interactive local setup path.
- Manual fallback instructions are explicit, verified, and macOS-aware.
- Tests and docs cover the new lifecycle behavior and edge cases.
