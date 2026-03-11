# Skynet CLAW And Beyond

## Current Truth

There is no existing `CLAW` mechanism in the repository today.

Skynet has partial building blocks:

- context refresh in `tools/skynet_context_manager.py`
- context enrichment in `tools/skynet_brain.py`
- retrieval in `core/hybrid_retrieval.py`
- learning in `core/learning_store.py`
- persistent episodic/semantic memory in `core/persistent_memory.py`
- token-efficiency metrics in `core/self_evolution.py`

But there is no single named control mechanism that governs:

- what enters prompt context
- what gets compressed
- what gets anchored
- what gets paged out
- when a session is refreshed

This document introduces that missing layer.

## CLAW Definition

`CLAW` = `Context Lane Allocation and Windowing`

CLAW is a Skynet-native baseline context control mechanism for LLM sessions.

Its purpose is to keep prompts small, high-signal, and operationally correct without relying on huge chat history.

## What CLAW Does

CLAW has four core responsibilities:

### C: Context Lane Allocation

Every piece of candidate prompt material is assigned to a lane:

1. `Identity`
2. `Task Contract`
3. `Live State`
4. `Evidence`
5. `Procedure`
6. `Archive`

Only the top lanes should normally enter the active prompt.

### L: Limit By Budget

Each role gets a hard context budget:

- orchestrator
- worker
- consultant

Each task class gets a hard evidence budget:

- trivial
- simple
- moderate
- complex
- adversarial

Low-value context is dropped before it hits the LLM.

### A: Anchor Critical Information

Because long prompts suffer from position bias, CLAW anchors:

- non-negotiable constraints near the front
- active task state near the front
- expected output and validation contract near the end

It avoids burying critical facts in the middle.

### W: Window And Handoff

When a session approaches context exhaustion, CLAW does not replay the whole transcript.

It creates a structured handoff packet:

- current task
- files touched
- last verified result
- unresolved blockers
- next intended action
- top evidence references
- verification state

Then it refreshes or transfers the session using that compact packet.

## Why CLAW Is Needed

Current Skynet behavior still wastes context in three ways:

1. context enrichment is mostly append-style text
2. retrieval exists but is under-wired and not governed by budgets
3. refresh handoff is too thin and not based on a canonical context policy

CLAW fixes those specific weaknesses without pretending to solve all long-context problems by itself.

## Atomic CLAW Implementation Plan

### Wave 0: Policy Contract

1. Add `claw` section to `data/brain_config.json`
2. Define fixed lanes
3. Define role budgets
4. Define staleness and refresh thresholds
5. Define handoff packet schema

### Wave 1: Prompt Compiler

Create:

- `tools/skynet_prompt_compiler.py`

Responsibilities:

- accept role, task, live state, evidence candidates
- assign context to lanes
- enforce budgets
- anchor critical fields
- return final prompt plus token estimate

### Wave 2: Retrieval Integration

Modify:

- `tools/skynet_brain.py`
- `core/hybrid_retrieval.py`
- `core/learning_store.py`

Tasks:

1. retrieve compact evidence packets instead of raw text blobs
2. prefer validated, recent, reinforced items
3. separate procedure retrieval from archive retrieval
4. make retrieval optional and adaptive, not mandatory fixed-k stuffing

### Wave 3: Windowing And Handoff

Modify:

- `tools/skynet_context_manager.py`

Tasks:

1. generate CLAW handoff packets
2. refresh from handoff packet rather than last-result text alone
3. support role-correct re-entry for worker, consultant, orchestrator

### Wave 4: Metrics

Modify:

- `core/self_evolution.py`

Track:

- prompt size by lane
- prompt size by role
- memory hit rate
- quality per token
- refresh success rate
- dropped-context rate

### Wave 5: Operator Visibility

Optional but strongly recommended:

- expose CLAW metrics on dashboard
- show current lane budget usage
- show refresh risk / context pressure per worker

## CLAW Is The Baseline, Not The End State

CLAW is a control layer, not the final intelligence layer.

It should be deliberately surpassed by research-backed upgrades.

## How To Surpass CLAW

### 1. Surpass CLAW With Adaptive Retrieval

Research basis:

- Self-RAG shows retrieval should be adaptive and reflection-driven, not fixed-width stuffing.

Skynet implication:

- after CLAW lane selection, retrieval should be gated by necessity
- if evidence confidence is already high, skip retrieval
- if evidence is weak or conflicting, expand retrieval and critique

Source:

- Self-RAG: https://arxiv.org/abs/2310.11511

### 2. Surpass CLAW With Prompt Compression

Research basis:

- LLMLingua and LongLLMLingua show prompt compression can preserve signal while sharply reducing cost and latency.

Skynet implication:

- add a compressor after CLAW lane selection
- compress low-priority explanatory text before dropping high-value task facts
- use compression strongest on archive/procedure lanes, weakest on task contract lane

Sources:

- LLMLingua: https://arxiv.org/abs/2310.05736
- LongLLMLingua: https://arxiv.org/abs/2310.06839

### 3. Surpass CLAW With Virtual Memory

Research basis:

- MemGPT treats limited prompt context like RAM and external memory like virtual memory.

Skynet implication:

- make CLAW the page allocator
- move archived context into durable stores
- page compact summaries and evidence back into prompt only when required

Source:

- MemGPT: https://arxiv.org/abs/2310.08560

### 4. Surpass CLAW With Long-Term Memory Banks

Research basis:

- LongMem shows decoupled long-term memory can outperform naive long-context use.

Skynet implication:

- keep prompt context small
- move durable history into a long-term memory bank
- retrieve from memory bank rather than replaying prior conversation

Source:

- LongMem: https://arxiv.org/abs/2306.07174

### 5. Surpass CLAW With Graph-Structured Retrieval

Research basis:

- HippoRAG improves retrieval for deeper knowledge integration and multi-hop reasoning.

Skynet implication:

- build graph retrieval for:
  - repeated incidents
  - file relationships
  - procedures
  - causal chains
- use graph retrieval for complex or adversarial tasks

Source:

- HippoRAG: https://arxiv.org/abs/2405.14831

### 6. Surpass CLAW With Middle-Aware Placement

Research basis:

- Lost in the Middle shows long contexts are position-sensitive.
- Found in the Middle shows positional calibration can reduce that failure mode.

Skynet implication:

- front-load hard constraints
- end-load output contract
- avoid placing one critical fact only in the middle
- when model/runtime control exists, test middle-aware placement and calibration

Sources:

- Lost in the Middle: https://arxiv.org/abs/2307.03172
- Found in the Middle: https://arxiv.org/abs/2406.16008

## Recommended Architecture Path

### Stage 1: Introduce CLAW

Goal:

- stop prompt bloat
- stop transcript replay
- add fixed budgets
- add structured handoff

### Stage 2: Add Compression

Goal:

- preserve more signal under the same budget

### Stage 3: Add Adaptive Retrieval

Goal:

- retrieve only when needed
- critique evidence quality

### Stage 4: Add Virtual Memory + Long-Term Memory

Goal:

- make prompt context hot memory only

### Stage 5: Add Graph Retrieval And Positional Optimization

Goal:

- improve multi-hop and hard long-context tasks beyond CLAW baseline

## Worker Split

- `alpha`
  - prompt compiler and dashboard visibility
- `beta`
  - retrieval and compression integration
- `gamma`
  - procedure memory, graph memory, long-term memory wiring
- `delta`
  - context refresh handoff, regression tests, truth audits

## Acceptance Criteria

CLAW is real only when:

1. prompts are lane-structured
2. prompts have enforced budgets
3. refreshes use structured handoff packets
4. prompt size drops measurably
5. task quality does not regress
6. archive history is not replayed by default

Beyond-CLAW progress is real only when:

1. adaptive retrieval beats fixed retrieval on quality-per-token
2. compression reduces token cost without harming outcomes
3. long-term memory retrieval beats transcript replay
4. graph retrieval improves multi-hop problem solving

## Final Position

CLAW should be introduced first because it is feasible with current Skynet primitives.

But CLAW should be treated as a baseline context control mechanism, not the final answer.

The stronger research-backed destination is:

- CLAW
- plus compression
- plus adaptive retrieval
- plus virtual memory
- plus long-term memory
- plus graph retrieval

That is the path for Skynet to save context and still outperform naive long-context prompting.
