---
applyTo: ".copilot-tracking/changes/20260524-cloakpipe-api-sanitize-rehydrate-multi-provider-changes.md"
---

<!-- markdownlint-disable-file -->

# Task Checklist: Hermes CloakPipe direct API sanitize/rehydrate multi-provider

## Overview

Plan the redesign that uses CloakPipe as a direct sanitize/rehydrate sidecar so Hermes can keep native provider transport while supporting multiple upstream providers under explicit privacy-policy and vault boundaries.

## Objectives

- Replace the current proxy-first design with an API-first privacy sidecar flow built on `/v1/pseudonymize` and `/v1/rehydrate`.
- Make profile and vault boundaries explicit so the design never depends on per-request `/v1/configure` mutations.

## Research Summary

### Project Files

- `plugins/model-providers/cloakpipe/__init__.py` - current proxy-oriented provider surface that still assumes transport routing through CloakPipe.
- `tests/test_cloakpipe_provider.py` - current test harness that will need sidecar-oriented coverage if the redesign proceeds.
- `README.md` - current operator documentation that still describes CloakPipe as the transport proxy.

### External References

- #file:../research/20260524-cloakpipe-api-sanitize-rehydrate-multi-provider-research.md - verified findings on direct privacy endpoints, shared vault state, profile race risks, and the recommended sidecar architecture.
- `/Users/admin/dev/cloakpipe/docs/api.md` - local API reference for `/v1/pseudonymize`, `/v1/rehydrate`, and `/v1/configure`.
- `/Users/admin/dev/cloakpipe/policies/` - fixed startup preset files that differ from runtime `/v1/configure` profiles.

## Implementation Checklist

### [ ] Phase 1: Establish sidecar integration contract

- [ ] Task 1.1: Verify or add Hermes request/response interception hooks
  - Details: .copilot-tracking/details/20260524-cloakpipe-api-sanitize-rehydrate-multi-provider-details.md (Lines 11-27)

- [ ] Task 1.2: Define sidecar instance mapping by policy and vault boundary
  - Details: .copilot-tracking/details/20260524-cloakpipe-api-sanitize-rehydrate-multi-provider-details.md (Lines 29-45)

### [ ] Phase 2: Rework the plugin and service contract

- [ ] Task 2.1: Replace transport-provider remapping with direct privacy API calls
  - Details: .copilot-tracking/details/20260524-cloakpipe-api-sanitize-rehydrate-multi-provider-details.md (Lines 49-67)

- [ ] Task 2.2: Model profile selection as fixed startup configuration, not runtime mutation
  - Details: .copilot-tracking/details/20260524-cloakpipe-api-sanitize-rehydrate-multi-provider-details.md (Lines 69-86)

### [ ] Phase 3: Cover limitations, tests, and migration docs

- [ ] Task 3.1: Add tests for direct sanitize/rehydrate flows and documented limits
  - Details: .copilot-tracking/details/20260524-cloakpipe-api-sanitize-rehydrate-multi-provider-details.md (Lines 90-106)

- [ ] Task 3.2: Update migration and operator documentation
  - Details: .copilot-tracking/details/20260524-cloakpipe-api-sanitize-rehydrate-multi-provider-details.md (Lines 108-124)

## Dependencies

- Hermes host support for pre-request and post-response transforms, or a new plugin abstraction that provides those hooks.
- One or more CloakPipe sidecar instances with fixed presets or fixed runtime profiles and intentionally scoped vault paths.
- A non-streaming-first scope unless Hermes or CloakPipe gains a direct streaming rehydration path.

## Success Criteria

- Hermes keeps native provider transport while CloakPipe handles reversible privacy transforms through `/v1/pseudonymize` and `/v1/rehydrate`.
- One CloakPipe sidecar can be shared across providers only when sharing the same detector policy and vault is acceptable.
- Different privacy policies or isolation boundaries are modeled as separate sidecar instances instead of `/v1/configure` races.
- Tests and documentation make the direct-endpoint limitations explicit, especially for streaming, multimodal content, and lost proxy-only session behavior.
