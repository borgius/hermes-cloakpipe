---
description: 'Implement the Hermes CloakPipe direct API sanitize/rehydrate multi-provider redesign.'
agent: agent
model: GPT-5.4
---

<!-- markdownlint-disable-file -->

# Implementation Prompt: Hermes CloakPipe direct API sanitize/rehydrate multi-provider

## Implementation Instructions

### Step 1: Create changes tracking file

You WILL create `.copilot-tracking/changes/20260524-cloakpipe-api-sanitize-rehydrate-multi-provider-changes.md` if it does not exist.

### Step 2: Execute implementation

You WILL systematically implement #file:../plans/20260524-cloakpipe-api-sanitize-rehydrate-multi-provider-plan.instructions.md task-by-task.
You WILL use #file:../details/20260524-cloakpipe-api-sanitize-rehydrate-multi-provider-details.md and #file:../research/20260524-cloakpipe-api-sanitize-rehydrate-multi-provider-research.md as the authoritative evidence for behavior, constraints, and operator guidance.
You WILL stop and document the blocker if the required Hermes request/response interception hook is absent in the actual runtime integration surface, because the checked-out repository does not prove that the current model-provider plugin can rehydrate native-provider responses by itself.

**CRITICAL**: If ${input:phaseStop:true} is true, you WILL stop after each Phase for user review.
**CRITICAL**: If ${input:taskStop:false} is true, you WILL stop after each Task for user review.

### Step 3: Cleanup

When ALL Phases are checked off (`[x]`) and completed you WILL do the following:

1. You WILL provide a markdown style link and a summary of all changes from the changes tracking file created in Step 1 to the user:

   - You WILL keep the overall summary brief.
   - You WILL add spacing around any lists.
   - You MUST wrap any reference to a file in a markdown style link.

2. You WILL provide markdown style links to .copilot-tracking/plans/20260524-cloakpipe-api-sanitize-rehydrate-multi-provider-plan.instructions.md, .copilot-tracking/details/20260524-cloakpipe-api-sanitize-rehydrate-multi-provider-details.md, and .copilot-tracking/research/20260524-cloakpipe-api-sanitize-rehydrate-multi-provider-research.md documents. You WILL recommend cleaning these files up as well.
3. **MANDATORY**: You WILL attempt to delete .copilot-tracking/prompts/implement-cloakpipe-api-sanitize-rehydrate-multi-provider.prompt.md.

## Success Criteria

- [ ] Changes tracking file created.
- [ ] All plan items implemented with working code or explicitly blocked with evidence.
- [ ] The design uses `/v1/pseudonymize` and `/v1/rehydrate` without relying on per-request `/v1/configure` races.
- [ ] Tests and documentation reflect the sidecar architecture and its limitations.
- [ ] Changes file updated continuously.
