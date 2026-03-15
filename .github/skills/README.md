# ScreenMemory Skills

Workspace skill files for VS Code Copilot live in this folder.

## Current Status

No skill files have been implemented yet. See `docs/CLAUDE_SKILLS_INTEGRATION_PROPOSAL.md`
for the proposed skill catalog (18 items across 4 priority tiers).

## Proposed Skills (from integration proposal)

### Critical (Tier 1)
- `skynet-dispatch` — Worker task dispatch patterns and ghost-type delivery
- `skynet-boot` — Boot protocol sequences (Phase 1 + Phase 2)
- `worker-task` — Worker self-invocation and post-task protocol
- `browser-automation` — Chrome Bridge / GodMode usage patterns

### High Value (Tier 2)
- `bus-communication` — Bus publish/subscribe patterns and SpamGuard
- `daemon-management` — Daemon lifecycle, PID files, health checks
- `uia-automation` — UI Automation for VS Code window management
- `scoring-protocol` — Cross-validation and scoring system

### Standard (Tier 3)
- `self-awareness` — Consciousness kernel and identity verification
- `convene-protocol` — Multi-worker consensus and ConveneGate
- `knowledge-sharing` — Learning broadcast and collective intelligence
- `perception-stack` — Screen capture, OCR, visual grounding

## Adding a Skill

Place `.md` files in this directory following VS Code Copilot skill format.
Each skill should contain reusable prompt patterns and code examples.

<!-- signed: alpha -->
