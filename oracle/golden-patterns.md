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

### [2026-03-29] Pattern: oracle vocabulary as detection precondition

**Pattern:** Reading `learnings.md` and `golden-patterns.md` before the substantive scan is not optional — the vocabulary determines what gets named, not just labeled. The bar is behavioral change: what would I have done differently without this pattern? If the answer is "nothing," the citation is a label, not a citation.

**Why it works:** Detection is vocabulary-dependent. An agent scanning for smells without named failure modes will find things but miss the pattern class they belong to. An agent scanning for golden patterns without named structural wins will produce vague descriptions instead of extending a recognized vocabulary. Reading the oracle files first locks in the detection vocabulary before the scan begins, which changes what gets flagged and how it gets encoded. The test is whether the oracle read constrained any specific decision — weighted a finding differently, caused a flag that would have been passed over, or prevented an inclusion.

**Where it appears:** `hygiene/sweep-context.md` Step 1 — oracle vocabulary is read before the detection pass begins. The explicit behavioral bar ("naming a pattern without stating its effect on your analysis is not a citation") was added to enforce this distinction.

**Reuse guidance:** Apply whenever an agent must detect pattern instances from a corpus. Read the pattern vocabulary first, before touching the corpus. After the scan, apply the behavioral-change test to every citation: "what did I do differently because of this pattern?" If no answer surfaces, the citation is cosmetic and should be removed or replaced with an honest description of what was found.

---

### [2026-03-29] Pattern: structural pre-scan before linear corpus read

**Pattern:** Before reading a large corpus linearly (issues, memory entries, files), run a structural pre-scan that buckets items by type, label, or source. Use the bucket distribution to build a priority queue. Read the priority queue, not the corpus.

**Why it works:** Linear reading of a large corpus (e.g., 100 issues, 3500 memory entries) has O(n) cost with no signal-to-noise improvement — every item is weighted equally. A structural pre-scan costs O(n) once but produces a sorted, filtered priority queue. Subsequent reading is O(k) where k is the number of high-signal items, which is typically much smaller. The pre-scan also surfaces flood conditions (e.g., 3500 health-check entries from one source) that would be invisible during linear reading until the reader is already buried.

**Where it appears:** `hygiene/sweep-context.md` Step 1b (Memory Pre-Pass) and Step 1c (Issue Pre-Scan), both added in Night 4 methodology improvement (2026-03-29). The issue pre-scan buckets by label and surfaces `needs-decision` items older than 14 days first. The memory pre-scan counts entries by event_type and greps for consumers before treating as signal vs. noise.

**Reuse guidance:** Apply whenever an agent reads more than ~20 items from a corpus. The structural pre-scan questions are: (1) what dimensions bucket this corpus? (labels, event_type, age, author, status); (2) what bucket distribution signals noise vs. signal? (single source dominating, no labels, very old); (3) what ordering within the signal bucket puts highest-value items first? Build the priority queue, then read it.

---

### [2026-03-30] Pattern: seam-first abstraction

**Pattern:** When building at current scale, choose the simplest implementation that works, but place named abstractions precisely at the seams where future change will be needed. The seam is identifiable from the grain of the design — it's where two systems with different evolutionary rates meet, or where the implementation backend may need to swap without touching the surrounding logic.

Applied in WOS Phase 2 design (2026-03-30):
- `workflow_artifact` struct: minimal fields now, but named — the Steward/Executor contract seam
- `evaluate_condition(uow)` callable: polling implementation now, but abstracted — the trigger evaluation backend seam

**Anti-pattern:** inlining logic at a seam because "it works now" — this makes future change a refactor rather than a backend swap.

**Distinction from over-engineering:** over-engineering adds abstraction layers for hypothetical futures. Seam-first adds one named interface at a point where the design's grain already indicates future flex will be needed. The test: "does the grain point here?" — not "might we ever need this?"

**Reuse guidance:** Before writing an implementation, identify the seams: where do two components with different evolutionary rates meet? Where might the backend swap without changing surrounding logic? Name those boundaries explicitly — a struct, a callable, a protocol — even if the implementation behind them is trivial today. The naming is load-bearing; it is what makes future change a backend swap rather than a refactor.

---

### [2026-04-24] Pattern: StrEnum constants in all enum-backed SQL and routing logic

**Three rules, all required:**

**Rule 1 — Always use StrEnum constants in SQL.** Raw string literals for enum-backed DB columns are forbidden. Import and use the enum; never write the string value directly in a query. `WHERE status = 'active'` is a latent bug; `WHERE status = UoWStatus.ACTIVE` is correct. A grep for the enum class surfaces all usages; a grep for the raw string catches some of them and misses others.

**Rule 2 — New modules must import enums at definition time.** When writing a new module that uses status/type values from a shared enum, import the enum before writing any code that uses those values. "Not in scope mentally" is not an excuse — the enum exists for type safety and maintainability. The correct pattern: import statement at the top of the file, then use the imported constant. The incorrect pattern: discover the value by looking at an existing string in another file and copy it as a literal.

**Rule 3 — PathSelection/type discriminators get StrEnum.** Any function that returns one of N fixed string choices (path selection, strategy names, mode strings) should define a StrEnum. Naked string returns have no place in internal routing logic. `StrEnum` values compare equal to their string counterparts, so the migration is backward-compatible: callers doing `if path == 'fast'` continue to work without changes.

**Why these rules together.** The three failure modes are related: raw SQL strings, raw literals in new modules, and naked routing returns are all the same underlying pattern — a value with a finite domain is being managed as an unbounded string. The fix is always the same: find the enum (or define one), import it, use it.

**Where these apply in this codebase:** PR #904 introduced `list_executing()` with raw SQL literals despite the surrounding `registry.py` using `UoWStatus` consistently (issue #925). `shard_dispatch.py`'s `select_path()` returns `'fast'`/`'thorough'` with no StrEnum backing (issue #926). Both are filed for fix.

**Reuse guidance:** Before committing any SQL query that filters on a status/type column, run: does the value being compared have an enum in this codebase? If yes, use it. Before merging any module that returns one of N fixed string choices, ask: should this be a StrEnum? The answer is almost always yes.

---

### [2026-04-25] Pattern: full-repo grep as precondition for dead-code removal

**Three rules, all required:**

**Rule 1 — Dead-code removal requires a full-repo grep across ALL Python files.** Searching only `src/` is insufficient. The required scope is: `src/`, `.claude/`, `scripts/`, `tests/`, `scheduled-tasks/`, `hooks/`. A grep limited to `src/` can miss imports in heartbeat scripts, scheduled tasks, hooks, and agent definitions that live outside the main package tree.

**Rule 2 — Run `grep -r 'module_name' ~/lobster/ --include='*.py'` and verify zero matches before filing any dead-code removal issue.** The grep must return zero results across the entire repo. Do not grep a subdirectory and extrapolate — the full repo command is the gate.

**Rule 3 — A single grep hit anywhere in the repo is sufficient to block removal.** One import in `scheduled-tasks/steward-heartbeat.py` or `hooks/` is as binding as ten imports in `src/`. Location outside `src/` does not make the usage less real.

**Why these rules together.** Issue #197 filed `dispatcher_handlers.py` for removal based on a grep scoped to `src/` and `.claude/`. The file was actively imported in `steward.py`, `shard_dispatch.py`, and `inbox_server.py` — files that were never searched. The failure mode is: an incomplete search produces a false "no usages" result, the issue is filed and acted on, and a live import breaks. All three rules exist to make that failure structurally impossible: the scope rule covers the search perimeter, the command rule makes the gate explicit, and the single-hit rule prevents rationalization ("it's only in a script, probably safe to remove").

**Where it applies in this codebase:** `src/orchestration/dispatcher_handlers.py` — issue #197 closed as "not planned" after the routing agent discovered the three live imports during WOS executor implementation (2026-04-25).

**Reuse guidance:** Before filing any issue to remove or rename a Python module, run: `grep -r '<module_filename_without_extension>' ~/lobster/ --include='*.py'`. If any results appear, the module is not dead code — stop. If no results appear, the module is safe to remove. This command is the gate, not a judgment call about whether the usages are "important."

---

*Entries should be added when: (1) an oracle decision receives an "Alignment verdict: Confirmed" with notable quality findings; (2) a negentropic sweep identifies an "undernamed gem" in the golden patterns section; (3) a reflection-systems review names a structural win specific to this codebase.*

---

### [2026-05-23] Pattern: binary confirm with pending-JSON and ruamel round-trip write

**Pattern:** When an agent proposes a structural change to a signed data file (e.g., vision.yaml), implement the accept/decline path as: (1) hash-keyed pending JSON for idempotent storage before dispatch; (2) binary Telegram inline keyboard (Accept / Decline buttons) as the Dan-gate; (3) ruamel.yaml round-trip loader for comment-preserving writes on accept; (4) append-only JSONL logs for accepted and discarded proposals. The eligible field scope is encoded as a frozenset anchored to a logged decision (od-1).

**Why it works:** The pattern cleanly separates the three concerns: proposal validation (against the live file), Dan confirmation (binary, no text input required), and durable write (comment-preserving, atomic via tmp-rename). Pending JSON keyed by hash is idempotent — repeated dispatch of the same proposal does not create duplicate buttons. The ruamel round-trip loader is the correct tool for YAML files where the comment block carries governance meaning (as vision.yaml's authority model header does). PyYAML's `yaml.dump` silently strips all comments, which destroys the governance record on first write. The JSONL audit trail (accepted and discard) makes every proposal traceable without any DB dependency.

**Where it appears:** `src/harvest/vision_inlet.py` introduced in PR #834 (Round 3 APPROVED). `dispatch_vision_proposals`, `handle_vision_accept`, `handle_vision_decline`, and `handle_vision_callback` implement the full pattern. Wired into `route_callback_message` in `dispatcher_handlers.py` via `CALLBACK_DATA_HANDLERS` frozenset and a startswith branch.

**Reuse guidance:** Apply whenever an agent needs to propose a change to a human-governed file and the change requires explicit human confirmation before write. The minimum required elements: (1) a hash-keyed pending store (JSON, atomic write via tmp-rename); (2) a binary button UI (not prose); (3) a comment-preserving write mechanism for the target file type (ruamel.yaml for YAML, not PyYAML); (4) append-only JSONL logs for both accept and decline paths; (5) eligible scope anchored to a logged decision — not inferred from the file's own authority model header. The authorized scope must be in code (a frozenset), not in documentation.

---

### [2026-04-22] Pattern: caller-declared identity in shared utilities

**Pattern:** When consolidating copy-pasted functions into a shared utility, add a `job_name` (or equivalent identity) parameter at the call site rather than hardcoding the prefix inside the shared function or inspecting the call stack. Each caller passes its own stable identifier literal, and the shared function uses it as-is.

**Why it works:** The alternative — letting the shared function hardcode a generic prefix or inspect call stack frames — produces opaque message IDs not traceable to origin. Caller-declared identity makes the origin explicit in the function signature, observable in every output (msg_id prefix), and searchable in logs without reading source code. It also makes the shared function a pure transformation: given the same inputs, it produces the same output. No hidden coupling between the function's behavior and the script that calls it. The `job_name` parameter was the distinguishing design choice in PR #805's consolidation: six scripts each passed their own literal, and every inbox message ID is now prefixed with the originating script name.

**Where it appears:** `src/utils/inbox_write.py` — `write_inbox_message(job_name, chat_id, text, timestamp)` introduced in PR #805. All six callers pass a stable literal: `"auto-router"`, `"surface-queue-delivery"`, `JOB_NAME` constant.

**Reuse guidance:** Apply whenever extracting a utility function that produces identifiable outputs (IDs, log entries, file names). The identity should flow in as a parameter, not be inferred inside the function. Test: if you grepped every output of this function for its job origin, would the origin be unambiguous without reading source? If yes, the pattern is applied correctly.

---

### [2026-03-30] Pattern: importlib re-export bridge for backward-compatible file splits

**Pattern:** When splitting a monolithic script into two files (concern A in file-A.py, concern B stays in file-B.py), use `importlib.util.spec_from_file_location` at module scope in file-B.py to load file-A.py and bind its exported symbols as top-level names in file-B.py. Register the loaded module in `sys.modules` before `exec_module` to prevent double-execution and ensure `@dataclass __module__` resolution works correctly.

```python
import importlib.util as _ilu
_SWEEP_PATH = Path(__file__).parent / "startup-sweep.py"
_sweep_spec = _ilu.spec_from_file_location("startup_sweep", _SWEEP_PATH)
_sweep_mod = _ilu.module_from_spec(_sweep_spec)
sys.modules["startup_sweep"] = _sweep_mod
_sweep_spec.loader.exec_module(_sweep_mod)
run_startup_sweep = _sweep_mod.run_startup_sweep
StartupSweepResult = _sweep_mod.StartupSweepResult
```

**Why it works:** Tests that load file-B.py via importlib and access symbols by name continue to work without modification after the split. The re-export makes the file split invisible to callers — their import path (load heartbeat, access sweep symbols) still resolves correctly. Registering in `sys.modules` before `exec_module` is critical for `@dataclass` forward reference resolution. The failure mode for a missing file-A.py is a hard `FileNotFoundError` at import time, which makes the deployment dependency explicit rather than producing silent misbehavior.

**Where it appears:** `scheduled-tasks/steward-heartbeat.py` lines 62–72 (PR #358). The split extracted startup sweep logic from the heartbeat; the re-export bridge preserved the test surface without requiring test modifications.

**Reuse guidance:** Apply when splitting a scheduled-task script that is loaded by name via importlib in tests. The bridge belongs at module scope near the top of the file (after path setup, before the module's own logic). Note: this pattern introduces a deployment coupling — file-B requires file-A to exist at the expected relative path. Document this coupling in the module docstring. Do not use for src/ package modules where normal relative imports are available — importlib bridging is a script-context workaround, not a general-purpose import pattern.

---

### [2026-04-04] Pattern: classification result as typed frozen dataclass with observability fields

**Pattern:** When a classification function can produce systematically wrong results (all heuristic classifiers can), return a typed frozen dataclass that carries not just the classification but also `gate_matched`, `confidence`, and `rationale`. The classification is immutable; the observability fields are first-class outputs.

**Why it works:** A raw register string returned from `classify_register()` gives callers no information about *why* that classification was made or how confident the classifier was. A frozen `RegisterClassification(register, gate_matched, confidence, rationale)` gives the caller everything it needs to log, surface, and audit classifications without re-running the classifier. `confidence="low"` (the default-fallback case) is a structured signal that the Steward can use to flag UoWs for human review. `gate_matched` tells the observability layer which code path fired without requiring a debugger. The frozen/slots combination prevents mutation after the fact, which is correct for an immutable germination-time decision. This structure surfaces systematic misclassification without requiring per-item examination.

**Where it appears:** `src/orchestration/germinator.py` — `RegisterClassification` dataclass and `classify_register()` return type. Introduced in PR #602 (WOS V3 Germinator). The dry-run path in `cultivator.py` logs all four fields, enabling batch inspection of classification quality before promotion.

**Reuse guidance:** Apply to any classification function that (a) can misclassify and (b) whose misclassification rate matters. The minimum set of observability fields: `result` (the classification), `gate_matched` or `rule_matched` (which branch fired), `confidence` (high/medium/low), `rationale` (one sentence). Do not return bare strings from classifiers that operate on real data — the string is undebuggable at scale. The frozen dataclass is the return type; the string value is extractable in one attribute access.


---

### [2026-04-20] Pattern: self-applying Stage 2 check as structural correctness test

**Pattern:** When adding a new named check to the oracle's Stage 2 criteria, apply the check to the PR that introduces it. If the check passes reflexively (the PR meets the criterion it is adding), the check is correctly scoped. If the check does not pass reflexively, either the criterion is over-broad or the PR introducing it is itself underanchored.

**Why it works:** Any oracle criterion that cannot be satisfied by the PR introducing it is structurally suspect — it either imposes requirements the codebase hasn't yet met, or it is poorly specified. The self-applying test catches both failure modes at authoring time rather than at first application to a downstream PR. For the Encoded Orientation check (PR #797): the PR encodes a new behavioral orientation into lobster-oracle.md; the check fires on the PR itself; the check passes (vision.yaml constraint-3 is the anchor, "or equivalent" covers the prior decision); the check is correctly scoped.

**Where it appears:** PR #797 — Encoded Orientation check added to lobster-oracle.md Stage 2. The check applies to "agent definitions, bootup files, gate tables, vision.yaml, CLAUDE.md" — and the PR itself modifies an agent definition, triggering the check.

**Reuse guidance:** Apply when authoring any new Stage 2 named check for the oracle. Before writing the check into a PR, ask: does this PR satisfy the check I'm adding? If yes, proceed. If no, either (a) the criterion is over-broad and needs refinement, or (b) this PR is itself the example of what the criterion is meant to catch — in which case it should not be the PR that introduces the criterion.


---

### [2026-04-20] Pattern: epoch-scale normalisation as an isolated seam function

**Pattern:** When consuming a numeric timestamp from an external system that may use either seconds or milliseconds, isolate the epoch-scale detection in a named function (`_normalise_timestamp`) that accepts the raw value and returns a canonical seconds float. The detection logic is a simple magnitude threshold (`> 1e11`). All downstream callers use the normalised output.

**Why it works:** Epoch-scale ambiguity (seconds vs. milliseconds) is a recurring defect surface for any system that consumes timestamps from Node.js/Electron producers (which use `Date.now()` in milliseconds). Isolating the detection into one function means: (a) the threshold is defined in one place; (b) tests can exercise the normalisation independently; (c) all callers get the correct scale without each implementing the threshold check. The alternative — inline `if ts > 1e11: ts /= 1000` at each call site — passes code review but creates multiple divergence points when the threshold or logic changes. The seam-first pattern from golden-patterns.md applies here: the normalisation function is a minimal abstraction placed precisely at the boundary where two systems with different epoch conventions meet.

**Where it appears:** `src/orchestration/steward.py` — `_normalise_timestamp()` introduced in PR #800. Threshold constant `_MILLISECOND_TIMESTAMP_THRESHOLD = 1e11` defined at module scope. Called by `_check_token_expiry` for int/float `expiresAt` values.

**Reuse guidance:** Apply whenever a system reads a timestamp from any external source (credential files, API responses, webhooks) where the epoch scale is not contractually guaranteed. The threshold `1e11` is the correct choice: Unix seconds will not exceed `1e11` until year 5138; any current Unix milliseconds value is already above `1e11`. Document the threshold in a module-level comment with the rationale (as PR #800 does). Import this constant in tests rather than re-declaring it locally.

---

### [2026-04-22] Pattern: advisory-to-hook promotion with explicit non-promotable boundary

**Pattern:** When an advisory gate in the Tier-1 Gate Register is compaction-vulnerable (the dispatcher may forget the rule after context compaction), promote it to a PreToolUse hook if and only if the gate's enforcement condition is a string pattern or field check requiring no semantic judgment. The non-promotable boundary is explicit: gates requiring semantic judgment (Relay filter, Design Gate, Bias to Action) cannot be promoted because no string pattern reliably captures rhetorical priority or message intent. Document the non-promotable gates and the reason in learnings.md at the same time the promotion is made.

**Why it works:** Compaction-vulnerable advisory gates fail precisely when the system is under most context pressure — which is also when incorrect subagent dispatch or premature merges are most likely. A PreToolUse hook fires regardless of context state; the check is mechanical, not memory-dependent. The distinguishing test is whether the gate condition can be expressed as a string search on `tool_input["prompt"]` or `tool_input["command"]` without false positives from legitimate use cases. Both promoted gates (Dispatch template, PR Merge Gate) satisfy this test: the required fields are named strings, and the PR number is an extractable integer. The gates that fail the test (semantic judgment required) remain advisory — attempting to promote them would generate false positives that erode trust in the hook system.

**Where it appears:** `hooks/dispatch-template-check.py` and `hooks/pr-merge-gate.py` (PR #825). The learnings.md addition in the same PR names the non-promotable gates and their reason. `scripts/upgrade.sh` Migrations 80 and 81 install the hooks on existing instances with idempotency guards.

**Reuse guidance:** Before promoting any advisory gate to a hook: (1) State the gate's enforcement condition as a string pattern in one sentence. (2) Ask: does any legitimate use of the tool produce a false positive? If no → promote. If yes → refine the pattern or keep advisory. (3) Write the non-promotable reasoning at the same time — what the hook does not check is as important as what it does. (4) Add idempotency guards to the migration using the command string as the uniqueness key, consistent with the existing upgrade.sh hook registration pattern.

---

### [2026-04-23] Pattern: slow-section carry-forward in overwrite-snapshot files

**Pattern:** When a shared memory file is written by multiple agents using a full-overwrite pattern (to prevent unbounded accumulation), designate one section as "Stable Context" — content that changes rarely (infrastructure, contacts, long-term goals) — and carry it forward verbatim from the prior file. All other sections are synthesized fresh from current signals. This prevents the overwrite pattern from silently discarding slow-moving context that no single run's signals would reconstruct.

**Why it works:** A pure overwrite loses any context not visible in the current run's signal window. A pure carry-forward accumulates stale content indefinitely. The slow-section designation is the correct partition: sections that reflect current state (open PRs, recent decisions, active threads) are re-synthesized each run; sections that reflect stable facts (who the contacts are, what infra is running, what the long-term goals are) are carried verbatim unless the current run's signals explicitly change them. The partition is explicit and named — agents know exactly which section survives rewrites and why.

**Where it appears:** `compact-catchup.md` Phase 3 (step 14) and `nightly-consolidation.md` step 3, both updated in PR #892. The "Stable Context" section is the designated slow section in both agents.

**Reuse guidance:** Apply whenever a memory file is shared between multiple agents using an overwrite pattern. Identify the sections by update rate: (1) fast-moving sections (current work, recent decisions, active threads) — re-synthesize each run; (2) slow-moving sections (contacts, infra, long-term goals) — carry forward verbatim. Name the slow section explicitly in the file's structure and in the agent instructions. Require that both agents use the same section names and the same carry-forward rule — asymmetric rules between agents produce structural variation in the file that downstream readers must handle.

---

### [2026-04-25] Principle: canonical placement must be obvious from first encounter

**Principle:** The right home for any file, module, or definition must be unambiguous without interpretation. If you add something and the correct location is not immediately obvious to someone reading the repo for the first time, the organizational structure has failed — not the reader. When you're about to add something, ask: "Is the right home for this unambiguous?" If the answer requires judgment, context, or familiarity with history, restructure first. Never add to a structure that would require explanation.

**The anti-pattern:** Placing a file "close enough" to where it belongs and relying on contributors to figure out the intent. This compounds. One ambiguous placement generates a second ambiguous reference to the first, then a third that mirrors the second. The confusion isn't localized — it propagates forward and creates friction in every future decision made in its vicinity.

**The staleness problem is the same problem:** A file that used to be canonical and no longer is — because its function moved, its name no longer matches its contents, or its location was never updated when the surrounding structure changed — is not just dead weight. It is active misdirection. A stale canonical home is worse than no canonical home because it answers the question "where does X live?" with the wrong answer confidently.

**The test:** Could a new contributor, given only the repo structure and file names, land on this file without help? If not, the structure owes them a correction — not documentation explaining the deviation.

**Where it applies:** Every file addition, every module split, every directory reorganization. The question "is the right home unambiguous?" is a pre-commit check, not a post-hoc audit.

**Reuse guidance:** Before adding any file: (1) state the home in one sentence using only the directory and file name; (2) ask if someone new would agree without being told; (3) if no — restructure. Before renaming or moving anything: check whether existing references to the old location would mislead rather than break (breaks are loud; misdirection is silent). The goal is a repo where organizational structure never generates a question that requires a human to answer.

**Documentation is not a fix:** The urge to write a comment or README saying "X lives in Y" is a signal the structure failed, not a solution. Document the why; never the where. If you need to explain where something lives, the location is wrong.

**No split canonical homes:** If something could plausibly live in two places, that ambiguity is the structural problem. Pick one, encode the convention. The existence of a reasonable second option means the first isn't obvious enough.

**Grounded in:** Single Source of Truth (one authoritative home; duplication is structural debt); Principle of Least Surprise (contributors find things where they expect, not where they were convenient to place); self-documenting structure (the repo layout answers "where does X live?" without external documentation).

---

### [2026-04-25] Pattern: named display cap with aggregate-preserving header for high-frequency context payloads

**Pattern:** When a list is prepended to a high-frequency context payload (e.g., every `wait_for_messages` call), cap the per-entry detail at a named module-level constant while preserving the true aggregate count in the header. Append an "...and N more" overflow hint so the consumer retains structural awareness (total count) without paying per-entry token cost. The cap constant is module-scope and directly importable by tests.

**Why it works:** The two failure modes this pattern avoids are: (a) hiding the aggregate fact (how many total) which would degrade routing decisions; (b) letting per-entry cost grow O(n) with agent count, which compounds context pressure at exactly the time the system is most loaded. Preserving the aggregate in the header is the load-bearing design decision — without it, the dispatcher cannot distinguish "5 agents (all shown)" from "12 agents (7 hidden)." The named constant makes the cap observable in tests without re-declaring a local mirror (which would create a divergence risk per learnings.md PR #800).

**Where it appears:** `src/agents/session_store.py` — `_ACTIVE_SESSIONS_DISPLAY_CAP = 5` and `format_active_sessions_block()` introduced in PR #938. The header computation runs before the slice, preserving true total count. Tests import the constant directly.

**Reuse guidance:** Apply whenever a list is formatted into a payload that fires at high frequency (per-poll, per-heartbeat, per-WFM). The structure is: (1) compute aggregate header from the full list; (2) slice to cap; (3) render sliced entries; (4) append overflow hint iff hidden > 0. User-facing entries go first so the highest-signal entries survive truncation. The cap constant belongs at module scope so it is importable by tests — never re-declare it locally in a test file.

---

### [2026-05-03] Pattern: design-spec-blockquote as authority-status declaration

**Pattern:** When a specification document describes a format or behavior that does not yet exist in production, place a visually distinct blockquote at the top of the document (before the introduction, after the document header) that explicitly states: (a) this is a design specification, not a description of current production behavior; (b) what the current production format or behavior is, with a pointer to the authoritative source file; (c) which implementation issues will close the gap between this spec and production; (d) a direct instruction for any agent using this document as orientation — where to look instead.

**Why it works:** A design spec co-existing with a divergent production implementation is a latent misconfiguration risk. Any agent that reads the spec as orientation will form a model of the system that does not match what the system does — and that model will propagate into prescriptions, issue descriptions, and dispatch decisions. The blockquote is machine-visible (it is rendered differently from prose in markdown), is read before any technical content, and names both the gap and the authoritative source in one place. The four-element structure (status, current source, implementation track, orienting instruction) closes all four failure modes: (1) reader does not know the spec is a design doc; (2) reader does not know what the current format is; (3) reader does not know when the gap will close; (4) orienting agent does not know where to look for the current authoritative format. Omitting any element leaves one failure mode open.

**Where it appears:** `docs/prescription-format-spec.md` — the blockquote added in PR #1053 Round 2 fix (commit dd0bcaed) addresses the Round 1 Gap 1 finding that the spec's "Effective: 2026-05-03" framing implied production readiness. The blockquote names `WorkflowArtifact`, `src/orchestration/workflow_artifact.py`, and issues #574, #576, #577, #578.

**Reuse guidance:** Apply to any design document that specifies a format, API, or behavior that is not yet implemented in production and where an agent could plausibly read the document as orientation. The blockquote must be placed before the document's own introductory prose — after the header frontmatter, before the "Introduction" or "Overview" section. The four elements are all required; a blockquote with only a status label but no current-source pointer or implementation track does not close the orientation gap. Do not rely on the document version number alone (e.g., 0.1.0 vs 1.0.0) to signal design-spec status — version numbers are not self-explanatory to a disoriented reader.

---

### [2026-05-18] Pattern: precondition-as-harness in declarative sweep instructions

**Pattern:** When authoring a sweep or diagnostic instruction, encode the epistemic preconditions as named, mandatory harness sections that must be completed before any domain-specific detection logic runs. Each harness section names the failure class it prevents, specifies a checkable enforcement rule, and provides a concrete enforcement example. Sections that genuinely do not apply must state "N/A — [reason]" explicitly rather than being silently omitted.

**Why it works:** Sweep failures caused by silent-contract-violation and missing-state-registry-read are not logic errors — they are missing preconditions. A detection pass that runs before verifying its contract assumptions will produce confident readings that are structurally incorrect. Encoding the preconditions as named, required sections in the instruction document makes them a checklist item for every sweep author and reviewer, not a tacit assumption that gets violated silently on the first run. The harness sections map to named smells in the registry, making the failure class traceable from symptom (false escalation) to cause (missing harness) to prevention (harness section). This converts an abstract epistemic principle into a checkable, auditable behavior — the same transformation applied in the coherence-narrative basin pattern.

**Where it appears:** `memory/canonical-templates/sweep-instruction.template.md` introduced in PR #1188. Three mandatory harness sections: Dependencies (contract-declaration pattern, prevents silent-contract-violation), Operational Posture (state-registry-read, prevents missing-state-registry-read), Classification States (elimination-completeness invariant, prevents binary-classifier coherence failure).

**Reuse guidance:** Apply to any new sweep or diagnostic instruction. The three harness sections are a minimum baseline — they address the two named failure classes known as of PR #1188. If a sweep introduces a new external dependency type not covered by the Dependencies section, add a row to the Dependencies table. If a sweep classifies a state space that includes additional legitimate no-activity states, add those states to the Classification States section. The template is a starting point, not a ceiling. Cross-reference to named smells in `oracle/smell-patterns.yaml` when a harness section directly prevents a named failure class — the traceability is load-bearing for future sweep audits.

---

### [2026-06-03] Pattern: two-signal steward discriminator (specificity + TTL)

**Pattern:** When a steward must route a pre-metabolic artifact (juice, a frontier thread, an uncrystallized direction), use a two-signal gate: Signal 1 — qualitative specificity test (can the steward reconstruct the gradient from the signal alone?); Signal 2 — temporal TTL check (is the artifact within the default TTL, or does it map to an active open thread?). Both signals must pass for the artifact to be treated as live.

**Why it works:** A single-signal gate (specificity only) misses artifacts that were once specific but have since been answered. A single-signal gate (TTL only) misses dead letters that are within the window but contain generic signals. The two-signal conjunction eliminates both failure modes. The decision rule on failure is also explicit: archive, do not dispatch a UoW (dispatching a UoW to resume a dead thread converts heat into more heat). The pattern encodes the discrimination function in terms the steward can apply without judgment overhead.

**Where it appears:** `philosophy/frontier/metabolic-juice.md §Resolution §(3) Steward Discriminator — Juice vs. Dead Letter`. Introduced in PR #1403 (uow_20260522_bf4a96).

**Reuse guidance:** Apply whenever a steward or agent must distinguish live generative threads from dead letters in a pre-metabolic context. The specificity test (qualitative) and TTL test (temporal) are complementary axes. Extend to new contexts by replacing "heat_signal" and "14-day TTL" with the domain-appropriate signal name and threshold, but preserve the two-signal conjunction and explicit archive-on-failure rule.

---

### [2026-06-03] Pattern: bulk gh CLI operation with (success_count, failure_count) return and per-item fault isolation

**Pattern:** When a function must apply a gh CLI operation to a list of N items, structure it as: (1) one-time precondition setup before the loop (e.g., label ensure); (2) a try/except per item catching both `CalledProcessError` and `Exception`; (3) accumulate `success_count` / `failure_count` without aborting the loop on any single failure; (4) return `(success_count, failure_count)` as a tuple; (5) never raise. Surface the tuple to the caller for operator feedback.

**Why it works:** A single GitHub API failure (rate limit, transient error, label not found) must not abort the remaining N-1 operations. The tuple return gives the caller actionable signal (how many succeeded, how many failed) without requiring the function to make a decision about what the caller does with partial success. The one-time precondition setup before the loop avoids N redundant API calls for shared preconditions (e.g., label existence check). The "never raises" contract means callers never need to wrap in try/except.

**Where it appears:** `bulk_swap_executing_to_paused` and `bulk_swap_paused_to_executing` in `src/orchestration/wos_issue_lifecycle.py`, introduced in PR #1425. Both functions follow this structure identically.

**Reuse guidance:** Apply whenever a function must apply a gh CLI operation (or any external side-effecting call) to a list of items where partial success is acceptable and visibility into partial failure is required. The one-time setup step (before the loop) is load-bearing — move anything that is invariant across iterations out of the loop body. The per-item except-Exception catch is not optional: gh CLI failures sometimes arrive as `subprocess.CalledProcessError`; network and OS failures arrive as other exception types. Both must be counted, not propagated.

---

### [2026-06-03] Pattern: named constants dict + fallback constant for per-type dispatch configuration

**Pattern:** When a dispatcher must apply different configuration values (turn caps, timeouts, model names) per executor type, encode the map as a module-level dict (`_CONFIG_BY_TYPE: dict[str, T]`) and a separate fallback constant (`_FALLBACK_CONFIG: T`). Resolution logic is a single expression: `artifact_value if artifact_value is not None else _CONFIG_BY_TYPE.get(executor_type, _FALLBACK_CONFIG)`. Both constants are importable by tests without re-declaring values locally.

**Why it works:** A hardcoded literal at the dispatch site is a magic number — invisible to search and inert to type checking. A module-level named constant is importable, grep-able, and documentable. The separate fallback constant makes the "unknown type" path explicit rather than silent (a `.get()` with no default silently returns `None`, which the caller must then interpret). The three-level resolution (artifact override → type default → global fallback) is a clean precedence chain with no ambiguous cases.

**Where it appears:** `_DEFAULT_MAX_TURNS` and `_FALLBACK_MAX_TURNS` in `src/orchestration/executor.py`, introduced in PR #1426.

**Reuse guidance:** Apply to any dispatch configuration that varies by executor type. The three-level precedence chain (artifact-level, type-level, global fallback) is the canonical extension point — adding a new executor type requires only a new key in the dict. The artifact-level override gives operators per-UoW control without code changes. Do not use a function-local dict for this pattern — the values must be importable by tests to avoid mirror-constant risk.

---

### [2026-06-05] Pattern: sidecar artifact file for large inbox message fields

**Pattern:** When an inbox message grows large due to a small number of embedded blobs (issue bodies, logs, context strings), strip those blobs to a JSON sidecar file keyed by a stable unique ID (e.g., `uow_id`), store the file path as a scalar field in the inbox message, and have the consumer read the sidecar at consumption time. Keep the inbox message under 2KB with scalar routing fields only.

**Why it works:** The inbox message's job is routing and identification, not payload transport. When large blobs are embedded inline, any system that reads the inbox message — including the dispatcher's message loop and any monitoring or analysis tool — pays the full payload cost on every read. Separating blobs into a sidecar means the routing layer (dispatcher) pays only the cost of the path reference; the consuming layer (subagent) pays the content cost. For dispatcher architectures where message processing happens synchronously in a blocking loop, this is a structural correctness guarantee, not merely an optimization: the loop's latency is now bounded by Python I/O + string ops, not by LLM inference over large context.

The None-sentinel return from the reader function (`return None` when file missing) is the correct backward-compat mechanism: the consumer checks `if artifact is not None` and falls back to inline fields for old-shape messages. This eliminates the need for a migration or a cut-over deployment.

**Where it appears:** `src/orchestration/prescribe_artifacts.py` introduced in PR #1433. `LARGE_FIELDS` frozenset + `PRESCRIBE_ARTIFACTS_DIR` Path constant at module scope (importable by tests). `write_prescribe_artifact` / `read_prescribe_artifact` are the two pure-function I/O operations. Wired into `steward._write_prescription_request` (writer) and `dispatcher_handlers.handle_wos_prescribe` (reader).

**Reuse guidance:** Apply when (a) an inbox message contains blobs whose total size exceeds ~5KB; (b) the consumer of the message is the dispatcher's synchronous loop (blocking latency matters); (c) the blobs are written by a producer that can be modified at the same time as the consumer. Key implementation decisions: (1) key the sidecar by a stable unique ID, not a timestamp — UoW IDs are stable across retries, timestamps are not; (2) return `None` (not raise) from the reader when the file is missing — this is the backward-compat path for in-flight messages; (3) re-raise on parse errors (corrupt file) — a corrupt sidecar is a real error, not a backward-compat case; (4) provide an `artifact_dir` override parameter on both writer and reader for test isolation; (5) store the path as a string field in the inbox message, not as a content hash or relative path — absolute paths are unambiguous and the consumer can check existence directly.

---

### [2026-07-04] Pattern: full-repo-grep and canonical-placement patterns generalize from code to content deletion

**Pattern:** The two existing patterns "full-repo grep as precondition for dead-code removal" and "canonical placement must be obvious from first encounter" apply unchanged when the thing being deleted is narrative/state content (markdown, JSON, HTML) rather than a Python module. The grep target changes (path string instead of module name) but the rule is identical: a full-repo, all-extension grep for the path being removed, run on both the target branch and main, is the gate before any bulk content-directory deletion — and a directory that could plausibly live in either the repo or the workspace is itself the defect the deletion is meant to fix.

**Why it works:** PR #1464 removed 6 repo-tracked workstream content directories (`workstreams/{agent-council,ergonomics-orient,html-interface,sensibility-stack-v3,violin-synthesis,wos}`) after migrating their content additively into the canonical `~/lobster-workspace/workstreams/<name>/` location. The PR body asserted "none of the deleted files were referenced by any code path" without showing a grep transcript — a self-report, not evidence. Running the grep independently (across `src/`, `scripts/`, `hooks/`, `.claude/`, `scheduled-tasks/`, `tests/`, and the whole-repo markdown/json/yaml corpus, on both `main` and the PR branch) confirmed the claim: every live path reference already pointed at `~/lobster-workspace/workstreams/...`, never the repo path being deleted. The two collision files (created when a repo-fork filename matched an existing workspace filename) were verified byte-identical to their `.repo-fork`-suffixed sidecars before treating the migration as complete — this is the content-deletion analog of "diff every non-obvious case before trusting the migration."

**Where it appears:** PR #1464, oracle verdict `oracle/verdicts/pr-1464.md` (Round 1, APPROVED). Traces to `assessments/reorg-action-list-20260704.md` item 13 and CLAUDE.md's pre-existing "Key Directories" / "Workstreams Convention" / "Project Directory Convention" sections, which already stated the rule this PR enforces.

---

### [2026-07-04] Pattern: advisory-to-hook promotion generalizes to filesystem-move side effects, with a reserved-name denylist caveat

**Pattern:** The 2026-04-22 "advisory-to-hook promotion" pattern (promote a CLAUDE.md preamble step to a hook if and only if its condition is a string/field check requiring no semantic judgment) applies cleanly to a filesystem *move*, not just a routing check. PR #1467's auto-archive-on-completion moved `workstreams/<task_id>/` → `workstreams/archive/<task_id>/` out of the CLAUDE.md Long-Running Dispatch Preamble (step 6, "documentation, not enforcement") into `hooks/require-write-result.py`, an already-firing `SubagentStop`/`Stop` hook — the condition (`task_id`, `status` on a `write_result` tool call) is a pure field check, satisfying the promotion test exactly. The move is additive to the hook's existing success branch: no new exit path, no change to the pre-existing `chat_id`-rejection or no-`write_result` retry branches.

**Why it works:** Filesystem-move side effects (unlike the string-pattern-match hooks the original pattern was written for) carry a new risk class the original pattern didn't have to name: the move target can collide with a reserved sibling name inside the same parent directory. Here, a `task_id` slug that happens to equal `"archive"` — the reserved destination subdirectory's own name — makes `src` and the archive root resolve to the same path. Verified directly: `shutil.move` raises on "move directory into itself," caught by the function's blanket exception handler, producing a safe no-op — but this safety is incidental (an incidental `shutil` guarantee), not a deliberate design check, and untested. The generalization of the promotion pattern to move operations is sound; the addition this instance surfaces is that **any character-class slug validation feeding a path-move must also carry an explicit reserved-name denylist** for any fixed sibling name in the same directory (here, just `archive`), rather than relying on the move primitive's own incidental error behavior.

**Where it appears:** `hooks/require-write-result.py` — `_archive_workstream_dir` / `_maybe_auto_archive_workstreams`, introduced in PR #1467 (Round 1, APPROVED, `oracle/verdicts/pr-1467.md`). The reserved-name gap is named as a fast-follow, not a blocker, since current behavior is a safe no-op rather than data loss.

**Reuse guidance:** When promoting any advisory step whose enforcement involves a filesystem move (not just a string-match gate) to a hook: (1) apply the original promotion test (is the condition a pure field/string check?) — if yes, promote; (2) additionally enumerate every reserved/fixed sibling name that lives in the same parent directory as the move destination, and denylist those names explicitly alongside the character-class regex; (3) write a test for the denylist, not just a test for the currently-safe incidental behavior of the underlying move primitive — incidental safety is not a substitute for a deliberate check and will not survive a refactor of the move mechanism.

**Reuse guidance:** Before approving any PR whose stated purpose is "remove this content because it's duplicated/migrated elsewhere," require: (1) a full-repo, all-extension grep for the literal path being deleted, run by the reviewer independently — not just asserted by the PR author; (2) for any filename collision between source and destination, a byte-level diff proving no data was silently dropped or clobbered; (3) a check that the claimed approval (if the deletion is judgment-call-worthy, not mechanical) traces to a specific, timestamped reply to a specific enumerated item — a terse reply ("sg", "yes") to a list with named defaults counts as traceable; a vague "sounds fine" with no enumerated list behind it does not.

---

### [2026-07-04] Pattern: repair a code/prose logic duplication by pointing prose at code, not by re-copying the corrected value

**Pattern:** When a round-1 review finds that a deterministic matching/extraction rule exists in two independently-driftable homes (a tested Python function and untested `.claude/agents/*.md` prose) and one copy was already fixed while the other wasn't, the correct fix is not to copy the corrected value into the prose a second time — it is to have the prose state the rule once, explicitly, and cite the tested function as the canonical source, so any future correction to the rule only has one place to land in code and one explicit cross-reference in prose to keep in sync.

**Why it works:** PR #1468's round-2 fix (commit `4c219b48`) corrected `.claude/agents/lobster-meta.md` Step 2.75 and `lobster-hygiene.md` Step 2c's scheduled-job id-matching by (a) stating the rule explicitly — registry ids for `scheduled_job` are three-segment `jobs/<state>/<key>`, jobs.json key is the last path segment — and (b) naming `scheduled-tasks/canon-digest.py`'s `compute_job_diff` (`rid.rsplit("/", 1)[-1]`) and `src/utils/artifact_registry.py`'s `ArtifactState` enum as the sources to mirror, rather than silently re-deriving the same string in prose a second time with no link back to the tested implementation. Verified independently (not just accepting the author's fix): hand-traced the corrected prose logic against the live 34-entry scheduled-job registry and 65-key `jobs.json` and confirmed zero duplicate "unregistered" entries would result — the underlying defect this pattern prevents (an untested prose copy silently drifting from its tested code counterpart) is closed by making the drift visible and citable, not just by making the current values match.

**Where it appears:** `.claude/agents/lobster-meta.md` Step 2.75, `.claude/agents/lobster-hygiene.md` Step 2c, PR #1468 commit `4c219b48`. Oracle verdict `oracle/verdicts/pr-1468.md` Round 2 (APPROVED). Directly extends the "no split canonical homes" principle from the schema layer (where it's usually applied — enum values, thresholds) to the matching-*logic* layer.

**Reuse guidance:** Whenever a bug fix corrects a deterministic extraction/matching operation that is restated in both tested code and untested prose (agent instructions, bootup docs), do not just patch the prose value to match — rewrite the prose to name the rule once and cite the code location that implements/tests it, so the next divergence is a stale cross-reference (easy to spot in review) rather than a second silently-wrong independent derivation (invisible until it corrupts data on the next run).

---

### [2026-07-30] Pattern: split a conflated directory-role parameter into two independently-defaultable parameters, with the new one falling back to the old for backward compatibility

**Pattern:** PR #1489 fixed `bridges.py`'s `sync_projects_to_arcs`/`sync_priorities_to_attention`/`run_bridges` — which took a single `workspace_path` param doing double duty as both "where canonical markdown source files live" and "where DB-adjacent output (`_context.md`) gets written" — by adding a second, optional `canonical_path` param dedicated to the source-of-truth read location, defaulting to `workspace_path` when omitted. This closed Issue #1480 (wrong default source directory causing a silent zero-effect sync) without touching the signature or default behavior of the function's other existing caller (`inference.py`'s `run_consolidation()`), which was left uninvolved because it doesn't need the fix today.

**Why it works:** The bug existed because two genuinely different concerns — read location and write location — were conflated under one name and one default. Splitting them into two independently-overridable parameters, rather than just correcting the single default value, makes the distinction a permanent, type-visible fact about the function rather than a one-time value correction that could drift back out of sync the next time someone reasons about "workspace_path" as if it meant one thing. The backward-compatible fallback (`canonical_path` defaults to `workspace_path` when omitted) means the fix has zero blast radius on any caller that doesn't explicitly ask for the new behavior — verified directly: the PR's diff touches zero call sites other than the one production caller (`nightly-consolidation.md` step 10) that actually needed the corrected path, and the existing 18-test suite passes unchanged with the new 2 tests added on top (20/20, independently re-run by the oracle in a fresh worktree against both branches).

**Where it appears:** `src/mcp/user_model/bridges.py` — `sync_projects_to_arcs`, `sync_priorities_to_attention`, `run_bridges`; `.claude/agents/nightly-consolidation.md` step 10; PR #1489 (Round 1, APPROVED, `oracle/verdicts/pr-1489.md`). Extends the general "no split canonical homes" principle to function *parameters*: when a bug is rooted in one parameter silently meaning two different things to two different call sites, splitting the parameter (not just fixing its default) is preferable when the two meanings have genuinely different correct values for different callers.

**Reuse guidance:** When a path/location bug is traced to a single parameter serving two conflated roles (e.g. "source of truth" vs. "output destination," or "input format" vs. "output format"), check whether the two roles ever need different values for any real caller. If yes, split into two parameters with the new one defaulting to the old, rather than just correcting the shared default — this preserves every existing caller's behavior exactly while making the fixed caller's correct behavior explicit and typed rather than tribal knowledge. Do not widen the fix to touch callers that don't currently need it; a narrow, additive-only diff is easier to verify completely (as here, via a from-scratch DB run against real production files) than a broad one.

---

### [2026-08-02] Pattern: a non-oracle consumer converges its own dedup logic onto the oracle's committed verdict file rather than building a parallel tracking mechanism

**Pattern:** PR #1495 fixed `PR_REVIEW_SWEEPER` re-dispatching redundant oracle reviews (issue #1491) by adding `has_final_verdict(pr_number)`, which reads `oracle/verdicts/pr-{n}.md` directly as a pure local-filesystem check (no `gh` API call) and treats an exact `VERDICT: APPROVED` first line as terminal. This is the second independent consumer of the verdict file (the dispatcher's own PR Merge Gate is the first) — rather than inventing a second dedup mechanism (a new state field, a new marker convention, or reviving the already-dead `<!-- lobster-review -->` PR-comment marker that `has_review_comment()` checks for but no agent ever posts), the fix points at the one artifact the codebase had already established as authoritative for "has this PR's review concluded."

**Why it works:** The codebase already had a competing, unimplemented dedup design on record — `docs/design/pr-review-layer.md` (Status: DRAFT) proposes a `lobster-reviewed` GitHub label as the dedup signal, never built. Had the sweeper implemented that design, or invented a third mechanism, the codebase would have accumulated three partially-overlapping "has this been reviewed" signals (a dead comment marker, an unbuilt label, and a real verdict file) with no way to know which one a given piece of code trusts. Converging on the verdict file — already read by the dispatcher, already the documented authority per CLAUDE.md's PR Merge Gate — means a future third consumer has exactly one place to look, and a change to the verdict format only needs updating in one place's worth of readers. Independently verified via full-repo grep (`has_review_comment`/`REVIEW_MARKER`/`lobster-review`) that no other code path depends on the mechanism being left in place unconverted, and that the DRAFT design doc's alternative was never implemented — so the convergence has no orphaned dependents.

**Where it appears:** `scheduled-tasks/pr-review-sweeper.py` — `has_final_verdict()`, `VERDICT_APPROVED_LINE`, `ORACLE_VERDICTS_DIR`; PR #1495 (Round 1, APPROVED, `oracle/verdicts/pr-1495.md`). Extends "no split canonical homes" (2026-04-25) from file/module placement to cross-component signal consumption: when two components need the same fact ("is this PR done being reviewed?"), they should read the same artifact, not maintain independent proxies for it.

**Reuse guidance:** Before adding a new tracking mechanism (state file, label, marker comment, DB flag) to answer a question another component in the system already answers authoritatively, grep for the existing authoritative artifact's consumers first. If one exists and is reachable as a plain read (file, DB row, API field) from the new call site, read it directly rather than deriving a parallel proxy signal — even if the new call site is architecturally distant (here, a Type B cron script vs. an LLM-driven dispatcher). A named, unimplemented alternative design (a DRAFT doc, an open proposal) does not count as "already implemented" — verify implementation status by grep, not by the design doc's own claimed authority.
