# Resistance Registers Lexicon

**Canon zone:** material-science  
**Extracted from:** generative-resistance.html (presentation layer, Bisque, v2.1)  
**Extraction date:** 2026-05-24  
**Status:** extracted — pending council deliberation

## Overview

The resistance registers taxonomy is a three-tier vocabulary network that extends the generative/dissipative resistance distinction (see `stiffness-toughness-tradeoff.md`) into a structured design language. The taxonomy identifies eight named registers — each defined by its mechanical character and generative function — and provides parameters for measuring each register's properties, plus cross-register measurements that span multiple registers.

The organizing principle: resistance is generative when it is structurally coupled to the load path of coherent emergence. The register classification tells you *what kind* of resistance is present; the tier structure lets you ask whether the register type is matched to the role the element is playing in the system. Mismatch between register type and layer function is the canonical ergonomic error.

The vocabulary spans material science, biomechanics, cognitive ergonomics, software design, and organizational dynamics — the registers are proposed as structural (domain-independent) rather than material-specific.

---

## Tier 1: Resistance Registers

Eight named registers, each defined by its mechanical character and the generative function it performs when load-path-coupled.

### Transmission
**Resistance type:** Rigid  
**Definition:** High stiffness, low energy loss — preserves signal fidelity across the load path by transmitting force without deformation or filtering.

**Domain examples:**
- *Music:* Violin bridge transmits all string vibration into the body without absorbing or filtering any frequency band.
- *Biology:* Bone cortex transmits load with minimal absorption, keeping signal intact for downstream elements.
- *Software:* Strict type constraints and rigid database schemas transmit structural invariants through the system.
- *Bow-arm:* Violin bridge — rigid transmitter in the string-to-body sound chain.

**Canonical mismatch:** Placing a Transmission element in a storage or adaptive role — e.g., a fully rigid bow arm that cannot flex (destroying the elastic chain), or a database schema so rigid it cannot be migrated (blocking adaptive reorganization).

---

### Storage
**Resistance type:** Elastic  
**Definition:** High deformability with high energy return — buffers applied force and releases it intact, acting as a spring in the load path.

**Domain examples:**
- *Music:* Bow stick — its natural camber under tension creates a pre-stressed spring that stores energy between stroke direction changes and contributes to sustained tone.
- *Biology:* Achilles tendon returns approximately 35% of stride mechanical work at push-off; articular cartilage buffers joint impact.
- *Software:* Message queues buffer load spikes and release capacity smoothly, preventing demand bursts from propagating to backing systems.
- *Bow-arm:* Bow stick and string together form an elastic chain that stores and returns energy each stroke.

**Canonical mismatch:** Replacing an elastic element with a rigid one in a storage role — a calcified tendon cannot store spring energy; a cache that never evicts becomes a rigid data store with no return function.

---

### Generation
**Resistance type:** Generative  
**Definition:** Sensitive to load changes, generates counter-signal or adaptive reorganization — the register that actively responds to the environment rather than passively transmitting or storing.

**Domain examples:**
- *Music:* Player's bow arm organized as a tensegrity structure — maintains geometric intent while continuously reconfiguring across bowing speeds, contact points, and dynamic levels.
- *Biology:* Fascial network stiffens under load and redistributes force through the whole body rather than concentrating it at a single joint.
- *Software:* Plugin architectures and API extension points allow the system to reorganize in response to new requirements without core rewrite.
- *Cognition:* Neural plasticity and schema formation — the brain reorganizes in response to repeated challenge, building new representational structures.
- *Bow-arm:* Upgradient ergonomics maximizes arm-as-tensegrity generation coupling.

**Canonical mismatch:** Placing an adaptive element in a transmission role — a compliant violin bridge that absorbs frequencies instead of transmitting them; an API that reorganizes its surface contract on every call (generative internally, but destructive to transmission-layer clients).

---

### Inscription
**Resistance type:** Plastic / Ductile  
**Definition:** Permanent deformation that absorbs energy and records force history — the structure does not return to baseline. The register writes the event into its own geometry.

**Domain examples:**
- *Music:* Leather patina on instrument cases and compressed felt of a piano hammer after years of use — material that carries its history of force in changed geometry and density.
- *Biology:* Callus and scar tissue: the body's permanent record of injury. Muscle memory formation: motor patterns become inscribed in motor cortex through repetition.
- *Software:* Append-only logs and event sourcing: events are permanently written into an immutable record; git history records every change with no baseline to return to.
- *Materials:* Crumple zones in automobile design — deliberately ductile steel members that absorb crash energy through permanent deformation.

**Canonical mismatch:** Using Inscription-type elements where elastic buffering is needed: mutable global state where reversible caching is required (each write permanently changes the baseline, so rollback is lost).

---

### Distribution
**Resistance type:** Hierarchical  
**Definition:** Resistance emerges from how elements are arranged, not from the elements themselves — routes force through organized multi-scale topology so no single element bears the full load.

**Domain examples:**
- *Music:* Violin body's internal bracing (bass bar, sound post) — the resistance of the body to string-induced vibration emerges from how the bracing routes force between top and back plates, not from the stiffness of any single element.
- *Biology:* Nacre's aragonite-polymer hierarchy — the arrangement of staggered platelets with organic interlayers is the toughening mechanism. Fascial network topology distributes joint load across the whole body.
- *Software:* Microservices with circuit breakers and supply chain redundancy — resistance to failure emerges from the arrangement of redundant paths, not from any single service's robustness.
- *Bow-arm:* Fascial network — whole-body tensegrity distributes bow-arm load across topology.

**Canonical mismatch:** Hierarchical structure with force concentrated through a single hub — apparent redundancy with actual single point of failure. A microservices mesh where all traffic routes through one gateway; a team with formal distribution of responsibility but informal concentration of decision power in one person.

---

### Filter
**Resistance type:** Viscoelastic  
**Definition:** Time-dependent, frequency-selective resistance — behaves rigidly under fast loads and compliantly under slow loads, separating signals by rate.

**Domain examples:**
- *Biology:* Ear canal ossicles transmit sound-frequency vibrations with high fidelity while absorbing low-frequency impact through a viscoelastic stapedius muscle reflex.
- *Software:* API rate limiting and token bucket algorithms — rigid to burst traffic, permissive to sustained low-rate access. Event debouncing in UI: ignores rapid repeated events, passes slow deliberate ones.
- *Cognition:* Kahneman's System 1/System 2 — fast automatic processing handles high-rate pattern recognition rigidly; slow deliberate processing handles rare, complex decisions compliantly.
- *Materials:* Silicone damping compounds absorb high-frequency vibration while allowing slow positional drift.

**Canonical mismatch:** Expecting uniform response regardless of rate — treating all urgency as equivalent (the same decision latency applied to urgent operational issues and long-term strategic questions). Rate-blind caching that treats all requests as equally time-sensitive.

---

### Threshold
**Resistance type:** Phase-Transforming  
**Definition:** Mechanism shifts at a critical load threshold — the element has a distinctly different resistance character above versus below the transition, and that shift is itself the functional event.

**Domain examples:**
- *Music:* Bow hair rosin threshold — below contact pressure, hair slides; above threshold, grip produces Helmholtz stick-slip oscillation (the tone-generating event).
- *Biology:* Immune activation — sub-threshold antigen exposure produces tolerance; above threshold it triggers full adaptive response.
- *Software:* Circuit breakers in distributed systems — sub-threshold failure rate allows requests through; above threshold the breaker trips and fast-fails all requests.
- *Bow-arm:* Helmholtz threshold — the precise contact pressure at which stick-slip tone begins.

**Canonical mismatch:** Failing to design the threshold position and transition character deliberately. A circuit breaker set too sensitively trips on normal variance; one set too high fails to protect. A threshold layer with an undesigned transition (no smooth ramp, immediate catastrophic phase shift) loses the generative function of the threshold itself.

---

### Tuning
**Resistance type:** Resonant  
**Definition:** Frequency-selective amplification and transmission — neither blocks all signals (rigid) nor adapts away from any (generative), but selectively amplifies specific frequencies while attenuating others.

**Domain examples:**
- *Music:* Violin body resonance chambers selectively amplify certain frequency bands (main air resonance and wood resonance peaks) while attenuating others, shaping the instrument's characteristic timbre.
- *Biology:* Cochlear tonotopy — different positions along the basilar membrane respond maximally to different frequencies, performing a biological Fourier transform.
- *Software:* Transformer attention mechanisms — selective amplification of relevant tokens in context, attenuation of irrelevant ones (not filtering, which would block, but tuning, which amplifies the signal of interest).
- *Bow-arm:* Instrument body as tuning layer shaping the sound produced by the bow-arm system.

**Canonical mismatch:** Using filtering (attenuation across a band) where tuning (selective amplification) is needed — or vice versa. A recommendation algorithm that filters content it predicts users won't engage with (filter bubble) rather than amplifying the highest-signal content while preserving the rest.

---

## Tier 2: Register Parameters

Parameters that measure specific properties within a register. Each parameter is a quantifiable dimension of its parent register's behavior.

### Stiffness
**Parent register:** Transmission  
**Definition:** Measures resistance to deformation under load — the magnitude of the transmitting element's rigidity. Young's modulus (E) is the canonical engineering quantity.

**Domain examples:**
- *Materials:* Young's modulus in structural steel — the quantitative stiffness parameter.
- *Music:* Bridge stiffness must be high enough to transmit all frequencies without absorbing them.
- *Biology:* Cortical bone stiffness is tuned to transmit mechanical load to trabecular networks.

---

### Elasticity
**Parent register:** Storage  
**Definition:** Measures the capacity to deform and return — the ratio of elastic energy stored to energy input. Quantified by the elastic modulus and the elastic recovery fraction.

**Domain examples:**
- *Biology:* Tendon elastic modulus — determines energy storage capacity during running gait.
- *Music:* Bow stick's elastic return — determines how much energy is returned between strokes.
- *Materials:* Rubber elasticity — high deformability, near-perfect energy return.

---

### Plasticity
**Parent register:** Inscription  
**Definition:** Measures the permanent deformation capacity — how much the element can be written before fracturing. Ductility is the mechanical correlate; synaptic plasticity is the neural correlate.

**Domain examples:**
- *Materials:* Ductility in mild steel — high plasticity enables crumple-zone energy absorption.
- *Biology:* Synaptic plasticity — long-term potentiation is the neural inscription parameter.
- *Cognition:* Cognitive plasticity in the ZPD — the window where inscription (schema formation) is possible.

---

### Damping
**Parent register:** Filter  
**Definition:** Measures energy dissipation per cycle — the rate-sensitivity coefficient of the viscoelastic element. Quantified by loss tangent (tan δ) in engineering materials.

**Domain examples:**
- *Materials:* Loss tangent (tan δ) in viscoelastic polymers — the engineering damping parameter.
- *Biology:* Stapedius muscle damping — attenuates low-frequency impact while passing audio-frequency signals.
- *Software:* Debounce interval — the time constant of the rate-sensitive filter.

---

### Compliance
**Parent register:** Transmission (inverse parameter)  
**Definition:** Measures deformability under load — the inverse of Stiffness. High compliance is dissipative in a Transmission role because it allows deformation that corrupts signal fidelity.

**Domain examples:**
- *Music:* A compliant violin bridge absorbs rather than transmits — high compliance here is a mismatch.
- *Biology:* Articular cartilage compliance at joints — functional in load distribution but dissipative in a pure transmission role.
- *Software:* A flexible type system — compliance in a transmission role leaks invariants.

---

### Hardness
**Parent register:** Transmission (surface parameter)  
**Definition:** Measures resistance to localized deformation at the contact interface — determines whether the surface transmits or absorbs contact forces. Vickers hardness (HV) is the canonical measurement.

**Domain examples:**
- *Materials:* Vickers hardness (HV) — measures surface resistance to indentation under load.
- *Biology:* Tooth enamel hardness (Mohs ~5) transmits masticatory load. Fluoroapatite over-hardness disrupts piezoelectric signaling downstream.
- *Music:* Ebony fingerboard hardness — transmits string contact forces without localized deformation.

---

## Tier 3: Cross-register Measurements

Measurements that span multiple registers. These are properties of systems or interfaces rather than of individual register elements.

### Strength
**Registers spanned:** Threshold  
**Definition:** The activation load at which the resistance mechanism transitions — the threshold value itself as a measurable quantity. Not an intrinsic material property but a system-level transition point.

**Domain examples:**
- *Materials:* Ultimate tensile strength (UTS) — the Threshold load at which fracture occurs.
- *Music:* Bow hair contact pressure at Helmholtz threshold — the Strength value for tone onset.
- *Biology:* Immune activation threshold — antigen count at which adaptive response triggers.

---

### Resilience
**Registers spanned:** Storage  
**Definition:** The maximum elastic energy a system can store without permanent deformation — the Storage ceiling before the system transitions into the Inscription register. Modulus of resilience is the area under the stress-strain curve to yield point.

**Domain examples:**
- *Materials:* Modulus of resilience — area under stress-strain curve to yield point.
- *Biology:* Tendon resilience — energy storage ceiling before plastic (Inscription) deformation begins.
- *Cognition:* Psychological resilience — elastic recovery capacity before schema fragmentation (Inscription).

---

### Toughness
**Registers spanned:** Storage + Inscription  
**Definition:** Total energy absorbed before fracture — includes both the elastic Storage phase and the plastic Inscription phase. Toughness = Resilience + plastic deformation area. This is why nacre (high Distribution coupling) achieves 3,000x the toughness of pure aragonite (high Stiffness, low toughness) — the cross-register measurement reveals what single-register optimization misses.

**Domain examples:**
- *Materials:* Nacre toughness — 3,000x aragonite alone, almost entirely from hierarchical Distribution, not material Stiffness.
- *Biology:* Bone toughness — controlled by collagen fiber orientation enabling both elastic storage and controlled plastic inscription.
- *Cognition:* Expertise toughness — capacity to absorb challenge (Resilience) and inscribe new schema (Plasticity) without cognitive fracture.

---

### Fatigue Resistance
**Registers spanned:** Inscription + Threshold  
**Definition:** The rate at which damage accumulates under cyclic load — how slowly the Inscription register fills under repeated sub-threshold loading. Fatigue resistance spans Inscription (cumulative damage) and Threshold (the load at which each cycle writes damage).

**Domain examples:**
- *Materials:* S-N curve (Wöhler curve) — fatigue life at each cyclic stress amplitude.
- *Biology:* Stress fracture formation rate — cumulative inscription from repeated sub-threshold bone loading.
- *Cognition:* Cognitive fatigue resistance — rate of working memory degradation under sustained germane load.
- *Music:* Repetitive strain in violin playing — cyclic bow-arm load accumulating below proprioceptive threshold.

---

## Structural Notes

The three-tier structure encodes a design logic:

**Tier 1 (Registers)** names the eight distinct types of resistance by mechanical character. The register determines *what the element does* when force is applied — transmits, stores, adapts, writes, distributes, filters by rate, transitions at threshold, or selectively amplifies.

**Tier 2 (Parameters)** quantifies how a register performs its function. Stiffness and Compliance are both parameters of the Transmission register — Compliance is the inverse, so it measures how far a transmission element deviates from ideal. Parameters allow calibration within a register type.

**Tier 3 (Cross-register measurements)** are properties that emerge from how registers interact at boundaries or across phases. Toughness cannot be computed from a single register — it requires both the Storage ceiling (Resilience) and the Inscription range (Plasticity). These measurements are the ones most diagnostic of system-level ergonomic design, because they reveal whether register transitions are well-designed.

The mismatch detection logic (see §4.7 of generative-resistance.html for the full matrix) follows directly: an element mismatched to a layer role is one whose register type produces the wrong behavior for the layer's generative function. The vocabulary network encodes which register types are compatible with which layer roles and what the failure mode is when they are not.

**Relation to load-path criterion:** The registers are all generative when load-path-coupled and dissipative when path-external. The taxonomy does not replace the load-path criterion — it operationalizes it by naming the *character* of the coupling. Knowing that the violin bridge is a Transmission register tells you it should have high Stiffness and low Compliance; knowing it is on the load path tells you that those properties are generative rather than dissipative in this position.

**Source document open questions** (not resolved by this extraction — see §5 of generative-resistance.html):
- Q5.1: Operational test for path-coupling in cognitively complex systems
- Q5.2: Multi-output load path design (element must serve different registers simultaneously)
- Q5.3: Universal characterization of the antifragility (hormetic) window in load-path terms
- Q5.4: Whether path-external resistance always produces proprioceptive masking, or only in certain domains
- Q5.5: Aesthetic correlate of load-path coupling — whether felt coherence tracks structural coupling mechanistically
