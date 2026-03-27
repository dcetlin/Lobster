# Oracle: Golden Patterns Register

The positive counterpart to `oracle/learnings.md`. Where learnings.md names failure modes, this file names structural wins — specific design decisions made in this codebase that have demonstrably worked or that carry high reusability.

Each entry is dated, evidence-grounded, and behaviorally specific. Generic software patterns are excluded. Entries must be traceable to a concrete decision in this system.

---

### [2026-03-27] Pattern: table-as-compaction-resistant encoding

**Pattern:** When a behavioral instruction must survive context compaction, encode it as a table row with explicit columns for trigger (one sentence), enforcement type (structural or advisory), and verifiable difference. The table format resists accumulation by design.

**Why it works:** Tables force the author to be specific about trigger conditions and outcomes. Prose can be vague; a table cell cannot. The format is scannable in a long document where prose would be skipped. Critically, each row is self-contained — partial compaction drops whole rows rather than corrupting semantics mid-paragraph. The format was identified as solving the exact contradiction of "adding context to address context-length problems."

**Where it appears:** `sys.dispatcher.bootup.md` — Tier-1 Gate Register, Oracle Pattern Register, Epistemic Hooks section. PR #8 compressed 78 lines of prose to 17 lines in table format without losing behavioral specification (commit 6131fbe).

**Reuse guidance:** Apply whenever a new behavioral instruction is being added to a bootup doc. If you cannot express the trigger in one sentence and the verifiable difference in one sentence, the instruction is not ready to be encoded. Use for: routing rules, gate criteria, self-check steps, agent dispatch criteria.

---

### [2026-03-27] Pattern: silent-drop sentinel via chat_id=0

**Pattern:** Use `chat_id=0` as a structured no-op signal meaning "this result is internal — do not relay to any user." The dispatcher checks `chat_id` before routing and drops silently when it equals zero.

**Why it works:** The sentinel is structurally enforced, not advisory. No deliberation is required at the relay layer — zero is zero. The dispatcher does not need to understand the task to know not to surface it. The convention extends naturally to any new result type that needs silent disposal: `agent_failed`, `cron_reminder`, and similar system messages all use the same signal.

**Where it appears:** `subagent_result` handler in dispatcher; documented in `sys.dispatcher.bootup.md` delegation section. Used by internal subagents that produce results not intended for user delivery (oracle writes, hygiene sweeps, scheduled job outputs).

**Reuse guidance:** Use `chat_id=0` for any subagent task that produces output the dispatcher should file or discard without routing to the user. Do not invent a new sentinel for each new internal task type — extend this one. The convention is already handled by the dispatcher's relay logic.

---

### [2026-03-27] Pattern: claim_and_ack as atomic claim-plus-acknowledge

**Pattern:** The `claim_and_ack` operation atomically claims a message from the inbox AND sends the typing acknowledgment in a single operation, preventing the race condition where a crash between claim and ack leaves a message claimed but unacknowledged.

**Why it works:** The two-step failure scenario (claim succeeds, ack crashes) produces a message that is stuck in processing with no user feedback and no retry path. By making claim and ack a single atomic operation, either both happen or neither does — no inconsistent intermediate state. This is structural enforcement of a correctness invariant rather than a behavioral instruction to "always remember to ack after claiming."

**Where it appears:** Dispatcher inbox processing loop; documented in `sys.dispatcher.bootup.md` delegation section. The operation is exposed as a single MCP tool rather than two separate calls.

**Reuse guidance:** Any two-step protocol where step 1 creates state that must be accompanied by step 2 for correctness is a candidate for this pattern. Ask: what happens if step 2 crashes? If the answer is "inconsistent state with no recovery path," collapse the two steps into one atomic operation.

---

### [2026-03-26] Pattern: crash-safe two-step delivery

**Pattern:** User-facing subagents deliver results via two sequential calls: `send_reply(task_id=...)` sends the message to the user; `write_result(sent_reply_to_user=True)` signals the dispatcher that delivery was completed. If the subagent crashes between the two calls, the dispatcher knows from the `task_id` signal in `send_reply` that delivery occurred.

**Why it works:** The naive single-step pattern (write_result with the reply text, let the dispatcher relay it) creates a failure mode where a dispatcher crash between receiving the result and relaying it silently drops the reply. The two-step pattern makes the subagent responsible for delivery — the most context-aware layer. The dispatcher acts as a deduplication guard, not the primary delivery mechanism. The `task_id` parameter in `send_reply` enables auto-suppression of duplicate delivery if `write_result` is later called with the same `task_id`.

**Where it appears:** `sys.subagent.bootup.md` — documented in detail as the canonical delivery pattern. Consistently implemented across all user-facing subagents: `lobster-generalist.md`, `review.md`, `brain-dumps.md`. The pattern has a dedicated "Internal vs. User-Facing Tasks" section.

**Reuse guidance:** Every subagent that sends a reply to the user should use this pattern. The only exception is purely internal subagents that use `chat_id=0` (see silent-drop sentinel pattern). When authoring a new agent definition, the question is: "is this task user-facing?" If yes, implement two-step delivery.

---

### [2026-03-26] Pattern: coherence-narrative basin as named production constraint

**Pattern:** Name "coherence-narrative generation" as a specific failure mode — producing fluent synthesis over lists of non-fitting observations — and encode it as a production constraint: "did the output resist synthesis or produce it?" This turns an abstract epistemic principle into an observable, checkable behavior.

**Why it works:** The failure mode is real and powerful: a model given a list of observations is strongly attracted to producing a synthesis that makes them cohere. Naming the attractor ("coherence-narrative basin") makes it observable as a specific thing to look for rather than a vague risk. The production constraint version — "resist synthesis; produce a list of things that don't fit the synthesis" — is checkable at output time. The lobster-meta epistemic posture uses this constraint explicitly.

**Where it appears:** `lobster-meta.md` epistemic posture section; `hygiene/sweep-context.md:19`; negentropic sweep instructions. The constraint is why lobster-meta is instructed to produce anomaly lists rather than synthesis narratives.

**Reuse guidance:** Apply whenever an agent is given accumulated signals and asked to synthesize. The natural production is synthesis; the correct production for pattern-detection tasks is often the opposite. Consider adding "resist coherence-narrative generation" as a named instruction to any agent whose job is to find what doesn't fit rather than to explain what does.

---

### [2026-03-26] Pattern: internal vs. user-facing task as two-mode subagent architecture

**Pattern:** Subagent result delivery is cleanly split into two modes: internal tasks (write_result only, no send_reply, user never notified) and user-facing tasks (two-step delivery via send_reply + write_result). The mode determines the entire delivery path.

**Why it works:** The dichotomy prevents a category of bugs where an internal task accidentally notifies the user, or a user-facing task silently succeeds without delivery. The mode is determined at task-spawn time, not at result-handling time, which means the dispatcher can route results correctly without inspecting their content. The architecture also enables a clean `chat_id=0` convention (see silent-drop sentinel): internal tasks signal their mode by using `chat_id=0`, making the delivery mode structurally encoded rather than advisory.

**Where it appears:** `sys.subagent.bootup.md` — "Internal vs. User-Facing Tasks" section (lines 101-126). All subagent definitions explicitly declare their delivery mode.

**Reuse guidance:** When defining a new subagent, the first design question should be: internal or user-facing? Answer this before writing the completion protocol. If the answer is "sometimes internal, sometimes user-facing" — make this a parameter at invocation time rather than a conditional at delivery time.

---

### [2026-03-23] Pattern: compression as architectural response to accumulation critique

**Pattern:** When an oracle review identifies "adding text to address text-length problems" as a structural contradiction, the correct fix is to compress the encoding — not remove the feature. PR #8 compressed 78 lines of behavioral prose to 17 lines in table format without losing any behavioral specification.

**Why it works:** Removing a feature because its encoding is too long is a false choice. The correct question is: what is the most compact encoding that preserves the behavioral specification? Table format is the answer for dispatcher step encoding — it resists accumulation by design, forces specificity at the trigger and outcome level, and is mobile-scannable. The compression is not aesthetic; it is architectural. A table row that states its trigger, enforcement type, and verifiable difference carries exactly the same behavioral specification as 4-5 lines of prose, but in a form that the dispatcher can scan and apply during a long session.

**Where it appears:** Oracle learnings.md (2026-03-23: "Pattern: compression as architectural response to accumulation critique"). Applied in PR #8 (commit 6131fbe). The Tier-1 Gate Register, Oracle Pattern Register, and Epistemic Hooks sections all use this encoding.

**Reuse guidance:** When an instruction feels too long but also too important to cut, ask: can the trigger be stated in one sentence? Can the verifiable difference be stated in one sentence? If yes, it can be a table row. If the answer to either question is "not yet," the instruction is not yet specific enough to encode — the vagueness is the real problem, not the length.

---

### [2026-03-27] Pattern: adversarial prior seeding before implementation review

**Pattern:** The oracle agent forms its Stage 1 vision-alignment finding before seeing the implementation. The prior entering any review is explicit: "this implementation is solving the wrong problem, or solving the right problem in a direction that forecloses better paths." This posture is locked in before the implementation commits the oracle to coherence with what was built.

**Why it works:** The builder's context is maximally committed to the coherence of their implementation. A reviewer who sees the implementation first will be pulled toward evaluating whether it is well-built, rather than whether it should have been built. By forming the Stage 1 finding first — and explicitly preventing it from changing after seeing the implementation — the oracle structurally enforces the separation between "right problem" and "well executed." Good implementation of the wrong thing is the failure mode this pattern exists to catch.

**Where it appears:** `lobster-oracle.md` — Stage 1 protocol. The two-stage structure (Stage 1 before seeing implementation; Stage 2 after) is the primary architectural feature of the oracle agent.

**Reuse guidance:** Apply to any review where the reviewer's prior should be independent of implementation quality. The pattern requires: (1) explicit adversarial prior stated before seeing the work; (2) Stage 1 findings written before seeing the implementation; (3) Stage 1 findings locked — they do not change after seeing the implementation. If Stage 1 findings are allowed to update after seeing the implementation, the separation collapses and you are doing a single-stage quality review.

---

*Entries should be added when: (1) an oracle decision receives an "Alignment verdict: Confirmed" with notable quality findings; (2) a negentropic sweep identifies an "undernamed gem" in the golden patterns section; (3) a reflection-systems review names a structural win specific to this codebase.*
