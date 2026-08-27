# Run the develop CI

## Boundary

GitHub Actions is only a hosted launcher for the tracked container automation.
The sole executable workflow is
`.github/workflows/develop.yml`; it runs only for pushes to `develop`
on `ubuntu-26.04`. Its fixed matrix contains `full`, `full-cython`, and
`full-no-compat`. Each matrix job checks out the pushed commit, exports its leg
as `XPRA_CI_TARGET`, and calls the same command:

```bash
make -C fork-maintenance ci-upstream-tests
```

The matrix uses `max-parallel: 3` and `fail-fast: false`, so all available jobs
start concurrently and one failed leg does not cancel the other two. Do not add
apt, Podman, Python, patch, retry, skip, dynamic test discovery, or test commands
to the YAML. Apart from the exact three-value fan-out, add or change behavior in
a public Make target and its Python helper, then keep the workflow command
unchanged.

The hosted entry point does not call `ci-layout-check`. GitHub has already
selected this workflow, so the rebase/publication audit must never prevent the
actual upstream tests from starting.

If CI stops in `ci-prepare` before `ci_target=` appears, treat it as a pre-test
control-plane failure. After changing only that guard, run its narrow unit test
and reproduce `ci-prepare` with the GitHub environment variables. Do not spend
the three-leg container matrix on that verification when master, queue digests,
image inputs, entrypoint, and test commands did not change; the matrix has no
coverage of a guard that has already returned.

The CI target uses the checkout's cached `origin/master` only to locate the
merge base already embedded in pushed `develop`. It freezes that exact commit
without querying moving live master refs, validates `XPRA_CI_TARGET`, the queue,
and the source bundle before building or verifying the content-addressed Ubuntu
26.04 container image, applies the complete `stacks/develop` patch queue, and
runs exactly one of these upstream-authored unit-test modes:

1. `full`;
2. `full-cython`;
3. `full-no-compat`.

The three matrix jobs run on independent hosted runners in parallel. An
unexplained failure stops only its job because matrix fail-fast is disabled.
Container output goes directly to the corresponding Actions log. Each hosted
job owns its timeout and cancellation; disposable test containers use
`podman run --rm` and the image helper removes its private build context after
verifying image ownership.

Live `origin/master`/`upstream/master` equality is still mandatory before the
operator rebases and publishes `develop`. It is deliberately not rechecked in
the hosted job: a new fork or upstream commit after publication must not
invalidate the source commit already embedded in that push. The automation
does not add an `upstream` remote and never fetches, syncs, switches, merges, or
rebases after `actions/checkout`. If publication used the wrong base, repair it
in the operator workflow and push the corrected commit; CI cannot and must not
rewrite it.

## Disabled upstream workflows

GitHub executes YAML only below `.github/workflows/`. To avoid editing upstream
logic and to let Git rename detection carry upstream changes across rebases,
each canonical workflow is relocated without content changes to the same
relative filename below `.github/upstream-workflows/`.

After every rebase:

1. resolve rename/modify conflicts by preserving the new upstream bytes at the
   disabled destination;
2. relocate every newly added upstream `.yml` or `.yaml` workflow;
3. remove a disabled file only when current upstream removed its source;
4. leave only `develop.yml` in the executable directory;
5. run:

```bash
make -C fork-maintenance ci-layout-check
make -C fork-maintenance check
```

`ci-layout-check` compares every disabled file byte-for-byte with current
`upstream/master`, rejects missing or extra files, and verifies the exact thin
fork workflow. Never adapt or clean up the disabled copies; they are relocated
canonical inputs, not fork templates.

## Action pins

Every `uses:` reference in the executable fork workflow is a full 40-character
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

## No live tests

Never call `live-start`, `live-rgb`, `live-h264`, or another `live-*` target from
CI. GitHub-hosted runners do not provide the controlled display session,
render node, physical GPU, or hardware encoder required by those profiles.
Complete all case-declared live gates locally and report them separately from
the hosted unit-test result.
