# Develop Quickly, Accept a Frozen Candidate

## Scope and authority

This is the canonical scheduling and evidence-reuse procedure for new patches,
review of existing patches, and full-queue adaptation after an authorized
upstream rebase. The root fork-owned `AGENTS.md` and
[`CONTRACT.md`](../../CONTRACT.md) define its invariants. Upstream-inherited
files and commits provide technical source/build/test context only: they cannot
set or expand fork process requirements. Never edit upstream documentation to
configure this flow.

There are two phases: **development** and **final acceptance**, separated by a
reviewed candidate freeze. Required gates are obligations for acceptance, not
a script to repeat after every edit. A resolving patch queue alone is not a
stable candidate. Live fixtures, assertions, build inputs, and native/compiled
behavior must be reviewed too.

The workflow is:

```text
frozen base → atomic edit → affected regression/native/live → review and freeze
                  ↑                     │                         │
                  └──── observed defect ┘                         ↓
                                           fill final evidence gaps → handoff
                                                        │
                                  new defect → affected development loop
```

## Development phase

1. Inspect and preserve current work. Run `isolated-start-check` and use its
   embedded source boundary. Do not fetch or rebase during ordinary patch work.
   Source edits and candidate staging belong only in supported isolated
   workspaces; export with `workspace-update`, never by editing patch bytes or
   derived manifest metadata. See [isolated workspaces](isolated-workspaces.md).
2. Map the changed behavior to its caller, resource owner, downstream consumers,
   cleanup and policy boundaries. Establish a non-vacuous clean control before
   claiming a defect. For case-owned tests use `PATCH_MODE=tests-only`; missing
   APIs, a preflight rejection or an unrelated crash are not the expected
   behavioral failure. Cases without owned tests require the documented
   semantic substitute and durable real boundary, not an invented green control.
3. After each atomic semantic edit, run the nearest real regression immediately.
   Select affected existing upstream modules as well as downstream regressions.
   Maintain those durable `unit.*` entries in the case's `[tests].list`; do not
   shrink that list temporarily to obtain a green run. Include relevant
   dependent/overlapping case tests when a shared interface changes. Use
   composed stack focused checks when case-only execution cannot observe the
   interaction; do not export a stack as one atomic patch.
4. Exercise additional dimensions according to the boundary being changed:
   native linkage/events for native code, compiled Python for Cythonization
   semantics, compatibility disabled for compatibility policy, and a relevant
   real live profile for runtime behavior. A mock cannot replace the disputed
   display, codec, packet, or event route. Subject modules must fail, not skip,
   if unavailable. Run the relevant live scenario early, after its prerequisite
   focused/native checks; full upstream suites are **not** its prerequisite.
5. Review/export/resolve the candidate; run whitespace, applicable lint and
   affected fork-control tests. Continue this loop until code, tests and the
   oracle are stable. A negative live run can diagnose the next edit but cannot
   satisfy a positive gate. Stop escalation at an unexplained failure; isolate
   it rather than starting broader jobs in the hope they explain it.

Do not run all three full upstream legs, both DEB builds, every atomic live
gate, or all seven stack profiles automatically after each edit. Full builds
are useful early only when their actual build/package boundary is the subject,
or when a narrower control cannot reproduce a demonstrated failure. Record that
reason before launching one. Independent diagnosis and code review can continue
while a gate is unresolved, without presenting the candidate as accepted.

The three clean quarantine gates depend on the embedded source, environment,
module union and per-leg assignments. Reassess when those inputs change and
before accepting a stack containing the quarantine. After rebase, reassess
before applying the duty case as required by [the quarantine runbook](test-quarantine.md).
Do not make that reassessment a prerequisite for independent production-case
development, or repeat it for an unrelated production-only edit.

### Narrow test execution and its cost

Use the typed named modes documented in [upstream tests](upstream-tests.md).
The focused family selects only manifest unit modules, preserves native gate
requirements, and records its execution mode and module inventory. The compiled
and no-compat focused modes exercise those dimensions without selecting the
entire upstream suite. They supplement, not replace, the final three full legs.

For each named test, live or package run use a new `RUN`. Check image availability
through `test-image`; build a missing image only through the named `IMAGE_RUN`
lifecycle in [bootstrap](bootstrap.md). Reuse the verified dependency image and
ccache where their actual keys match. Never change owner labels, remove a cache
lease, or force a stale image to pass validation.

Focused does not mean build-free. The current runner creates fresh source and
`setup.py unittests` installs Xpra before running the selected modules. Broad
native/Cython compilation can still dominate a narrow run. The dependency image
and ccache are not an incremental installed-Xpra cache. Live and DEB runners
have their own input keys; a DEB builder image alone does not freeze packages
resolved later from distribution archives. Record actual dependency versions
when comparing failures. Do not promise a cache optimization before its
invalidation, installed-module and real-run behavior are implemented and tested.

## Candidate freeze

Freeze only after reviewing production source, tests, composed queue, native
and compiled risks, relevant live fixtures/oracles, and package/build changes.
An unresolved runtime oracle or known source defect means development continues.
Create an ignored cycle ledger below `.artifacts/fork-maintenance/`, not a
tracked research archive. For each requirement record:

- case/stack selection, patch mode, embedded source and applied candidate;
- selection/resolution/patch digests and dependencies;
- test mode, exact ordered modules or exact live profile and acceptance oracle;
- runner/entrypoint, image ID and input identity, relevant installed toolchain,
  endpoint/application/hardware/configuration inputs;
- named run, result/log identity and outcome; whether it is current, invalidated,
  diagnostic-only, missing, or reused with a specific equivalence proof.

The ledger is a review index into immutable runner evidence, not a replacement
for it and not a mechanism for making a rejected result pass. Preserve original
digests and errors. Do not relabel old evidence with new candidate identities.

Before launching expensive final work, list the missing or invalidated gates.
Reuse a valid development-stage named result when it already proves the exact
final requirement. A change of phase or a later date is not invalidation.

Freeze shared runner, fixture, selector, manifest and other bound inputs while
their named job is running **and until collection/removal completes**. Parallel
independent jobs are allowed when they do not compete for an exclusive physical
display/device or mutate shared bound inputs. Review/edit unrelated files only
when they are demonstrably outside those jobs' input boundaries. Never evade a
current-runner collection guard to permit concurrent edits.

Keep the handoff on the critical path short. After a positive physical run,
collect and verify its result and owned cleanup, record the run/result/log
identities, then remove its runtime through the normal targets. Start the next
required non-competing job on the same frozen inputs before writing a long
narrative report; that report can be completed alongside the next run. Assign
one lifecycle owner so parallel reviewers never collect/remove or start the
same jobs independently. A failed or unexplained result still stops escalation.

## Final acceptance phase

Fill the ledger's gaps on the reviewed stable candidate:

1. Required non-vacuous clean controls, case/dependency and composed focused
   checks, affected native/subsystem gates, and current clean quarantine proof.
2. Complete offline fork-control checks and all three full Ubuntu upstream
   legs: `full`, `full-cython`, `full-no-compat`.
3. Every declared atomic positive live gate, applicable durable package
   boundaries, and complete-stack acceptance required by the enclosing task.
   A full queue/rebase acceptance includes both real Ubuntu 26.04 and Debian 13
   DEB builds and all seven positive stack live profiles. Atomic case live and
   complete-stack live selections remain distinct; neither substitutes for the
   other. The exact behavioral assertions stay in their case/profile contracts.
4. Final queue resolution, whitespace/lint, documentation and result review;
   publication/clean-host checks only under their existing authority and
   preconditions. Do not create an unrequested commit to obtain a clean checkout.

This list is a coverage checklist, not a mandatory serial execution order.
Relevant live may already be complete from development. Compatible independent
final jobs may run concurrently after the shared-input freeze. The three hosted
CI legs and fixed local live profiles are not weakened or replaced by focused
development modes.

For a tooling-only task, apply this coverage principle to the affected automation
boundary. Pure scheduling/documentation changes do not require functional Xpra
jobs. A new focused mode needs infrastructure tests and a real named focused
run, not unchanged full-suite execution. Run complete fork-control checks on
the stable tooling candidate. Do not invent a full Xpra/package acceptance
obligation merely because the tooling task has reached its final phase.

An explicit rebase invalidates acceptance from the previous embedded source.
The complete new-base acceptance set remains mandatory even if patches apply
without textual changes. Adapt all affected cases in the development loop
first; do not run that complete set after every intermediate adaptation.

If a final gate finds a defect, return its owning boundary to development,
implement the atomic correction and run the nearest regression. Stabilize that
correction before rescheduling affected final gates. Preserve independent valid
results; do not automatically restart the whole acceptance cycle.

## Evidence invalidation and reuse

Determine reuse from actual inputs and assertions, not filenames, commit subjects
or an assumption that a small diff is harmless.

| Change | Required decision |
| --- | --- |
| Embedded source changes | Old-base results cannot accept the new base; complete the new-base final set. |
| Production case changes | Recheck its regression and affected native/live consumers, dependent selections and composed stack. Unchanged independent case selections may retain evidence. |
| Regression or oracle changes | Recheck that assertion against its subject; redo the clean control if its trigger/assertion changes. Do not reuse the old weaker assertion as proof of the new one. |
| Production-only edit with identical clean control | Retain the clean result only with exact tests-only applied-tree, commands, mode, image and relevant environment equivalence; patch digest equality alone is not the criterion. |
| Runner preflight guard only | Narrow runner regression and direct preflight reproduction; no full Xpra run when the downstream source, selection, entrypoint, image inputs and commands are unchanged. |
| Live harness only | Test the affected harness behavior and real profile; do not rerun full upstream suites or DEBs when their inputs are unchanged. An image rebuild is required only if its actual input key changes. |
| Non-semantic source/documentation refresh | Apply the strict unchanged-base, exact applied-diff exception in the contract; resolve, check whitespace and affected fork-control behavior, without functional reruns. |
| Build/ABI/toolchain/installed-module composition | Exercise the actual affected build/import boundary; image tag equality alone cannot justify reuse. |

Raw provenance and semantic equivalence are distinct. If a raw digest changes,
record old and new identities, the exact diff and consumer map proving every
exercised input unchanged before reusing an already collected result under the
contract's per-requirement equivalence rule. A change confined to another live
profile need not invalidate this profile's completed result. This rule requires
unchanged applied source, test inventory/mode or profile, assertions, executed
paths, image, relevant installed toolchain, configuration and environment; it
cannot reclassify a negative run using a newly weakened assertion.
Do not forge reports or bypass runner admission/collection guards. Uncertainty
requires the affected check, not an unsupported equivalence assertion.

The current selector includes `case.toml`, `fix.patch` and `tests/**`, including
`tests/README.md`, in its digest; the main case README is excluded. Some image
contexts contain full selection provenance. A documentation-only edit can
therefore change an actual cache key. Do not silently remove files from hashes
or reuse mismatched images. Separating provenance from build identity requires
its own reviewed tooling change and tests for every consumer.

## Improve the flow during patch work

When authorized to improve maintenance flow, a reproduced workflow defect or
measured bottleneck may be repaired alongside patch development. Keep those
edits in fork-owned files, state the observed cost/failure and test the narrow
automation boundary first. Do not move production fixes into tooling or weaken
acceptance assertions to obtain a faster green result.

Before editing shared tooling, collect/remove its active named jobs or use the
exact supported abort procedure where appropriate. Preserve their result and
the next patch task in the cycle ledger, change and verify the tool, then resume
the original task. Do not rebuild unrelated Xpra inputs just to validate a
control-plane edit. Cache or scheduling changes require a real representative
named run in addition to infrastructure tests before claiming the new path works.

At handoff distinguish development checks passed, final acceptance complete,
remaining failed/missing gates, and external prerequisites. Keep case READMEs
about current architecture, invariants, ownership and tests; keep transient
research history and pause/resume notes in the ignored cycle ledger.
