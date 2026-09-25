# Case README Standard

## Scope and completion boundary

Every production `cases/<id>/README.md` is a maintained technical explanation
of one atomic behavior, not a short patch summary or incident note. New cases
must meet the same analytical depth as established cases. A small diff does
not waive analysis of callers, state ownership, failure paths, queue
interactions or regression limits. Do not wait for the operator to request a
second documentation pass.

Apply this standard when creating a case, changing its behavior or tests,
reassessing it after upstream changes, or migrating regression ownership.
Complete the analysis alongside implementation, before draft promotion or
exported handoff. During refresh it is part of the current case's checkpoint,
not deferred whole-queue paperwork. Material source, test or oracle changes
reopen the corresponding sections. Do not rewrite unrelated cases merely to
make all headings identical.

The permanent quarantine scaffold follows its specialized
[quarantine contract](test-quarantine.md): explain assignments, exact clean
failures, narrowing/removal criteria and retained infrastructure. Do not
invent a production defect while that scaffold is inactive.

## Required structure and depth

Use this ordered structure for new production cases. Existing cases may keep
equivalent headings and additional domain-specific sections. The following
content is mandatory; headings, generic assurances and touched-file lists
alone do not satisfy it. Ground explanations in current paths and symbols.

### Boundary

Describe the user-visible failure, necessary options/platforms/state, smallest
triggering sequence, expected behavior and actual behavior. Explain why the
difference requires a production fix. Distinguish a demonstrated mechanism
from an inference about an incident with incomplete logs. Name unaffected
configurations and the atomic scope; not every similar warning has this cause.

### Embedded-source context

Bind reasoning to the exact source embedded in `develop`. Identify relevant
upstream changes, current callers and the code-supported keep/adapt/retire
decision. Explain what remains broken without this case and what an equivalent
upstream replacement must establish before retirement. Applicability and old
commit messages are not proof. Documentation work never implies fetch, remote
edits or rebase; existing Git authority rules remain unchanged.

### Surrounding code and ownership map

Map entry points, callers, state/resource owners and consumers to actual files
and symbols. Trace beyond the edited function through dispatch, lifecycle,
toolkit/native state or build/package consumers as applicable. Explain who
creates, registers, mutates, hands off and releases relevant objects and IDs.
Tables and short call-flow diagrams should expose responsibilities and order,
not repeat filenames without analysis.

### Mechanism and lifecycle

Add named domain-specific sections at the depth the behavior requires.
Explain the old failure and corrected sequence step by step. Distinguish IDs,
objects, flags, queues, timers or artifacts; state the invariants before and
after each transition. Justify every production hunk and its ownership.
Address threading, locks and reentrancy when relevant; explain serialized
execution rather than inventing concurrency for a synchronous fix.

Trace applicable alternative and failure paths: feature/admission gates,
empty state, invalid/duplicate input, nesting/repetition, compatibility,
partial initialization, exceptions, late callbacks, shutdown and cleanup.
Separate preserved behavior from new guarantees. Do not claim rollback,
atomicity, recovery or idempotence unless the code provides it. Explain why a
dimension is inapplicable instead of writing bare `N/A`; do not pad a narrow
case with irrelevant scenarios.

### Patch-queue and integration ownership

Explain changed production/test paths and manifest dependencies, including why
no dependency is needed when related cases share a file or route. Link relevant
cases and name the invariant each owns. Discuss overlap, order and composed
risks: independent applicability is not independent runtime behavior. Separate
shared harness/fixture ownership from installed production behavior. Do not
absorb another case merely to make this description self-contained.

### Patch ownership and non-goals

State what changes and what intentionally remains unchanged: protocol,
feature policy, platform/backend, resources/security, packaging or application
integration as relevant. Explain tempting but incorrect repairs when supported
by code. Name remaining limits without disguising an unresolved required fix
as a non-goal.

### Regression design and clean control

Name durable executable modules and affected upstream controls from `case.toml`.
For each distinct invariant, describe stimulus, real production route,
observable assertion, and expected clean-source versus patched outcome.
Explain why the clean control reaches the defect rather than failing on a
missing API, import, fixture or dependency. Make independent defects observable
independently so the first failure cannot hide the next.

State exactly what is real and substituted: scheduler, threads, native window,
device, parser, factory, clock, logging sink or peers. Describe fixture setup,
cleanup, synchronization and missing-subject failure. State blind spots and
which native, compiled, compatibility or live check closes them. A mocked
setter is not native state, and a native unit test is not a transported
application. Reused upstream tests need the same durable boundary explanation.

### Durable live or package boundary

Identify the actual fixture, input stimulus, endpoint route and acceptance
oracle. Explain option defaults affecting reachability. Distinguish behavior
really exercised from behavior not proved; successful rendering or paste may
miss lifecycle or warning failures. For package changes explain payload
ownership, import/link and runtime proof. Every live endpoint contains the
complete stack, and patch acceptance requires all nine profiles. This section
cannot create a case-only live exception.

### Invariants not to simplify

List concrete constraints whose apparent simplification would reintroduce the
defect or change ownership. Tie them to the mechanism above, not generic advice
like "avoid races" or "keep tests green". Enable future adaptation/retirement
review without requiring the reader to reconstruct the rationale from a diff.

### Required validation

Link [canonical validation](validation.md) and identify this case's clean,
focused, composed, native/compiled/compatibility, live and applicable package
obligations. Explain boundary-specific sequencing without creating a second
scheduling policy, dropping gates or repeating expensive jobs after every edit.
State actual optional static/type-check scope. Keep the manifest's exact
test/gate inventory consistent with the explanation.

## Documentation and evidence

Write in English with precise technical prose, meaningful headings and tables
or diagrams where they clarify identities or order. There is no word/line
quota: depth is measured by answered technical questions, not copied boilerplate.
Conversely, two generic sections about the fix and tests are not a complete
case README. Do not duplicate the full contract, manifest or patch.

Keep current-source rationale, ownership and durable oracles in the README.
Run names, timestamps, image/result/selection digests, counts, transient blockers
and completed/pending acceptance state belong in the ignored cycle ledger under
`work/<session>/`; lasting lessons go into the distilled
[session record](session-close.md). Label designed/required checks as such; planned tests and
old results are not current acceptance evidence.

Calibrate analytical depth against
[packet dispatch](../../cases/packet-handler-error-boundary/README.md),
[GTK scrolling](../../cases/gtk-client-scroll-deduplication/README.md),
[video pipeline cleanup](../../cases/video-pipeline-cleanup-race/README.md), and
[Wayland keymap synchronization](../../cases/wayland-client-keymap-sync/README.md).
These are examples of explanation quality, not workflow authority or sources
of unrelated requirements to copy into another case.

## Author and reviewer checklist

Before promotion/export handoff or closing a case review, the agent must:

1. Re-read candidate, adjacent code, manifest and README; resolve contradictions
   in behavior, naming, ownership and test scope.
2. Check that every production hunk has a causal explanation, relevant states
   and failure paths have owners, and current-source necessity is established.
3. Trace each claimed regression/live/package guarantee to stimulus and
   assertion, recording fixture substitutions, clean failure and blind spots.
4. Verify dependencies, queue interactions and the invariants an upstream
   replacement must preserve, including relevant unchanged paths.
5. Confirm case-specific analysis in all required sections, no draft
   placeholders, unsupported guarantees, copied results or stale assumptions.
   Record unresolved questions honestly; they do not pass the affected review.

This is mandatory semantic review, not a heading-count or length test.
`case-new` emits a draft outline and link to this standard; the outline,
manifest validation and patch export do not certify documentation completeness.
The agent completes it in the same pass without another operator request.
