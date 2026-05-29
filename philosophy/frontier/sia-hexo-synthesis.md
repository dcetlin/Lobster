---
status: research synthesis
source_paper: arxiv:2605.27276 (SIA: Self Improving AI with Harness & Weight Updates)
source_repo: https://github.com/hexo-ai/sia
researched: 2026-05-29
---

# SIA / hexo-ai Synthesis: Evolution Signals for Lobster

*Research synthesis. Read in conjunction with wos-v3-convergence.md and system-metabolism.md.*

---

## 1. What SIA / hexo-ai Is

SIA (Self Improving AI) is a framework in which a Feedback-Agent iteratively updates both the *scaffold* (system prompt, tool dispatch, retry logic, answer extraction code) and the *model weights* (via LoRA fine-tuning) of a task-specific agent, using full execution trajectories as feedback. The paper's core thesis: the two dominant schools of AI self-improvement — harness/scaffold iteration and test-time weight updates — have operated in isolation, each leaving the other lever untouched. SIA combines both, demonstrating consistent gains over each alone across three domains (law, GPU kernel optimization, single-cell biology). The hexo-ai GitHub org is the lab that built and published this work, with adjacent repos covering multi-agent protocols (Socrates), agent fundamentals, and test-time training research.

---

## 2. Key Mechanisms

- **Two-lever self-improvement loop**: Feedback-Agent selects, per generation, whether to apply a harness update (scaffold edit, weights fixed) or a weight update (LoRA fine-tune, scaffold fixed). The selection is dynamic, informed by execution trajectory analysis.

- **Three-agent architecture**: Meta-Agent (generates initial scaffold from task spec) → Task-Specific Agent (executes, emits trajectory logs) → Feedback-Agent (reads full trajectories, selects and applies improvement action). The Feedback-Agent uses a frozen LLM prior for lever selection — not a learned policy.

- **Trajectory-grounded feedback**: The Feedback-Agent does not operate on aggregate metrics. It receives complete execution logs: every prompt, model response, tool call, tool result, and extracted answer for every task instance. This is the substrate for diagnosis.

- **Harness updates as software-engineering hygiene**: Scaffold edits concentrate on parsing, retries, dispatch, specialized extractors. They shape *how the agent searches and acts* — but cannot instill domain intuition the base model lacks. This is the observed limit of scaffold iteration.

- **Weight updates as domain internalization**: LoRA fine-tuning encodes knowledge that no prompt or scaffold can reach. Concrete finding: on MAGIC denoising, weight updates introduced a `np.clip + np.rint` post-processing step that rounded imputed counts to non-negative integers — encoding biological constraints directly into policy. The harness *never* proposed this across all scaffold iterations.

- **Feedback-Agent as dynamic RL algorithm selector**: The Feedback-Agent does not run a fixed RL procedure. It selects from: PPO-with-GAE (dense rewards, stability-constrained), GRPO (cheap rollouts, terminal verifier), entropic advantage weighting (right-skewed reward histograms), REINFORCE+KL-to-base (dense rewards, regression risk), best-of-N behavioral cloning (cold-start with near-zero reward), DPO (ranking verifiers). This is algorithm selection as a reasoning act, not a configuration decision.

- **Socratic paired-agent protocol** (hexo-ai/socrates repo): A scientist agent (tools, execution access) paired with a question-only advisor (no tools, no directives, cannot give answers). The advisor's constraint — *force approval via question only* — produced 55.9% average improvement over the scientist running alone. The adversarial constraint generates productive friction without introducing a competing executor.

- **Coupled Goodhart failure mode** (stated in Limitations): Both levers optimize against the same fixed verifier. Harness search shapes the distribution the weights see; weight updates train on data collected through a scaffold that will subsequently change. The joint fixed point is a Nash equilibrium between two optimizers blind to each other's update history — strong on the training verifier, fragile under perturbation to either component.

---

## 3. Resonances with Lobster

**Dispatcher → subagent delegation as harness iteration.** Lobster's dispatcher/subagent split is structurally a harness update system: the dispatcher (meta-agent) generates task specs and routes to subagents (task-specific agents), which emit result artifacts. The feedback loop is currently one-way and human-mediated — Dan adjusts bootup files, IFTTT rules, and dispatch logic based on observed failures. SIA names this pattern and makes the feedback loop automated. The structural form is identical; the automation level is not.

**WOS steward/executor as the trajectory feedback substrate.** The WOS corrective trace mechanism (V3 Change 2, `corrective_traces` table) is the closest thing Lobster has to SIA's trajectory-grounded feedback. The steward reads traces from prior UoW cycles and injects prescription_delta for the next execution. This is trajectory-grounded harness update — the same mechanism SIA's Feedback-Agent uses, without the weight update lever. The S3 Observation Loop (cross-cycle pattern learning) is the missing "inter-organ signal" — SIA calls the same gap "Feedback-Agent applying weight updates" in the sense that weight updates embed patterns no scaffold edit reaches.

**Register taxonomy as the scaffold-side of the two-lever problem.** SIA's harness updates shape search procedures and tool dispatch. Lobster's WOS register taxonomy (operational, philosophical, human-judgment registers) shapes executor routing. Both are attempts to classify *what kind of work this is* before deciding *how to approach it*. SIA is empirical — it discovers the right scaffold through iteration. Lobster is principled — it tries to classify correctly before execution. SIA's finding that scaffold iteration plateaus without weight updates maps onto Lobster's open problem: the register table was reasoned from first principles and its edge cases are where attunement will develop (WOS V3 convergence doc, Pearl 2).

**Metabolic taxonomy and SIA's outcome categories.** SIA's two improvement actions map cleanly onto Lobster's metabolic taxonomy: harness updates are *seeds* (intentional investment in future capability, shape how the agent acts) and weight updates are *pearls* (direct high-value output, encode new capability that persists). The finding that harness updates alone plateau — weight updates are needed for the full gain — is equivalent to Lobster's taxonomy observation that a system running only seeds never produces pearls. The generative cycle must close (seed → juice → pearl) not just accumulate seeds.

**Poiesis framing and the Feedback-Agent.** SIA's Feedback-Agent reading full execution trajectories and selecting the right improvement action is, in Lobster's vocabulary, an attempt at poietic orientation: attending to what the trajectory is doing and letting the next move be shaped by that, rather than executing a fixed improvement procedure. The distinction between SIA's dynamic algorithm selection and a fixed RL pipeline is exactly the poiesis/production distinction. SIA is a system that made the harness-update school more poietic by adding a responsive lever-selection layer.

**Trajectory as the right feedback substrate.** Both systems agree: aggregate metrics are insufficient. SIA's Feedback-Agent receives full execution logs. Lobster's corrective trace mechanism reads full UoW execution traces. The structural convergence here is not accidental — it reflects a shared finding that coarse-grained feedback (pass/fail, metric delta) loses the diagnostic signal needed for targeted improvement.

---

## 4. Divergences

**Automated vs. human-mediated improvement loops.** SIA automates the full improvement cycle; Lobster's scaffold changes require Dan's review. This is a deliberate Lobster design choice (human-in-the-loop on bootup/dispatch changes) but it means Lobster's improvement rate is bounded by Dan's engagement cadence. SIA's finding — that automation across three contrasting domains outperforms scaffold-only with human iteration — puts quantitative weight on what Lobster's current constraint costs.

**Model weights as a lever.** Lobster has no weight update lever. All capability improvement is scaffold/prompt/routing improvement. SIA's central finding is that this lever is categorically insufficient for domain-specific internalization: "patterns encoded into the model's parameters that no scaffold edit reaches." Lobster currently has no path to the equivalent of SIA's weight update gains. The implication is not that Lobster should fine-tune (it cannot control its model weights), but that there is a class of improvement Lobster's current architecture cannot achieve.

**Fixed verifier vs. continuous feedback.** SIA requires a deterministic verifier per task — a function that scores output. Lobster's "task success" is often ambiguous, multi-dimensional, or only evaluable by Dan. This means SIA's architecture cannot be directly applied to most of what Lobster does. The Socratic protocol (hexo-ai/socrates) partially addresses this — the question-only advisor provides feedback without needing a formal verifier.

**Lever-selection policy: frozen LLM vs. learned selector.** SIA's Feedback-Agent selects levers via a frozen LLM prior. SIA's own Future Work (Section 9) identifies this as a design limitation: the selection policy should itself be learned via meta-RL across a task distribution. Lobster's WOS steward is also using a frozen LLM prior for UoW routing. Both systems have the same open problem at the same structural layer.

**Coupled Goodhart is a shared risk Lobster has not named.** SIA explicitly names the coupled optimizer failure mode: two levers optimizing against the same verifier produce a Nash equilibrium rather than a true optimum. Lobster's WOS has an analogous structure: the steward and executor both optimize against the same prescription context, with the steward updating context based on executor outputs that were themselves shaped by prior steward prescriptions. The coupled Goodhart problem is latent in Lobster's corrective trace loop and is not currently named or guarded against.

---

## 5. Evolution Signals

**Signal 1 — Trajectory observation as a first-class WOS component (maps to S3).** SIA's Feedback-Agent reads full execution trajectories to select improvement actions. Lobster's S3 Observation Loop is designed to do the same thing across UoWs. The SIA paper provides the empirical grounding for why trajectory-grounded feedback produces gains that metric-based feedback cannot: it surfaces the diagnostic signal for *which lever to apply*. S3 should be designed with this as its explicit purpose — not just pattern detection, but lever-identification: "is this a scaffold problem (wrong routing, wrong prescription) or a capability problem (no amount of scaffolding will produce this behavior)?" The second category is currently invisible to Lobster.

**Signal 2 — The Socratic advisor protocol for WOS philosophical-register UoWs.** The Socratic pattern (tool-using executor paired with question-only advisor that cannot issue directives) addresses a specific Lobster gap: philosophical-register UoWs currently have no counterpart to a question-only advisor that prevents premature closure. The WOS V3 convergence doc identifies the Dan-interrupt cartridge (S5) as the "surface to Dan" slot that should be the beginning of an encounter, not a terminal state. The Socratic protocol is a concrete instantiation of S5 — the advisor is structurally prevented from giving answers, which forces the scientist to reason rather than defer. This pattern could be applied to Lobster's philosophical-register dispatcher sessions.

**Signal 3 — Coupled Goodhart guard for the corrective trace loop.** Lobster's corrective trace mechanism (WOS V3 Change 2) is the structural analog of SIA's two-lever optimization. The S1 seed (loop gain bounding on prescription_delta) addresses oscillation risk but not the Nash-equilibrium risk SIA identifies: the prescriber shapes the executor distribution; the executor produces the traces that shape the prescriber. A guard is needed — not loop gain bounding (that is S1) but a mechanism to detect when the corrective traces are recycling rather than improving. SIA's observation: "prescription recycling appearing across multiple unrelated UoWs" is exactly the signal. The cross-UoW detection in S3 should include this as an explicit detection category (it is already partially specified in the wos-v3-convergence.md S3 section — "prescription recycling cross-UoW" — but not connected to the coupled Goodhart framing).

**Signal 4 — Lever-selection policy as an improvable component.** SIA's Feedback-Agent uses a frozen LLM prior for lever selection. SIA identifies this as the next-order improvement target: make the selection policy itself learnable. Lobster's WOS steward has the same structure at the register-routing layer — the steward uses a frozen LLM prior to classify register and select executor. The germinator.py register classification is the lever-selection policy. If classification errors are persistent (same register repeatedly misrouted), that is a signal to improve the classification policy, not just the prescription. S2 (mismatch observability instrument) is the data collection mechanism. The question SIA raises: when you have enough mismatch data, what do you do with it? Currently Lobster surfaces it to Dan. SIA suggests the path forward is making the selection policy adaptive. This is probably the deepest architectural evolution signal in the paper.

**Signal 5 — Register-portfolio diversity as a metabolic health signal (not just an operational signal).** SIA demonstrates that combining two categorically different levers (scaffold and weights) produces gains neither achieves alone. The insight is that the two levers address *categorically different classes of problem*. Lobster's register-portfolio diversity metric (WOS V3 convergence, Timescale 1) is the direct analog: philosophical-register UoWs maintain the orientation capacity that operational-register UoWs cannot provide. The SIA paper provides an empirical grounding for why portfolio diversity is not just "balance" but necessary for full-spectrum capability: the category of problem scaffold-editing cannot solve requires a different lever.

---

## 6. Framings

**"Two silos operating in isolation"** — SIA's diagnostic for the field's self-improvement research. Directly applicable to Lobster's improvement mechanisms: bootup file edits (scaffold), IFTTT rules (scaffold), and WOS prescription updates (scaffold) are all in the harness silo. There is no weight update silo. The framing sharpens the diagnosis: Lobster's self-improvement is currently entirely within one lever. The limits of that lever are empirically known from SIA.

**"Harness updates make the model agentic; weight updates build the domain intuition no prompt can instil."** This is the clearest formulation of the two-lever distinction. Lobster's bootup files, IFTTT rules, and WOS prescription context make Lobster more agentic — they shape search procedures and dispatch logic. But there is a class of capability improvement that no amount of prompt engineering reaches. SIA names this class: *domain intuition*. For Lobster, the relevant domain is Dan's context, preferences, and working patterns. The question SIA's framing raises: what is the "domain intuition" Lobster most needs, and what would it take to instil it structurally rather than via prompt?

**"Coupled Goodhart"** — SIA's name for the failure mode where two optimizers share a verifier and converge to a Nash equilibrium rather than a true optimum. Lobster does not currently have a name for this failure mode. It should. The corrective trace loop is the Lobster structure most at risk.

**"Meta-RL over the action-selection policy"** — SIA's framing for the next-order improvement target: make the lever-selection policy itself improvable. In Lobster terms: make the register classification policy improvable based on accumulated mismatch evidence. This is currently a human-mediated improvement (Dan reviews mismatch patterns, edits the germinator prompt). SIA's framing names this as a learnable policy, not a human review problem.

**"Scaffold as software-engineering hygiene; weights as domain internalization"** — The distinction that explains why scaffold iteration plateaus. Applied to Lobster: IFTTT rules and bootup edits are software-engineering hygiene. They shape structure and procedure. The "domain internalization" equivalent for Lobster — deeply learned patterns about Dan's context, working style, and judgment calls — is currently only achievable through more context (more memory, better retrieval), not through structural encoding. This framing clarifies the category boundary between what memory/context can achieve and what it cannot.

---

*Synthesis complete. Evolution signals 1-4 map to existing WOS V3/V4 design directions and deepen their rationale. Signal 5 maps to the portfolio diversity observation. Framings 2-5 introduce vocabulary Lobster does not currently have. No implementation decisions embedded — this document is research input for Dan.*
