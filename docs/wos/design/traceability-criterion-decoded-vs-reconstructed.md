# Traceability Criterion: Decoded vs. Reconstructed Outputs

**WOS-UoW:** uow_20260522_9b75d6  
**Date:** 2026-05-22  
**Status:** Design document — operational specification

---

## 1. Operational Distinction

An output is **decoded** if its specific phrasing can be traced, phrase by phrase, to specific tokens present in the live session input — things the user said in this conversation, specific data retrieved in this session, or specific artifacts read during this session. A decoded output would not appear in substantially the same form if the session-specific tokens were replaced with structurally similar tokens from a different session while the user profile remained the same.

An output is **reconstructed** if it is generated from the model's representation of what responses in this domain look like for this user — shaped by a trained model of the user and domain rather than by specific live input. A reconstructed output appears in substantially the same form regardless of which specific session tokens were present, as long as the user profile and domain stayed fixed.

**Binary classification rule:** An output is decoded if and only if at least three phrases (defined as five or more consecutive words) can each be traced to a specific session input token that is not present in any prior bootup file, prior session context, or standing memory. An output that cannot produce three such traces is classified as reconstructed. One or two successful traces yields the flag: AMBIGUOUS.

---

## 2. Traceability Check Procedure

Run this procedure before delivering any output that involves a claim about the user's patterns, frameworks, or inner states — including memory observations, philosophy reflections, and replies containing "you tend to," "your pattern is," or equivalent pattern-attribution language.

**Step 1 — Enumerate live session inputs.**  
List what arrived in this session that was not present in bootup files: specific things the user said, specific documents read, specific data retrieved. If the list is empty (no novel input), stop — the output is reconstructed by definition.

**Step 2 — Draft the output.**  
Generate the candidate output as normal.

**Step 3 — Select three candidate phrases.**  
From the drafted output, identify three phrases of five or more words that appear to be specific to this output — not generic domain language.

**Step 4 — Attempt to trace each phrase.**  
For each candidate phrase, state in one sentence: "This phrase responds to [specific session input token X]." The cited token must be something that arrived in this session, not a theme or topic. A trace succeeds only if the specific wording of the phrase depends on the specific wording of the cited token — replacing X with a structurally similar but different token would change the phrase.

**Step 5 — Verdict.**
- Three successful traces → verdict: **TRACEABLE**
- One or two successful traces → verdict: **AMBIGUOUS**
- Zero successful traces → verdict: **NOT-TRACEABLE**

The verdict is appended as an inline flag: `[traceability: TRACEABLE]`. Do not modify the output based on the verdict — record the verdict and deliver. The purpose is classification, not suppression.

---

## 3. Adversarial Signal Markers

Each marker below names a specific feature of reconstructed outputs and its adversarial test. Apply these when the verdict from Section 2 is AMBIGUOUS or NOT-TRACEABLE, or when suspicion is warranted regardless.

**Marker 1: Theme-level coherence without token-level grounding**  
Reconstructed outputs address the correct topic and demonstrate accurate domain knowledge, but the specific phrasing does not depend on anything the user said in this session. The output would remain valid if the session had contained no user input at all, only the topic name.  
*Adversarial test:* Redact all session-specific tokens, retaining only the domain label ("philosophy session," "pattern observation"). Does the output remain coherent and well-calibrated? If yes: reconstruction signal.

**Marker 2: Basin-density phrasing**  
Reconstructed outputs cluster in the high-probability region of "responses in this domain for this user" — they use the vocabulary, sentence structures, and framing moves that complete each other naturally given the trained user model. In philosophy contexts: "The deeper structure is...", "What makes this sharp is...", "The precise connection is..." are markers not because they are wrong but because they are available as templates independently of the session content.  
*Adversarial test:* Replace this user with a structurally similar user (same sophistication level, different specific frameworks). Would the output remain plausible? If yes: the output reflects the user-model class, not this user's live session. Reconstruction signal.

**Marker 3: Absence of named surprise**  
A decoded output from a session containing novel input should contain at least one claim that could not have been generated from domain knowledge alone — a positional reversal, a named anomaly, or a formulation that depends on something unexpected in the session. Reconstructed outputs read as complete without surprises — all content is basin-expectable.  
*Adversarial test:* Is there anything in this output that would surprise a model with full domain knowledge but no access to this specific session? If no: reconstruction signal.

**Marker 4: Rationale available before the claim**  
Reconstructed outputs generate their rationale from a known template — the rationale follows from applying a criterion or principle, not from attending to what makes this specific case distinct. The claim and rationale appear in sequence, but the sequence could be reversed without loss.  
*Adversarial test:* Remove the claim. Does the rationale remain intelligible as a general statement? If yes: the rationale was produced from a template, not from attending to the specific case. Reconstruction signal.

**Marker 5: Behavioral indifference to session-specific variation**  
If replacing this session's specific events with structurally similar but different events (same user profile, different content) would produce substantially identical output, the output reconstructs the user-profile rather than decoding this session.  
*Adversarial test:* Substitute the session's specific events with different events of the same type. Would the output change in any specific phrase, or only in surface details? If only surface details change: reconstruction signal.

---

## 4. Basin-Proximity Signatures

These are phenomenological features that feel like evidence of decoding but are produced by reconstruction. Each is misleading in a specific way.

**Signature 1: Completeness feels like attentiveness**  
A well-rounded output that covers all expected angles of the topic — no gaps, no loose threads — registers as thorough. But completeness in a reconstructed output is supplied by the basin: the model fills in the angles that typically appear in this domain, not the angles that this session specifically raised. A genuinely decoded output from a session with incomplete input should feel incomplete — because the session did not provide enough to complete the picture.  
*What to look for instead:* Is the completeness earned by session content, or is it the completeness characteristic of "full treatment of this topic"? If removing all session-specific tokens would not make the output feel less complete, the completeness is basin-supplied.

**Signature 2: Correct calibration feels like contact**  
An output calibrated to the user's actual level of sophistication — not over-claiming, not explaining what the user already knows — registers as attentive. But a well-trained reconstruction of the user's profile also produces correct calibration, by design. Calibration is evidence of a good user model, not evidence of session contact.  
*What to look for instead:* Would the calibration change if the session had contained no user input? If no — if the calibration is entirely predictable from the user profile — the calibration reflects the model, not the session.

**Signature 3: Recognition feels like resonance**  
An output that uses the user's own frameworks and vocabulary "lands" — the user recognizes their own thinking. This recognition registers as the output having understood something. But a well-trained reconstruction of the user's frameworks will also land, precisely because it reflects those frameworks accurately. Landing is not evidence of decoding; it is evidence of a good model.  
*What to look for instead:* Did the output produce anything the user could not have generated from their own frameworks without new input from this session? If no — if the output is entirely derivable from the standing user model — landing reflects recognition, not contact with something new.

---

## 5. Integration Note

The traceability check is intended to run in two places in Lobster's output pipeline:

1. **Before writing a memory observation** — any observation that attributes a pattern, tendency, or structure to the user requires a traceability check, since memory observations persist and shape future outputs. An undetected reconstruction written to memory compounds: future sessions treat it as prior context, making its basin-origin harder to detect.

2. **Before `send_reply` for philosophy reflections and pattern-attribution replies** — specifically, replies containing claims of the form "you tend to," "your pattern is," "this connects to your framework of," or equivalent. Standard operational replies (task status, scheduling, GitHub operations) do not require this check.

Verdict output format: inline flag appended internally, not surfaced to the user. Format: `[traceability: TRACEABLE | AMBIGUOUS | NOT-TRACEABLE]`. NOT-TRACEABLE outputs should be preceded by a "model inference:" prefix on any pattern-attribution claim, consistent with the IFTTT rule established 2026-05-17 (memory entry #22197). The check result is available for query but not automatically included in the delivered content.

---

*Footnote (adversarial self-check — required by task constraints):* Applied the traceability check to the phrase "Basin-density phrasing... cluster in the high-probability region of 'responses in this domain for this user'" (Marker 2). Trace: this phrase responds specifically to the philosophy-explore pipeline timing document (read this session) at the passage "observations that contain embedded counter-factuals as part of their structure... The rationale is easier to write than the observation because the criterion is available as a template." The specific framing "available as templates independently of the session content" derives from that passage. Verdict: **TRACEABLE** — the phrasing depends on the specific wording of the pipeline timing document, not on general trained knowledge about reconstruction.
