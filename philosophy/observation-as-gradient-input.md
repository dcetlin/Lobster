# Observation as Gradient Input

*A named principle — April 26, 2026*

---

## The Distinction

Every observation the system produces has two possible destinations:

1. **Content artifact** — a log entry, digest, GitHub issue, session note. The observation terminates here. It is recorded. It does not change anything the system will do next.

2. **Gradient input** — a signal that modifies the routing substrate, the behavioral rules, or `vision.yaml`. The observation propagates. It changes the weights.

The current architecture routes almost all observations to destination 1. This is not a neutral design choice. It means the system accumulates records of its own behavior while remaining structurally unchanged by what those records contain. The feedback loop is open.

---

## Why This Matters

The distinction between observation-as-content and observation-as-gradient-input maps directly to Vision Object Function 2 (VOF-2), which governs the observation-to-behavior loop. VOF-2's implementation is the vision inlet: the mechanism by which observations become `vision.yaml` field changes. Without the inlet, observations terminate in content and VOF-2 is not satisfied in any functional sense.

This is not a WOS-specific concern. It applies to any domain where the system produces observations:

- **Philosophy-explore sessions** generate insights about system structure, attentional priorities, and form-function alignment. These insights currently become session files. They do not modify routing weights.
- **User modeling outputs** generate inferences about preferences, communication patterns, and behavioral tendencies. These currently become memory entries. They may or may not modify how future messages are routed.
- **Negentropic sweeps** generate prescriptions for new UoWs. These do modify the pipeline — sweep output is designed as gradient input, not content. The sweep is the closest existing example of the principle in practice.

The sweep is instructive precisely because it is the exception. It was designed with the explicit intention that its output would change what the system does next. This intention is what makes it a gradient input rather than a content artifact. The design decision came first; the mechanism followed.

---

## The Principle

**Observations from philosophy-explore sessions, user modeling, and negentropic sweeps should function as gradient inputs that modify routing weights (Vision Object Function 2), not merely as content to be logged.**

This requires:

1. A pathway from observation to behavioral change that costs less than one discrete issue-and-review cycle per propagation. The vision inlet design (see reference below) is the concrete implementation for `vision.yaml` field changes.

2. Classification of observations by their intended destination at the time of writing, not retrospectively. An observation written as content will be treated as content. An observation written as a gradient input — with explicit field mapping, confidence level, and proposed change — can be routed to the inlet.

3. Acknowledgment that the absence of this pathway is a form-function violation. A system that is functionally a learning system but structurally lacks a feedback loop is not learning. It is going through the motions with no gradient. The structure lies about the function.

---

## Connection to Form-Function Isomorphism

The open observation loop is a form-function violation at the systemic level. From `philosophy/form-function-isomorphism-20260426.md`, §V:

> "The system's behavior is not connected to its form in the way that would allow the system to improve. The gradient input — observation-as-change, the signal that modifies the weights — never arrives. The system continues generating behavior that is not informed by the consequences of that behavior."

And:

> "The correction is not to add more observations. It is to close the loop: to make the form (the feedback architecture) isomorphic to the function (self-improving goal-directed behavior). The inlet is not a monitoring system bolted on afterward; it is the structural expression of the system's nature as a learning system. When it is absent, the system is lying about what it is."

The principle named here is the philosophical grounding for that structural claim. When the inlet is built, it is not adding a feature. It is closing a gap between what the system is and what its form says it is.

---

## References

- `~/lobster-workspace/workstreams/wos/design/vision-inlet-design.md` — concrete implementation: observation taxonomy, inlet queue, auto-apply thresholds, audit trail
- Issue dcetlin/Lobster #975 — tracking issue for VOF-2 inlet implementation
- `~/lobster/philosophy/form-function-isomorphism-20260426.md` §V — "Why Things Break When Form and Structure Diverge", including the open observation loop analysis
- `oracle/patterns.md` — infrastructure-vs-execution discriminator (related: distinguishing signal from noise in observation routing)

---

*Written April 26, 2026. This principle emerged across three independent threads in the same session: Priority Guardrail retirement analysis, vision-inlet-design.md drafting, and the form-function-isomorphism philosophy exploration. All three resolved to the same conceptual place.*
