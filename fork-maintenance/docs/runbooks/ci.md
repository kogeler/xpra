# Run The Fork GitHub Workflows

## Boundary

GitHub Actions is only a hosted launcher for the tracked container automation.
The executable workflows are `.github/workflows/develop.yml`,
`.github/workflows/master-sync.yml`, and
`.github/workflows/deb-packages.yml`. The develop workflow runs only for pushes
to `develop` on `ubuntu-26.04` with a six-hour job timeout. Its fixed matrix
contains `full`, `full-cython`, and `full-no-compat`. Each matrix job checks out
full history without persisting credentials, exports its leg as
`XPRA_CI_TARGET`, and calls the same command:

```bash
make -C fork-maintenance ci-upstream-tests
```

The hosted preflight requires that checkout to remain clean at its exact
`GITHUB_SHA` before it reads any queue or runner input.

The matrix uses `max-parallel: 3` and `fail-fast: false`, so all available jobs
start concurrently and one failed leg does not cancel the other two. Do not add
apt, Podman, Python, patch, retry, skip, dynamic test discovery, or test commands
to the YAML. Apart from the exact three-value fan-out, add or change behavior in
a public Make target and its Python helper, then keep the workflow command
unchanged.

The master-sync workflow is a separate write-scoped launcher. It runs every 12
hours or by operator dispatch and calls only
`make -C fork-maintenance ci-master-sync`. Its exact remote-master contract and
manual launch are documented in [`master-sync.md`](master-sync.md). It does
not run tests or change `develop`.

The package workflow is manual-only with a six-hour job timeout. It checks out
the full history of the branch or tag revision selected by the operator without
hard-coding its name or persisting credentials, and calls only:

```bash
make -C fork-maintenance ci-deb-release
```

That Make/Python path locates the clean source boundary from `HEAD` and refs
whose final component is `master`; it never fetches, syncs, or depends on a
current branch or remote name. It builds Ubuntu 26.04 and Debian 13 DEB tars
through the common mount-free Podman transport from one frozen selection
snapshot and one frozen source snapshot, validates both, stages and verifies
one draft prerelease, then publishes its unique package tag at the dispatched
checkout. Before a retried attempt publishes, it may remove only an exact draft
and unchanged tag left by an earlier failed attempt of that same hosted run,
after validating the Actions record and embedded release transaction. Recovery
deletes and verifies the tag first, then deletes and verifies the immutable
release ID last. Draft creation records that ID from the authenticated REST
create response; absence and exact orphan recovery use bounded pagination of
the release collection, never the published-only tag lookup. Published,
tag-only, duplicate, or ambiguous state is preserved. See
[`deb-packages.md`](deb-packages.md).

The hosted develop test entry point does not call `ci-layout-check`. GitHub has
already selected this workflow, so the rebase/publication audit must never
prevent the actual upstream tests from starting.

If CI stops in `ci-prepare` before `ci_target=` appears, treat it as a pre-test
control-plane failure. After changing only that guard, run its narrow unit test
and reproduce `ci-prepare` with the GitHub environment variables. Do not spend
the three-leg container matrix on that verification when the exact frozen fork
source commit, queue digests, image inputs, entrypoint, and test commands did not
change; the matrix has no coverage of a guard that has already returned.

The CI target uses the checkout's cached `origin/master` only to locate the
merge base already embedded in pushed `develop`. It freezes that exact commit
without querying moving live master refs, validates `XPRA_CI_TARGET`, the queue,
and the source bundle before building or verifying the input-keyed,
label-verified Ubuntu 26.04 container image, applies the complete
`stacks/develop` patch queue, and
runs exactly one of these upstream-authored unit-test modes:

1. `full`;
2. `full-cython`;
3. `full-no-compat`.

The three matrix jobs run on independent hosted runners in parallel. An
unexplained failure stops only its job because matrix fail-fast is disabled.
Container output goes directly to the corresponding Actions log. Each hosted
job owns its timeout and cancellation; disposable test containers use
`podman run --rm`. When the input-keyed image is absent, the foreground CI image
helper streams the tracked build inputs directly to Podman and creates no
`.ci-image.*` host context; it then verifies the resulting immutable image ID
and labels. The foreground test selection uses exact
`.foreground-payload{,.owner.json}` staging under retained
`.foreground-payload.lock`; image creation and immutable-ID handoff use retained
`image-builds/.image-cache.lock`. Any interrupted marker-backed payload is
recovered only by a later foreground invocation and blocks cycle cleanup.

Before the operator rebases and publishes `develop`, the local workflow fetches
both master refs, verifies each against live GitHub state, requires exact
fork/canonical equality, and consumes that commit. The hosted job keeps the
source commit already embedded in that push even if either live ref advances
later. It does not add an `upstream` remote and never fetches, syncs, switches,
merges, or rebases after `actions/checkout`. If publication used the wrong base,
repair it in the operator workflow and push the corrected commit; CI cannot and
must not rewrite it.

## Disabled upstream workflows

GitHub executes YAML only below `.github/workflows/`. To avoid editing upstream
logic and to let Git rename detection carry upstream changes across rebases,
each canonical workflow is relocated without content changes to the same
relative filename below `.github/upstream-workflows/`.

After every rebase:

1. resolve rename/modify conflicts by preserving the new upstream bytes at the
   disabled destination;
2. relocate every newly added upstream `.yml` or `.yaml` workflow;
3. remove a disabled file only when current fork master no longer contains its
   source;
4. leave only `develop.yml`, `master-sync.yml`, and `deb-packages.yml` in the
   executable directory;
5. run:

```bash
make -C fork-maintenance ci-layout-check
make -C fork-maintenance check
```

`ci-layout-check` compares every disabled file byte-for-byte with current fork
`origin/master`, rejects missing or extra files, and verifies all exact thin
fork workflows. Never adapt or clean up the disabled copies; they are relocated
canonical inputs, not fork templates.

## Action pins

Every `uses:` reference in the executable fork workflows is a full 40-character
commit SHA. Put the corresponding release immediately after it as an inline
comment. The current checkout pin is:

```yaml
uses: actions/checkout@3d3c42e5aac5ba805825da76410c181273ba90b1  # v7.0.1
```

Before changing a pin, inspect the latest release in the action's official
GitHub repository and resolve that exact tag from the official Git remote, for
example:

```bash
git ls-remote --tags https://github.com/actions/checkout.git \
  refs/tags/v7.0.1 refs/tags/v7.0.1^{}
```

Record only the reviewed release commit, update the version comment and the CI
layout checker together, and rerun the offline checks. A moving major tag such
as `@v7`, a branch, or an unversioned SHA comment is forbidden.

## Local reproduction

Each hosted matrix leg can be reproduced from `develop` with Git, Python, GNU
Make, Podman, and network access:

```bash
XPRA_CI_TARGET=full make -C fork-maintenance ci-upstream-tests
XPRA_CI_TARGET=full-cython make -C fork-maintenance ci-upstream-tests
XPRA_CI_TARGET=full-no-compat make -C fork-maintenance ci-upstream-tests
```

Each invocation deliberately runs one heavy leg and may build the container
image. For ordinary patch acceptance use the named `test-start`/`test-wait`
lifecycle from `upstream-tests.md`; the CI foreground target is not durable
local evidence.

Do not invoke `ci-deb-release` as local reproduction: a successful call creates
a remote tag and GitHub prerelease, while a failed publication may exercise its
exact tag-first/release-last rollback. Reproduce each distribution with the
named `deb-start` / `deb-wait` / `deb-remove` lifecycle instead.

## No live tests

Never call `live-start`, `live-rgb`, `live-h264`, or another `live-*` target from
CI. GitHub-hosted runners do not provide the controlled display session,
render node, physical GPU, or hardware encoder required by those profiles.
Complete all case-declared live gates locally and report them separately from
the hosted unit-test result.
