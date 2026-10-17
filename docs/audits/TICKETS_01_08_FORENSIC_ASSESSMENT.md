# Tickets 01–08: forensic assessment

**Assessment date:** 2026-10-17 UTC. **Repository:** `b33cc3f` plus the working tree present at audit start—not merely that commit. **Ticket 09 is excluded.**

## Verdict: the useful current ceiling

Happy Path has a functioning **small, seeded, stateful constrained-itinerary prototype**. Its offline safety rules, finite-candidate Pareto filtering, rollback on infeasible sequential mutations, duration baselines, and parallel timeout handling are substantive engineering. They are worth preserving and repairing, not replacing wholesale.

It does **not yet demonstrate a dependable real-trip autopilot**. Independently reproduced failures let the public API publish a closed-venue visit after live retiming, price a 1,195 km minimum-distance trip as 119 driving minutes, draw an alternative branch through its excluded main-spine region, split workers into different stores during ordinary SQLite contention, and acknowledge concurrent mutations while losing one update. The documented startup command also reseeds away session state.

The most important gap is **composition**: individually working solver, decoration, storage, and geometry pieces do not preserve the intended physical and state guarantees when connected. More repetitions of the existing hourly sweeps would not repair that.

### Evidence and boundaries

- Read the governing freeze documents, all eight ticket contracts, the production owners and their API callers, schema/seed compiler, relevant configuration, public contract, and complete in-scope tests. Byte comparisons established that the scratch ticket copies are identical to the read contracts.
- Executed the existing ticket 01–08 gates against **disposable databases**. Did not run `make test`, `make run`, or `make openapi`: all depend on `seed` and would replace the working database. Did not select ticket 09 or collection tests.
- Compared the actual working database with a fresh seed compilation. All rows in `corridors`, `corridor_branches`, `pois`, `corridor_edges`, and `poi_co_occurrence` matched exactly.
- Used real SQLite, a genuine separate Python process, real delayed TCP sockets, and the actual FastAPI routes. Controlled HTTP responses are explicitly **fault injection**, not observations of Goong traffic or real venues.
- Attempted a credential-bearing request to the configured official Goong endpoint using the existing local credential, without printing it. It failed with `ConnectTimeout`. A separate credential-free public connection check also timed out. **Provider interoperability and actual traffic accuracy remain unestablished, not disproved.**
- Production source, existing tests, seed JSON, and the working database were not edited. Only this report and isolated audit scripts/receipts were added.

## 1. What the gates actually establish

| Execution | Observed result | Meaning |
|---|---|---|
| Default Python: FastAPI 0.136.3 / Pydantic 2.13.4 / httpx 0.28.1 | **12/12 selected tests pass**, 7.766 s | Current in-scope gates are green in this environment. |
| Repository `.venv`: FastAPI 0.99.1 / Pydantic 1.10.26 / httpx 0.27.2 | **11 pass; ticket 08 errors** | `ParetoCandidate.model_dump()` does not exist. The socket and congestion checks are not reached. Pydantic 1 is allowed by `pyproject.toml:15`. |
| Eleven isolated, in-memory ablations | **11/11 detected; assertion failures, no incidental test errors** | Existing gates genuinely depend on important behavior. This is not a coverage-count claim. |
| Whole `/plan`, three genuinely delayed socket requests per call | **0.2974 s cold; 0.3302 s warm**, HTTP 200 | The timeout design meets the 350 ms target in these two local observations. This is not a production latency distribution or a bound on database contention. |
| OpenAPI artifact versus default runtime | Both **3.1.0**, structurally identical; coordinate schema identical | The three requested public routes and bounds are registered coherently in the default environment. |
| Independent 2× live-leg perturbation | **216 candidates examined; 59 stop-level operating-window/UV violations** | Final decorated candidates are not revalidated. Some operating-window violations concern the separate 24/7-midnight modeling problem, so this is not “59 genuinely closed businesses.” |

Ablations removed session writes, the closing guard, dominance filtering, exertion cap, co-occurrence influence, anchor bonus, EV requirement, custom-origin geometry, commit-after-feasibility ordering, parallel decoration, and arrival-clock propagation. Each corresponding existing gate detected the intended effect. Sequential decoration measured approximately **0.847 s**, causing the delayed-socket gate to fail.

These are valuable tests. Keep them. Their weakness is the untested integration boundary and external grounding—not a lack of ceremonial test volume.

## 2. Ticket-by-ticket operating ceiling

| Ticket | Demonstrated capability against its freeze gates | Current limitation and repair value |
|---|---|---|
| **01 — storage/contracts** | Real separate-process session visibility; restart-style registry reads; deterministic shared-path fault injection; corruption/missing-table checks; coordinate bounds; interruption before atomic publication preserves the original database. | Ordinary lock contention selects the fallback store; startup can proceed without a DB and serve 500; `make run` reseeds state away. **High-value, bounded lifecycle repair.** |
| **02 — express/schedules** | The 48-request bidirectional sweep, real directional seed tags, early wait/closing boundaries, five-minute floors, and endpoint/stop inclusion checks pass. | Valid only against supplied hours and generated edge costs before live decoration. Timezone offsets are ignored; nominal 24/7 service cannot cross midnight. **Preserve the rule engine; repair clock semantics and final validation.** |
| **03 — branches/Pareto** | Stop-sequence interchange constraints and independent 5D comparisons pass the Da Lat direction/hour/powertrain sweep. The frontier includes feasible unselected alternatives rather than only comparing the final three. Shortfall warnings work. | Stop-label legality does not establish legal rendered geometry. “Pareto” is relative to the finite seeded one-to-three-stop candidate universe and supplied scores, not all feasible real roads or demonstrated user utility. **Repair geometry; retain the mathematical filter.** |
| **04 — UV/pacing/anchors** | UV shielding, two-HIGH rejection with a positive control, night open-hour checks, and genuine scoring ablations work offline. | Live retiming bypasses those schedule checks. The arrival-based anchor wording has an overnight counterexample despite a feasible morning anchor; a numeric bonus is not a hard guarantee. **High-value safety repair; clarify the anchor contract rather than escalating arbitrary weights.** |
| **05 — EV/tolls** | EV placement, static open hours, branch compatibility, and frozen toll constants pass the specified sweeps, including all seeded branches. | A boolean charger tag and ordinal progress window do not demonstrate a usable physical charger or battery feasibility. Frozen branch toll arithmetic is not independently verified actual road usage, especially for custom slices. **Keep the constraint; ground its inputs before claiming anti-stranding capability.** |
| **06 — arbitrary pins/slicing** | Piecewise projection near the supplied control points, exact encoded endpoints, slice-lock rollback, short direct routes, and minimum leg floors work in the specified examples. | Off-corridor pins are silently priced as on-corridor; first/last leg scaling is not residual-distance slicing; branch geometry is not branch-following road geometry. **Essential repair: this is currently a nearby-pin slicer, not arbitrary-GPS routing.** |
| **07 — mutations** | Sequential 409 rollback, no silent archetype substitution, action validation, removal history, and live duration delta persistence are demonstrated. | Read/solve/decorate/write is not serializable: overlapping successful requests lose updates. Unknown sessions with `corridor_id` are fabricated from defaults. **Preserve copy-on-write; repair concurrency and remove undocumented reconstruction.** |
| **08 — live decoration** | Persistent client, real parallel socket timeout, complete positive leg handling, null-polyline protection, clock propagation, mutation decoration, and signed duration deltas work in Pydantic 2. | Final safety is unchecked, obsolete waits survive, smaller live changes are deliberately ignored, failures silently return offline output, and actual Goong protocol behavior was not reachable. Pydantic-1 gate execution is broken. **Useful bounded adapter, not yet a safety-preserving live planner.** |

## 3. Reproduced defects that materially limit real use

### A. Live decoration publishes and persists infeasible schedules

**Owners:** `src/happy_path/clients/goong.py:76–105`; `src/happy_path/api/routes.py:73–96,213–224`.

For HCMC → Vung Tau, departure `16:00`, the actual `/plan` route was given complete, positive, controlled Goong legs at **2× its own offline leg durations**. It returned **HTTP 200** with `P1_EXPRESS` servicing `vt_banh_khot_bai_sau` at **22:48**, although the seed says it closes at **21:00**, with a 45-minute dwell. The live-decorated duration, **481 minutes**, was then stored as the session baseline.

For Da Lat, a `01:00` departure's `P2_ICONIC` tea stop moves to **11:04** under the same perturbation, inside the unconditional UV block. The 216-candidate probe finds other instances; the public-route probe independently establishes that a closed visit is not merely an unused intermediate object.

A genuine opening wait also becomes obsolete: the specified breakfast case initially arrives **06:06**, waits **24 minutes** until 06:30, then a controlled live leg moves arrival to **07:06**. The returned plan still waits 24 minutes and starts service at **07:30**. `srv_m = arr_m + stop.wait_minutes` copies the old wait instead of deriving it from the venue's opening time.

The current congestion test injects an artificial seven-minute wait and expects it to be carried forward (`tests/test_corridors_e2e.py:849–881`). That is useful clock-arithmetic coverage but is **not an independent oracle for opening-hour waiting**; it endorses the stale-wait behavior.

**Worth repairing:** absolutely. Derive waits and absolute stop clocks from live legs and venue metadata, then validate the final candidate before returning or committing it. A known-invalid live result must not be published as feasible. Preserve prior mutation state if no valid requested candidate remains. Check all applicable invariants at this boundary, not just duration arithmetic.

### B. Ordinary SQLite contention creates split-brain storage

**Owner:** `src/happy_path/core/graph.py:196–233`, especially `except sqlite3.OperationalError` at line 208.

On a normal local filesystem, held a real `BEGIN IMMEDIATE` writer lock while a second registry started. After **5.064 s**, the contender copied the database to `/tmp/happy_path_shared_<hash>.db` rather than treating the failure as normal busy contention. The original worker continued using the primary database. A subsequently committed primary session was **invisible to the contender**.

The existing unsupported-lock test is valid fault injection, but its custom connection raises a disk-I/O error precisely on `BEGIN IMMEDIATE`. It does not distinguish unsupported filesystem locking from ordinary `SQLITE_BUSY`/`SQLITE_LOCKED` behavior. Catching the same Python exception class is not sufficient classification.

**Worth repairing:** high. Normal contention must never choose a new authoritative store. Distinguish contention from a demonstrated filesystem-lock failure, and keep all workers on the same authority. This is a present reproducible problem, not a speculative distributed-systems requirement.

### C. Documented startup defeats restart durability; missing DB does not fail the app fast

**Owners:** `Makefile:10–20`; `scripts/seed_db.py:17,59,170`; `src/happy_path/main.py:11,27`.

`run: seed`, `test: seed`, and `openapi: seed` all invoke a compiler that replaces the target with a new seed database. In a disposable reproduction, a saved session survived a genuine separate process but disappeared after running that compiler again—the behavior `make run` requests on every start. The compiler's CLI default path also does not follow `HAPPY_PATH_DB_PATH`.

Separately, launched the actual app with a nonexistent configured database. Its lifespan completed instead of raising; `/v1/corridors/health` returned **500** because no registry was installed. The class-level missing-file guard exists, but the entry point skips calling it.

**Positive finding:** atomic publication itself is meaningful. Injecting an interruption exactly before `os.replace` left the prior DB byte-identical and its session readable, with no destination staging file left behind.

**Worth repairing:** high and small in scope. Separate explicit data reset/build operations from normal serving, testing, and contract export. Initialize the configured registry unconditionally so missing/corrupt state fails startup. Do not replace a working database to run a verification command.

### D. Two successful mutations lose an acknowledged update

**Owners:** `src/happy_path/api/routes.py:127–224`; `src/happy_path/core/graph.py:442–477`.

Forced overlap at the existing awaited decoration boundary:

1. `SHIFT_DEPARTURE` from 06:00 to **06:01**.
2. `SKIP_CATEGORY` for **viewpoint**.

Both requests returned **200**. Persisted state contained the skipped category but departure was back at **06:00**. `BEGIN IMMEDIATE` covers the final replacement only, not the state version read earlier. A transaction around the last write does not make the whole mutation atomic with respect to another mutation.

**Worth repairing:** high. Serialize mutations per session or use a checked revision/compare-and-swap commit that rejects a stale update. Do not hold a database-wide write lock through an upstream network await. Sequential 409 rollback is real and should remain intact.

### E. Legally tagged branch stops produce an incompatible route drawing

**Owner:** `src/happy_path/core/graph.py:397–440`; `data/seed_corridors.json:36–108`.

For Da Lat at 06:00, `P3_SCENIC` selects `BRANCH_QL28B_DAININH`. Its polyline nevertheless includes main-spine control points at **km 64,114,148,190,228**, all strictly inside the branch's **(55,262)** exclusion region. The Bao Loc loop candidate likewise includes the main-spine km 190 point inside its excluded interval.

This is not a claim that the stop-sequence gate itself failed: selected POI labels pass that gate. It is a failure of the stronger road-following/physical-execution intention. `build_sliced_polyline` merges *every* main-spine waypoint without any branch geometry. Vertex counts, exact endpoints, and inclusion of stops cannot establish that intervening segments follow a legal road.

**Worth repairing:** essential. Use actual branch-aware road geometry and access segments. Do not fix this by inventing extra vertices or simply deleting offending points and drawing a new straight chord.

### F. “Arbitrary pin” acceptance can hide 1,195 km of travel

**Owners:** `src/happy_path/core/graph.py:364–395`; `src/happy_path/core/solver.py:167,233,280`.

Submitted HCMC → Vung Tau with origin at the real Hanoi coordinate **(21.0285,105.8542)**. The coordinate passes the required Vietnam box. `/plan` returned **200**, projected start to **km 0**, and assigned **119 driving minutes**, with no warnings. Origin-to-destination great-circle distance alone is **1,195.1 km**, requiring an impossible minimum average of **602.6 km/h**. The endpoint test still passes because the Hanoi coordinate is literally encoded first.

Even a correct nearby pin has faulty cost slicing: from exact Da Lat spine **km 114** to a locked **km 190** stop, the first leg is **117 minutes**. Applying the *same seed compiler's* speed and detour model to the actual remaining **76 km** gives **75 minutes**. The latter is an internal consistency comparison, **not a measured real-road ETA**. Multiplying the canonical origin-to-stop edge by the whole active-span ratio is not slicing that edge at the custom origin.

**Worth repairing:** essential. Either price genuine road access from the pin to the supported corridor or reject an unsupported access request explicitly. Do not add an arbitrary distance cutoff as a substitute for establishing corridor membership. Derive edge costs from the actual sliced path, not a whole-trip multiplier.

## 4. Specifications and scientific claims that need tighter interpretation

### Calendar and timezone semantics

`parse_departure_minute_and_bucket` uses the supplied datetime's hour without converting its offset. Two representations of the **same instant** produce different plans:

- `2026-10-16T00:00:00Z` → **00:00**, rain/evening bucket.
- `2026-10-16T07:00:00+07:00` → **07:00**, peak bucket.

Dates are otherwise reduced to a repeating 1,440-minute clock. The “48-hour” gate is 24 sampled departures × two directions on one date, **not 48 elapsed hours of calendar-specific venue validation**.

A seeded 24/7 rest stop arriving at **23:55** with 20-minute dwell is rejected **409**, because `1435 + 20 > 1440`. This follows the literal numeric closing-window rule but contradicts the physical meaning of 24/7 service. Repair the representation for continuous availability without weakening genuine closing constraints. Offset normalization or explicit offset restrictions also need a coherent public contract.

### Arrival anchors: influence is proven, universal inclusion is not

Across 144 corridor/direction/hour portfolios, inspected 77 morning-arriving non-express candidates. One lacked an anchor despite a feasible morning-arriving anchor alternative: **Nha Trang → HCMC, 22:00 departure, P3** arrives **09:23** without an anchor; locking `nt_phan_rang_culinary_ql27` gives an anchor route arriving **09:35**.

The chosen and alternative objective vectors are respectively `(-628,-7,94.5,71.5,-0.07)` and `(-630,-10,95.5,64.5,-0.09)`; neither dominates the other. It is not simply an unavoidable dominance rejection.

This contradicts the broad **arrival-window-based** wording of F4.4. It is **not** a demonstrated counterexample to the narrower wording “morning departures”: this trip departs at night. Resolve which intention governs. A +38 bonus demonstrably influences the canonical morning benchmark but cannot by itself prove a universal inclusion guarantee. If inclusion is mandatory, express it as a feasibility rule; if it is a preference, describe the actual tradeoff.

### EV and toll correctness is conditional on model inputs

The frozen EV placement rule is implemented: a branch-compatible `has_ev_charge` stop lies in the 15–80% **ordinal corridor-progress** window for long spans, subject to the shared service-window guard. A direct read of the working DB found **all 12 charger-tagged POIs marked [0,1440]**. Thus the EV sweep's open-charger assertions largely depend on always-open seed tags. Other existing tests do challenge the shared closing guard on a restaurant; duplicating that same logic check would not ground charger operating hours.

The wording “open at arrival” in ticket 05 also differs from ticket 02's permission to wait briefly before opening; these always-open inputs do not resolve that ambiguity. Neither rule establishes connector compatibility, operational availability, energy sufficiency, or the true distance/time of a branch. Those capabilities are not represented by the current contract.

Tolls match the frozen constants. The test explicitly expects **309,000 VND** for a **20 km [400,420]** Nha Trang direct slice (`tests/test_corridors_e2e.py:544–551`). That demonstrates literal concordance, not that the vehicle physically crosses the full corridor's toll stations. No independent current tariff/station-usage validation was established here. The slice toll model needs actual station/segment usage if that is the intended product meaning; merely changing the expected number would not supply it.

### Unknown-session reconstruction is not durable recovery

`/mutate` with a never-created session ID and a valid `corridor_id` returned **200** and created a session (`api/routes.py:128–145`). This fabricates ICE/default-trip state; it does not recover the previous user's coordinates, powertrain, constraints, or displayed durations. Remove this undocumented fallback unless a separately specified reconstruction contract is actually required.

### Real Goong and the latency ceiling

The adapter requests `/direction` with semicolon-delimited destinations. Its current tests provide matching payloads themselves; they do not prove the actual provider's multi-stop request/leg contract, API version, routing restrictions, or departure-specific traffic semantics. No departure timestamp is sent upstream.

Only live sums above **1.35×** trigger schedule changes; smaller increases and faster travel retain offline timings by design. Exceptions and unsuccessful requests silently retain offline output. Our genuine delayed-socket calls met the target but returned no timeout/source warning, so a consumer cannot distinguish that fallback from successful decoration using the response alone.

The shell had no exported Goong key. A local credential is available to the explicit CLI loader, but the runtime client uses the environment-only constant (`config.py:6,12–25`); the default commands do not automatically activate the local `.env` credential. This configuration distinction must not be confused with successful live integration.

## 5. Data authenticity and test-gaming assessment

### The planner currently consumes a deterministic demonstration model

The working database contains **25 POIs, 9 branch rows, 1,040 edges, and 15 co-occurrence pairs**. Its five planning tables match a fresh seed compilation exactly. The compiler creates:

- distances from absolute corridor-kilometer differences;
- travel minutes from fixed **62/52/38/42 km/h** bucket speeds plus a hand-written detour formula;
- edge polylines from two endpoint coordinates;
- heuristic hazards and hand-authored utility/scenic values.

All 25 seed place identifiers are locally named `goong_*` strings. The consumed DB has no associated verified-POI or co-occurrence-source rows. Specific charging/24-hour claims often cite a generic operator locator or government homepage, not an outlet-specific evidence record. This establishes **lack of demonstrated grounding of the planner's current inputs**, not that a named business definitely does not exist. Collection implementation and ticket 09 completion are not judged here.

The sparse direct fallback assigns utility **80**, scenic **60**, hazard **0.05**, and speed **52 km/h** directly (`solver.py:486,513–515`). These are authored estimates, not measurements. Adjacent-pair bonuses prove that the scoring function changes its choice; they do not demonstrate a learned/validated Markov preference model or improved family-trip outcomes.

Offline estimates and fixture responses are allowed by parts of the freeze contract. Their existence is not automatically cheating. **Presenting them as actual acquired road data, verified charger availability, or externally validated planning quality would be unsupported.** Passing an internally consistent fixture is not independent evidence that the fixture describes the world.

### Concrete historical gaming patterns—and their current state

The sole recorded baseline contains two especially clear artifacts:

1. `b33cc3f:src/happy_path/core/graph.py:421–425` manufactures a midpoint offset by **0.0015 degrees** to ensure an extra vertex. That satisfies a shape expectation without supplying road geometry.
2. `b33cc3f:tests/test_corridors_e2e.py:219–237` calls source-string presence checks an “ablation gate” and boosts one co-occurrence pair to **60** before checking route influence. That does not establish the influence of the actual unmodified data.

**Both patterns have already been removed in the current working tree.** The current seed-only preference differentials and real socket test are materially stronger. They should not be replaced with the historical theatre. No current test was established as safely redundant enough to delete during this assessment; deleting meaningful guards would reduce confidence rather than remove theatre.

The newly executed ablations show that important constraints are active. They do not retroactively establish that tests were written before implementation, that historical completion reports were accurate, or that the model is externally valid. The available Git history has one baseline plus substantial uncommitted changes, not a reconstructible red/green execution history. There is evidence of historical test-gaming patterns and current overclaim risk, **not evidence sufficient to attribute deliberate dishonesty to a person**.

## 6. Engineering decisions worth making, in dependency order

### First: preserve authoritative state

Separate serving/export/test commands from seed reset; honor the configured DB path; fail missing DB at startup; prevent contention-induced store forks; make overlapping session commits conflict-safe. Existing rollback/delta machinery is worth retaining.

**Meaningful proof:** the already reproduced restart/reseed, real contention, and two-overlapping-mutation cases. No additional inventories, random seeds, or broad gate-count targets are needed.

### Second: make the published itinerary the unit of physical validity

Live decoration cannot be a cosmetic mutation after feasibility is settled. Derive new waits, clocks, and totals consistently, then validate the exact returned/committed candidate. Preserve the previous session on an infeasible mutation. Do not publish a known closed or UV-invalid visit to satisfy a latency target.

**Meaningful proof:** the 16:00 public-route closed visit, the Da Lat UV shift, and the genuine 24-minute opening-wait case. Extend the strongest existing owner-boundary test rather than duplicating the same assertion across every layer.

### Third: make path, progress, ETA, and toll usage refer to the same route

Retain explicit interchange metadata, but use grounded main/branch road geometry and actual sliced access costs. Endpoint equality must remain a necessary condition, not a sufficient routing oracle. Establish supported-pin behavior explicitly. Resolve slice toll semantics against actual usage rather than a test's frozen full-corridor total.

**Meaningful proof:** the QL28B forbidden-spine coordinates, Hanoi-origin case, and km114→km190 residual-cost inconsistency. Consume road evidence from the appropriate data workstream; this is not authorization to silently substitute new invented geometry.

### Fourth: reconcile the contract with the actual safety/preference model

Resolve continuous 24/7 service, timezone handling, arrival-versus-departure anchor priority, and unknown-session behavior. Align test APIs with the declared supported Pydantic range. These are specific observed ambiguities/failures, not an invitation to redesign the whole product.

### Fifth: establish external function, not merely internal consistency

With a reachable provider, verify a genuine multi-stop request through the existing adapter and compare returned geometry/legs with the exact candidate. Establish operational evidence for the particular branch/charger/access facts the planner relies upon. Evaluate preference quality separately from mathematical non-dominance: removing a bonus proves causal influence, not better journeys.

The current search enumerates at most **three stops** per sequence; its pairwise frontier work grows with the generated candidate pool. The present 7–11 POIs per corridor and two observed latency samples do not establish capacity for a much larger live catalog. Do not build a synthetic scale benchmark merely to claim readiness; measure the actual catalog/workload when expansion is concrete.

**Overall repair judgment:** storage/state and post-live safety repairs are relatively concentrated and high leverage. Grounded routing geometry and outlet/road facts are the more substantial dependency. More prose, blanket full-suite reruns, or tuning bonuses to one canonical route are poor substitutes for those repairs.

## 7. Reproduction artifacts

All scripts use disposable databases and explicitly exclude ticket 09:

```bash
python3 .scratch/happy-path-autopilot/audit-01-08/run_existing_gates.py
.venv/bin/python .scratch/happy-path-autopilot/audit-01-08/run_existing_gates.py
python3 .scratch/happy-path-autopilot/audit-01-08/probe_runtime.py
python3 .scratch/happy-path-autopilot/audit-01-08/probe_semantics.py
python3 .scratch/happy-path-autopilot/audit-01-08/probe_ablations.py
```

Receipts in that directory: `artifact_inventory.json`, `existing_gates.json`/`.log` (default Python), `existing_gates.pydantic1.json`/`.log`, `runtime_probes.json`, `semantic_probes.json`, and `ablations.json`. The gate runner propagates test failure through its exit status; intentionally adverse probes report observations rather than disguising them as successful production operations. The gate runner overwrites its unversioned receipts when rerun.

Working DB SHA-256 before and after the independent runtime/semantic probes:

`b535fa94b5094a8a53b9361a94180a0af1d3ddb5277a803087d3c47c263ea7a7`

Seed JSON SHA-256: `b98d17b1016dc3bb3e4f9b961da31c1bc6c90db639067c4744dc244558b17b8c`.

Existing in-scope test file SHA-256: `2bbe533129a0426f6d3a961bae3e70f0dfdc2cb54dc3fa16bf420d5f95874d11`.

**Final assessment:** valuable constrained-planning machinery is present, but the demonstrated ceiling remains a seeded prototype with known cross-layer physical and state failures. This assessment supplies executable counterexamples and repair priorities, not a completion-label exercise. Ticket 09 remains unassessed.
