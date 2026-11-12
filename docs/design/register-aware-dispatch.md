# Register-Aware Subagent Dispatch

**Status:** design
**Issues:** #453 (this design), #452 (attentional re-entry protocol — sibling)
**Source:** philosophy-explore session [2026-03-31-1600](https://github.com/dcetlin/Lobster/blob/main/philosophy/2026-03-31-1600-philosophy-explore.md) — "The Externalized Juggle"
**Scope:** Design only. No dispatch-code wiring is changed by this UoW; the *Artifacts to change* section enumerates what a follow-up implementation UoW would touch.

---

## Problem

Lobster externalizes multi-dimensional attunement into parallel subagents (the 7-Second Rule, the dispatch pattern). The cost the philosophy thread names: the dispatch gap erases the *register* the subagent was operating in. A subagent doing philosophy-explore work is in an **exploratory register** — its job is to notice distinctions and follow a live gradient (Discernment / Attunement). A subagent resolving a GitHub issue is in an **execution register** — its job is to apply a known pattern to a named surface (Encoded-Insights / Embodiment). Today both are dispatched with structurally identical task prompts (`task_id` / `chat_id` / `source` frontmatter, `Minimum viable output`, `Boundary`) and both results arrive into the **same** reintegration process: text to be read and closed out.

This is *register erasure*. It produces results that are content-correct but orientation-wrong, and — critically — the failure is invisible at the resolution of content-processing. It only shows up when you ask, specifically, "was this result received as gradient-directional input, or as a closed deliverable?"

### Concrete wrong-register example

A UoW is dispatched: *"The harvester is dropping vision-inlet items under load — look into it."* The success criterion is loosely phrased. The dispatcher routes it like any other task. The subagent, receiving an execution-shaped prompt (`Minimum viable output: a fix`, `Boundary: do not exceed scope`), reads the prompt as *"produce a patch"*. It finds the first plausible cause — a queue-depth cap — patches it, opens a PR, and returns `outcome=complete`.

The patch is content-correct: the cap was real and the PR merges clean. But the task was **exploratory** — the live gradient was *"understand the load-shedding behavior of the inlet,"* of which the queue cap was one surface. An exploratory dispatch would have surfaced three candidate causes and a map of which load regimes trigger which, feeding the system's directional sense of the inlet. Instead the gradient was collapsed to the first artifact, the dispatcher reintegrated the PR as "done," and the orientation interest — *where is the inlet actually fragile?* — was silently dropped. Six weeks later the inlet fails a different way and the team re-discovers the gradient from cold start.

The orientation was correct at dispatch and lost at the gap. Nothing in the envelope carried the register, so nothing in the result handler could have treated it differently.

---

## Register encoding

### The hygiene finding that shapes the schema

A register field **already exists** in the codebase. `UoWRegister` (`src/orchestration/registry.py:148`) is a `StrEnum` whose own docstring reads *"Attentional register — determines executor type and completion evaluation policy."* Its four values already drive real routing:

| `UoWRegister` value | drives (today) |
|---|---|
| `operational` | `_select_executor_type` → `functional-engineer` (`steward.py:901`, `:1627`) |
| `iterative-convergent` | → `functional-engineer` |
| `philosophical` | → `lobster-meta`; special completion-eval hook (`steward.py:3946`) |
| `human-judgment` | → `lobster-generalist` |

So the design is **not** to invent a register concept. It is to (a) recognize that the existing `UoWRegister` is the *storage* form of register, (b) add the missing **operative collapse** that the dispatch layer and the result handler actually need, and (c) extend that collapse to the *general* (non-WOS) dispatch path, which has no register concept at all today.

### Three layers, deliberately distinct

```
ToL stage band   (4, conceptual)   Discernment · Attunement · Encoded-Insights · Embodiment
       │  collapse at the dispatch layer
       ▼
operative register (2, actionable)  exploratory  |  execution
       ▲  derived, never stored twice
       │
UoWRegister      (4, already stored) operational · iterative-convergent · philosophical · human-judgment
```

**Why two operative values, not four, at the dispatch layer.** The four ToL stages are a *developmental arc a system moves along over time*, not four ways to dispatch a task. A subagent is dispatched into an attentional *posture*, and there are exactly two postures that change prompt framing and reintegration: hold-the-gradient-open (exploratory) and converge-on-the-artifact (execution). Discernment and Attunement differ in *where on the arc* the system is, but both demand the same dispatch-time configuration: breadth, tolerance for open-endedness, deliverable-as-characterization. Likewise Encoded-Insights and Embodiment both demand convergence on a named artifact. Encoding four values at the dispatch layer would force the dispatcher to make a developmental-stage judgment it has no reliable signal for, to drive a distinction that has no downstream consumer. Two values is the form the dispatch layer actually requires; four would be imposed structure.

### The `register:` schema

The **operative register** is the dispatch-layer field. Enumerated value set:

```yaml
register: exploratory   # hold the gradient open — Discernment / Attunement
register: execution     # converge on the artifact — Encoded-Insights / Embodiment
```

Crosswalk (the canonical mapping a follow-up implementation encodes as `_REGISTER_TO_OPERATIVE`):

| `UoWRegister` (stored) | ToL stage band | operative `register:` |
|---|---|---|
| `operational` | Encoded-Insights → Embodiment | `execution` |
| `iterative-convergent` | Encoded-Insights (with an attunement tail) | `execution` |
| `philosophical` | Discernment → Attunement | `exploratory` |
| `human-judgment` | Discernment (judgment deferred to a human) | `exploratory` |

`iterative-convergent` is placed in `execution` because the *convergence loop itself* is execution toward a moving target; its exploratory tail is real but is not what the dispatch framing should optimize for. `human-judgment` is `exploratory` because the subagent's job is to *surface distinctions for a human to judge*, not to close them — premature convergence is exactly the failure mode.

### Determination rule — how the dispatcher derives register

The register is **never asked for by hand**; it is derived from signals the dispatcher already computes. There are two dispatch paths and each already has the discriminating signal in hand:

**WOS path (UoW dispatch via the Steward).** Derive `register:` from `uow.register` through the crosswalk above. This is a pure lookup — zero new stored field, zero new germinator classification. Refinement when `uow.register == operational` (the default, and therefore the noisy case): consult **success-criteria character**. If the success criteria are phrased as *produce a design / explore / map / surface / characterize* with no named output artifact, override to `exploratory`; if phrased as *implement / fix / merge / port / update `<artifact>`*, keep `execution`. `executor_posture` (`steward.py:2316`: `first_execution` | `continuation` | `remediation`) is a secondary signal: `remediation` and `continuation` against a stated `completion_gap` always lean `execution` (closing a known gap is convergent) regardless of the base register.

**General path (dispatcher spawns an `Agent` directly).** Reuse the **Mode Recognition classifier that already exists in `CLAUDE.md`** (the ACTION / DESIGN_OPEN gate). The mapping is one-to-one and requires no new logic:

| Mode Recognition result | `register:` |
|---|---|
| `ACTION` (named artifact, imperative verb, Bias to Action) | `execution` |
| `DESIGN_OPEN` (no statable output artifact, exploratory vocabulary) | `exploratory` |

This is the cleanest part of the design: the dispatcher *already* classifies every incoming message into exactly the two postures the operative register names. `register:` is the *serialization* of a decision the dispatcher already makes — not a new judgment.

---

## Subagent attentional configuration per register

The register changes **three concrete prompt elements** in the dispatch envelope. These are prepended to the existing preamble (the executor builds `preamble + wos_context + raw_instructions` at `executor.py:883`; the general path prepends to the Agent prompt). The `execution` framing is the **default** — it matches the current implicit behavior, so the common case adds zero tokens; only `exploratory` dispatches carry the extra block.

| Prompt element | `execution` (default — no added text) | `exploratory` (added block) |
|---|---|---|
| **Framing line** | *"You are applying a known pattern to a named surface. Produce the artifact."* (implicit today) | *"You are in an exploratory register. Your job is to follow a live gradient and surface distinctions — not to close them. A well-framed question, a map of the gradient, or a set of candidate readings is a valid and complete deliverable."* |
| **Breadth vs. convergence** | *"Converge. Pick the most defensible reading and proceed; do not return options."* | *"Hold breadth. Do not converge prematurely. If you find one answer, look for the two you are not seeing. Report the gradient, not just the endpoint."* |
| **Tolerance for open-endedness** | *"If the artifact is underspecified, choose the most defensible interpretation and implement it. Do not return questions."* | *"Open-endedness is expected. Returning a sharpened question or a characterization of where the system is fragile is success, not failure. Do not manufacture false closure."* |
| **`Minimum viable output` reshape** | a concrete artifact (file, PR, patch) | *"the most defensible **characterization** of the gradient" — a map, a distinction set, or a sharpened question* |
| **`Boundary` reshape** | *"do not exceed the stated scope"* | *"do not collapse the gradient into a single artifact; do not commit to one reading when several are live"* |

The `Minimum viable output` and `Boundary` fields are *required by the existing dispatch-template gate* — register does not add fields, it changes how those two existing fields are phrased. This keeps register from being a verbosity knob: an `exploratory` prompt is not *longer*, it is *attentionally configured for non-convergence*. The distinction is the orientation, not the word count.

---

## Result-handler routing table

Register's second job is to make reintegration **quality** conditional. Today every result flows through one path: `handle_wos_done` / `handle_wos_uow_completed` (`dispatcher_handlers.py:2281`, `:2512`) format a ping and close the UoW; the general path marks the message processed and relays. The register selects *which reintegration quality* the result is received with.

| operative `register:` | reintegration quality | concrete routing (directly implementable) |
|---|---|---|
| `execution` | **direct artifact integration** — the result is content; it either meets the success criteria or it does not | Current behavior, unchanged: `handle_wos_done` fast-path ping + HTML drilldown; PR enters the oracle merge gate; UoW → `Done`. Mark processed. No orientation re-inhabitation. |
| `exploratory` | **calibration / observation logging** — the result is *gradient input*, an anchor-point datum, not a closed deliverable | (1) Before reading the result body, read the dispatch-time gradient marker (the #452 coupling). (2) Route the result's `execution_summary` + `surprises` (already captured in the execution trace at `executor.py:230-288`) into observation logging via `write_observation` / `model_observe` as **anchor-point data** per the Theory-of-Learning structural finding. (3) Update the relevant vision/orientation anchor (`vision_routing.py`, `uow.vision_ref`) rather than marking a deliverable done. (4) Frame the completion ping as a *gradient update* — "this moved us toward X; the open direction is now Y" — not "task done." |

The `surprises` field already captured in the execution trace is the load-bearing reuse here: it is *exactly* the anchor-point data exploratory reintegration needs, and it is being thrown away today because the result handler has no register to tell it this result is gradient input.

Implementation shape: both `handle_wos_done` and `handle_wos_uow_completed` branch once on `operative_register(uow.register)`; `execution` returns the existing dict unchanged, `exploratory` additionally emits the observation/anchor writes before returning the (re-framed) ping.

---

## #452 coupling: attentional re-entry dependency

#452 asks for the minimum attentional marker that lets the dispatcher *re-inhabit the orientational gradient* of the original dispatch before processing a result as content. Its leading candidate is a `gradient:` field on the dispatch envelope (and the matching read-gradient-before-body protocol on arrival).

Register and gradient are **type and value**, and they compose:

- **`register:` is the type.** It tells the re-entry protocol *which reintegration quality to apply* — and, crucially, *whether to attempt orientation re-inhabitation at all*. Execution results do not need it; the re-entry protocol can skip the (non-trivial) cost of re-inhabiting orientation for them. Exploratory results require it.
- **`gradient:` is the value.** Within the exploratory quality, it tells the protocol *which specific direction* to re-inhabit — "the gradient was toward understanding inlet fragility, the live interest was toward load regimes."

What the #452 re-entry step needs to know about the dispatched register:

1. **Read `register:` first, before the result body and before `gradient:`.** It is the gate. `execution` → take the fast path, do not pay re-inhabitation cost. `exploratory` → proceed to step 2.
2. **For `exploratory` only, read `gradient:` next, still before the body**, and re-activate that orientation. This is the "minimal marker, not full reconstruction" #452 calls for.
3. **Then read the body**, received through the reactivated orientation.

So register makes #452's gradient-reentry **conditional and cheap**: without register, the protocol either pays re-inhabitation cost on every result (wasteful for execution work, which is most of the volume) or pays it on none (which is the cold-start status quo #452 is trying to fix). Register is the discriminator that lets gradient-reentry fire exactly where it earns its cost. The two issues are not parallel proposals; register is the gate that makes #452 affordable.

---

## Cost / hygiene note

**New-key-vs-fold decision.**

- **WOS path: fold, do not add.** The operative register is *derived* from the existing `uow.register` via a pure crosswalk. No new column, no migration, no new germinator classification step. The existing `UoWRegister` enum and `_REGISTER_TO_EXECUTOR_TYPE` table are the formulation that absorbs the new intent; we add a sibling `_REGISTER_TO_OPERATIVE` lookup and an `operative_register()` helper, nothing stored.
- **General path: one new frontmatter key, but zero new judgment.** The `register:` line is genuinely new in the `CLAUDE.md` dispatch-template frontmatter (that path has no register concept today). But it is the serialization of the ACTION / DESIGN_OPEN decision the dispatcher *already* makes. It adds a field, not a cognition.

**Token footprint.** The frontmatter line is ~6 tokens per dispatch. The `exploratory` framing block is ~50–70 tokens, paid only on exploratory dispatches (the minority); `execution` is the zero-added-token default because it matches current implicit behavior. The result-handler branch adds no tokens to the dispatch path.

**What it costs against the Embodiment ceiling.** The philosophy thread's sharpest point: the *telos* is internalized juggling — a system that no longer needs the scaffolding. Every context addition is a step *away* from the prompt-compressed-Embodiment ceiling, where the right behavior is so internalized it needs no instruction. `register:` is scaffolding. Its honest justification is not that it is free but that it makes a currently-*invisible* quality gap *visible* — and you cannot internalize a discrimination you cannot yet see. The field is a learning-phase instrument: it externalizes the exploratory/execution distinction so the system can observe itself getting it right or wrong, which is the precondition for eventually not needing the field. The mitigation that keeps the cost bounded: register is derived/reused on both paths (never a net-new judgment), and the common case (`execution`) carries zero added tokens. The field should be reviewed for retirement once the discrimination is reliably internalized — it is poiesis, not yet poiema.

---

## Artifacts to change

A follow-up *implementation* UoW (not this design UoW) would touch:

1. **`src/orchestration/registry.py`** — add `_REGISTER_TO_OPERATIVE: dict[str,str]` and an `operative_register(register) -> Literal["exploratory","execution"]` helper alongside the existing `UoWRegister` enum (`:148`). No enum change, no migration.
2. **`src/orchestration/steward.py`** — in/near `_select_executor_type` (`:1627`) and the prescription/preamble build (`generate_v2_prescription`, `~:2316`): derive operative register; when `uow.register == operational`, apply the success-criteria-character override; inject the register-specific attentional framing into the prescription `instructions`/`boundary` fields.
3. **`src/orchestration/executor.py`** — preamble build (`~:883`): prepend the `exploratory` attentional-configuration block when operative register is `exploratory`; pass register through to the trace (already carried at `:230-288`).
4. **`src/orchestration/dispatcher_handlers.py`** — `handle_wos_done` (`:2281`) and `handle_wos_uow_completed` (`:2512`): branch reintegration on operative register; for `exploratory`, route `execution_summary`/`surprises` to observation/anchor logging before returning the re-framed ping.
5. **`CLAUDE.md`** — the *Dispatch template* gate (Tier-1 Gate Register): add `register:` to the required frontmatter spec; document the ACTION/DESIGN_OPEN → register derivation and the two operative values.
6. **`.claude/sys.dispatcher.bootup.md`** — document register-aware reintegration on the general (non-WOS) result path: read `register:` (then `gradient:` per #452) before the body; route exploratory results to observation logging.
7. **`.claude/sys.subagent.bootup.md`** — document that a dispatched subagent should honor its `register:` (exploratory subagents may return a characterization/question as a complete deliverable).
8. **Coupling with #452** — when the `gradient:` field lands, the re-entry protocol reads `register:` first as the gate, then `gradient:` as the value (see #452 coupling section).

> **Note on the task's `vision.yaml` reference.** There is no `vision.yaml` in the repo. The actual vision artifacts are `src/orchestration/vision_routing.py` (`resolve_vision_route`) and the per-UoW `uow.vision_ref` dict; the `register` storage lives in the `UoWRegister` enum and the UoW row, not in a YAML file. The artifact list above cites the real files a follow-up would change.
