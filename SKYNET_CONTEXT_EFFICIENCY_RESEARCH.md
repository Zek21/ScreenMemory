# Skynet Context Efficiency Research

## Objective

Determine how Skynet should help LLM sessions save context while still doing the job correctly, reliably, and truthfully.

This is a research and architecture proposal, not a claim that the system already does all of this.

## Short Answer

Skynet should act as an external context operating system:

- keep durable state outside the prompt
- retrieve only the smallest evidence needed for the current task
- compile prompts from ranked context classes under a strict token budget
- refresh or hand off sessions using structured summaries instead of replaying full history
- measure quality-per-token and memory-hit rate so context policy is tuned from evidence

## Current Repo Truth

### What Skynet Already Has

Skynet already has most of the raw primitives needed for strong context efficiency:

- task decomposition and context injection in `tools/skynet_brain.py`
- worker dispatch integration in `tools/skynet_brain_dispatch.py`
- worker context exhaustion detection and refresh in `tools/skynet_context_manager.py`
- lexical + optional vector + optional memory retrieval in `core/hybrid_retrieval.py`
- persistent factual learning in `core/learning_store.py`
- persistent episodic/semantic memory in `core/persistent_memory.py`
- realtime worker state and direct UI observation in `tools/skynet_realtime.py`
- cost-efficiency and memory-hit metrics in `core/self_evolution.py`
- durable operational state outside prompts:
  - bus
  - `todos.json`
  - `workers.json`
  - consultant/orchestrator state files
  - episode artifacts

### What The Code Actually Does Today

1. `tools/skynet_brain.py` recalls top learnings and retrieved docs, but then turns them into a simple appended text block.
2. `_build_context()` in `tools/skynet_brain.py` currently injects only a short freeform list, not a measured prompt budget.
3. `tools/skynet_context_manager.py` detects long conversations using UIA element counts and refreshes windows, but its handoff summary is thin and mostly based on the last result or last directive.
4. `core/hybrid_retrieval.py` supports BM25, vector, and memory fusion, but `tools/skynet_brain.py` initializes it with no LanceDB store and no memory object, so retrieval is under-wired in practice.
5. `core/learning_store.py` can export large knowledge blobs for context injection, but that export is not budget-aware.
6. `core/self_evolution.py` already tracks `tokens_used`, `memory_hits`, `memory_queries`, and `quality per token`, but those signals are not yet used as a first-class prompt policy controller.
7. `data/worker_output/audits/agents_audit.md` correctly identifies that oversized always-loaded instructions force unnecessary context load.

## Research Findings

### Finding 1: Long Context Is Not The Same As Useful Context

Local repo evidence:

- `tools/skynet_brain.py` already limits learnings and docs because unconstrained prompt stuffing is wasteful.
- `tools/skynet_context_manager.py` exists because long chat history degrades worker sessions in practice.

External research:

- "Lost in the Middle" shows that relevant information in long prompts is often used poorly when buried in the middle.
- The practical consequence for Skynet is simple: do not rely on large undifferentiated prompt dumps.

Implication:

- critical constraints and task contract should be near the top
- final action checklist and expected output should be near the end
- low-value background should never occupy the middle by default

## Finding 2: Similar Examples Matter More Than Raw History Volume

Local repo evidence:

- `core/hybrid_retrieval.py` is already designed to retrieve relevant context instead of replaying everything.
- `tools/skynet_brain_dispatch.py` already treats learnings and past results as enrichment rather than full replay.

External research:

- long-context ICL results show many gains come from attending to similar examples, not from learning from a giant unfiltered context blob

Implication:

- Skynet should retrieve a few high-similarity episodes or procedures
- it should not re-inject long conversation transcripts unless no compact artifact exists

## Finding 3: Hierarchical Memory Is The Right Mental Model

Local repo evidence:

- `core/persistent_memory.py` already separates episodic and semantic memory
- `core/learning_store.py` already consolidates and reinforces facts
- `tools/skynet_context_manager.py` already performs a crude form of paging by refreshing a session and re-injecting reduced state

External research:

- MemGPT’s OS-style memory model is the right abstraction: hot prompt memory, warm working memory, cold archival memory

Implication:

- Skynet should treat prompt context as L1 cache
- short-lived task state as L2
- structured learned procedures and episodes as L3
- archives and raw logs as cold storage

## Finding 4: Context Policy Must Be Measured, Not Intuited

Local repo evidence:

- `core/self_evolution.py` already tracks:
  - memory hit rate
  - tokens used
  - cost efficiency
  - quality score

Implication:

- prompt policy should be governed by measured success-per-token
- retrieval depth and context budget should adapt by task class and by observed value

## Main Problem Statement

Skynet has memory, retrieval, and refresh primitives, but it does not yet have a canonical context operating policy.

The missing layer is a `context compiler` that answers:

- what must be in the prompt
- what can be retrieved on demand
- what should be summarized
- what should remain outside the context entirely
- when to refresh
- how to hand off state between windows or agents

## Recommended Architecture

### 1. Context Lanes

Split all prompt material into fixed ranked lanes:

1. `Identity Lane`
   - who the agent is
   - role constraints
   - sender id
2. `Task Contract Lane`
   - goal
   - acceptance criteria
   - expected output
3. `Live State Lane`
   - current TODO
   - assigned files
   - worker availability
   - blocking alerts
4. `Evidence Lane`
   - top retrieved episodes
   - top relevant learnings
   - top code references
5. `Procedure Lane`
   - reusable steps distilled from prior successful episodes
6. `Archive Lane`
   - raw bus history
   - full transcripts
   - logs

Only lanes 1-4 should normally enter the prompt. Lanes 5-6 should be pulled only when necessary.

### 2. Prompt Compiler

Add a compiler layer that builds prompts under explicit budgets.

Proposed output sections:

- `IDENTITY`
- `GOAL`
- `NON-NEGOTIABLES`
- `ACTIVE STATE`
- `TOP EVIDENCE`
- `EXPECTED OUTPUT`

Rules:

- budget by task difficulty and role
- include only top-N evidence items
- clip by sentence or bullet, not raw characters alone
- place highest-risk constraints first
- place concrete output contract last

### 3. Episode-First Handoff

When a session is refreshed or resumed, do not carry forward raw conversation.

Instead create a structured handoff artifact:

- task
- files touched
- last confirmed result
- unresolved blockers
- next intended action
- evidence references
- confidence / verification state

This should replace the current minimal re-injection in `tools/skynet_context_manager.py`.

### 4. Procedure Distillation

Promote repeated successful patterns into compact procedures:

- “how to validate dashboard JS”
- “how to recover a dead consultant bridge”
- “how to route a multi-file dashboard truth fix”

Then retrieve procedures instead of raw narrative memories.

### 5. Role-Specific Context Packs

Different identities need different prompt payloads:

- `orchestrator`
  - mostly planning, routing, worker state, TODOs, latest results
- `worker`
  - narrow task contract, file scope, top evidence, expected validation
- `consultant`
  - architecture context, truth constraints, proposal target, no worker-control state unless needed

This reduces cross-role contamination and saves tokens.

### 6. Refresh Before Failure

Upgrade context refresh from a blunt threshold into a budget policy:

- warning zone: stop adding background context
- critical zone: switch to minimal prompt mode
- refresh zone: generate handoff artifact and open fresh window

### 7. Evidence-Backed Retrieval

`core/hybrid_retrieval.py` should be fully wired with:

- BM25
- vector search
- episodic memory
- semantic procedure memory

Then results should be re-ranked by:

- relevance
- confidence
- recency
- validation status
- task similarity

## Atomic Implementation Plan

### Wave 0: Truth Contract

1. Add `context_policy` section to `data/brain_config.json`
2. Define context classes and budgets by role:
   - orchestrator
   - worker
   - consultant
3. Define explicit fields for handoff summaries and evidence packets

### Wave 1: Context Compiler

Create:

- `tools/skynet_prompt_compiler.py`

Responsibilities:

- accept task + role + live state + evidence candidates
- produce budgeted prompt sections
- clip by ranked importance
- return token estimate and discarded sections

### Wave 2: Retrieval Wiring

Modify:

- `tools/skynet_brain.py`
- `core/hybrid_retrieval.py`
- `core/learning_store.py`
- `core/persistent_memory.py`

Tasks:

1. fully wire retriever dependencies instead of defaulting to BM25-only operation
2. add `context_packet()` or equivalent compact export functions
3. separate raw document retrieval from compact evidence extraction
4. prefer validated, reinforced, recent items over bulk text

### Wave 3: Episode And Procedure Layer

Create or extend:

- `tools/skynet_episode_log.py`
- `tools/skynet_procedure_store.py` or equivalent extension in `core/learning_store.py`

Tasks:

1. store one structured episode per completed task
2. distill repeated successful episodes into procedure records
3. tag procedures by domain and task class
4. expose `retrieve_procedures(goal, limit)`

### Wave 4: Context Refresh Upgrade

Modify:

- `tools/skynet_context_manager.py`

Tasks:

1. generate handoff summaries from structured state, not only last result text
2. store:
   - current task
   - next step
   - touched files
   - latest evidence references
   - validation status
3. re-inject compact handoff packet through the prompt compiler

### Wave 5: Metrics Loop

Modify:

- `core/self_evolution.py`
- dashboard or telemetry surfaces as needed

Track:

- average prompt size by task class
- memory hit rate by task class
- success per token
- refresh recovery rate
- retrieval usefulness
- percent of prompt occupied by high-value evidence

### Wave 6: Instruction Slimming

Use the existing audit in `data/worker_output/audits/agents_audit.md`.

Tasks:

1. keep only hard behavioral rules always in-context
2. move operational detail into tools and role-specific packs
3. stop loading giant universal instruction payloads when a compact role pack is enough

## Proposed Worker Split

- `alpha`
  - prompt compiler and dashboard metrics surfaces
- `beta`
  - retrieval wiring and backend context packet APIs
- `gamma`
  - episode/procedure distillation and memory policy
- `delta`
  - context refresh handoff logic and regression coverage

## Validation Plan

The upgrade is complete only when these are measurable:

1. average prompt size drops materially for repeated task classes
2. task success does not regress
3. refresh handoffs recover without replaying full history
4. retrieved evidence is smaller and more useful than current raw append-style context
5. memory-hit rate rises
6. quality-per-token rises

## Why This Will Help LLMs Do The Job Properly

Because the LLM will stop being used as:

- long-term memory
- project state database
- event log
- TODO store
- incident archive

and will instead be used for what it is good at:

- reasoning over the current task
- transforming the best available evidence into action
- generating useful outputs from compact, well-ranked inputs

That is the correct division of labor:

- Skynet stores, retrieves, filters, measures, and hands off state
- the LLM reasons over the minimal relevant slice

## Feasibility

This is high-complexity but feasible because the building blocks already exist in-repo:

- `tools/skynet_context_manager.py`
- `tools/skynet_brain.py`
- `tools/skynet_brain_dispatch.py`
- `core/hybrid_retrieval.py`
- `core/learning_store.py`
- `core/persistent_memory.py`
- `core/self_evolution.py`

The proposal does not require inventing a new subsystem from nothing. It requires connecting and hardening the memory, retrieval, handoff, and measurement layers that already exist.

## External Research References

- MemGPT: hierarchical memory / virtual context model
  - https://arxiv.org/abs/2310.08560
- Lost in the Middle: long prompts are not uniformly useful
  - https://arxiv.org/abs/2307.03172
- Long-context ICL evidence: similar examples matter more than undifferentiated bulk context
  - https://arxiv.org/pdf/2405.00200

## Recommended Priority

- `priority`: critical
- `difficulty`: adversarial-feasible
- `execution_mode`: multi-wave, measured rollout
- `owner`: orchestrator plus workers
- `consultant_role`: research, architecture, truth audit
