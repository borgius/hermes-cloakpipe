---
applyTo: ".copilot-tracking/changes/20260524-cloakpipe-api-sanitize-rehydrate-multi-provider-changes.md"
---

<!-- markdownlint-disable-file -->

# Task Checklist: Hermes CloakPipe direct API sanitize/rehydrate multi-provider

## Overview

Plan the redesign that keeps CloakPipe as a virtual Hermes model provider while using CloakPipe only as a direct sanitize/rehydrate sidecar. Requests use `cloakpipe/<provider>-<model>` to select the real upstream provider/model, then the wrapper pseudonymizes prompts, dispatches sanitized traffic, and rehydrates responses.

## Objectives

- Replace the current CloakPipe proxy-first design with a virtual-provider wrapper built on `/v1/pseudonymize` and `/v1/rehydrate`.
- Preserve the `cloakpipe/<provider>-<model>` schema as the routing contract for selected upstream provider/model.
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

### [ ] Phase 1: Establish virtual-provider integration contract

- [ ] Task 1.1: Verify Hermes hook limits and keep the provider wrapper boundary
  - Details: .copilot-tracking/details/20260524-cloakpipe-api-sanitize-rehydrate-multi-provider-details.md (Lines 11-27)

- [ ] Task 1.2: Define virtual model routing plus sidecar instance mapping
  - Details: .copilot-tracking/details/20260524-cloakpipe-api-sanitize-rehydrate-multi-provider-details.md (Lines 29-45)

### [ ] Phase 2: Rework the plugin and service contract

- [ ] Task 2.1: Replace CloakPipe proxy transport with a virtual wrapper
  - Details: .copilot-tracking/details/20260524-cloakpipe-api-sanitize-rehydrate-multi-provider-details.md (Lines 49-68)

- [ ] Task 2.2: Model profile selection as fixed startup configuration, not runtime mutation
  - Details: .copilot-tracking/details/20260524-cloakpipe-api-sanitize-rehydrate-multi-provider-details.md (Lines 70-87)

### [ ] Phase 3: Cover limitations, tests, and migration docs

- [ ] Task 3.1: Add tests for direct sanitize/rehydrate flows and documented limits
  - Details: .copilot-tracking/details/20260524-cloakpipe-api-sanitize-rehydrate-multi-provider-details.md (Lines 91-107)

- [ ] Task 3.2: Update migration and operator documentation
  - Details: .copilot-tracking/details/20260524-cloakpipe-api-sanitize-rehydrate-multi-provider-details.md (Lines 109-125)

## Dependencies

- A virtual provider wrapper that can receive Hermes chat-completions requests, call CloakPipe direct privacy endpoints, dispatch to the selected real provider, and return OpenAI-compatible responses.
- One or more CloakPipe sidecar instances with fixed presets or fixed runtime profiles and intentionally scoped vault paths.
- A non-streaming-first scope unless Hermes or CloakPipe gains a direct streaming rehydration path.

## Success Criteria

- Hermes exposes provider `cloakpipe` with `cloakpipe/<provider>-<model>` virtual IDs; the wrapper dispatches sanitized traffic to the selected provider/model.
- CloakPipe handles reversible privacy transforms only through `/v1/pseudonymize` and `/v1/rehydrate`.
- One CloakPipe sidecar can be shared across providers only when sharing the same detector policy and vault is acceptable.
- Different privacy policies or isolation boundaries are modeled as separate sidecar instances instead of `/v1/configure` races.
- Tests and documentation make the direct-endpoint limitations explicit, especially for streaming, multimodal content, and lost proxy-only session behavior.
