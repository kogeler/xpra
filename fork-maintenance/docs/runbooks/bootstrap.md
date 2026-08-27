# Bootstrap Fork Maintenance

## Host prerequisites

Provide Git, GNU Make, Python 3.11 or newer, Ruff, and Podman. Physical profiles
additionally need access to the selected `/dev/dri/renderD*` node and, for the
Zed scenario, a readable application directory. No host service manager is a
runner prerequisite.

The automation never installs into system Python. Create its hash-locked live
analysis environment explicitly:

```bash
make -C fork-maintenance live-venv
```

Run offline source checks before host/GPU diagnostics:

```bash
make -C fork-maintenance check
make -C fork-maintenance doctor
```

`doctor` performs no package installation and no remote mutation.

GitHub-hosted CI uses the explicit `ubuntu-26.04` image, which already provides
Git, GNU Make, Python, and Podman. The workflow does not install host packages;
each of its three matrix jobs calls
`make -C fork-maintenance ci-upstream-tests` with one fixed
`XPRA_CI_TARGET`, and the tracked runner builds the frozen Ubuntu 26.04 test
container when its verified image is absent. No display, render node, or
hardware encoder is assumed in CI.

## Repository and remotes

The parent of `fork-maintenance/` must be the Xpra Git top level. Required
remotes are:

```text
origin    https://github.com/kogeler/xpra.git
upstream  https://github.com/Xpra-org/xpra.git
```

Inspect local state without fetching:

```bash
make -C fork-maintenance repo-status
```

Do not initialize a nested checkout. If a remote is missing or has a different
identity, stop for owner review rather than rewriting it automatically.

## Fork-master gate

Before updating local `master`, rebasing `develop`, beginning any patch work,
or making a publication claim, run:

```bash
make -C fork-maintenance repo-sync
```

The command fetches `upstream/master` and `origin/master`, verifies each cached
ref against live `ls-remote`, and requires exact live equality. It does not
modify either remote branch.

Only when this fresh gate reports that the fork is stale does the operator
run:

```bash
gh repo sync kogeler/xpra --source Xpra-org/xpra --branch master
```

Never use `--force`. Repeat `repo-sync` afterward. If the fork cannot
fast-forward, stop; do not reset or rewrite master.

After equality is proven, update only the local mirror:

```bash
make -C fork-maintenance master-update
```

This target creates or fast-forwards local `master` to the verified commit. It
rejects local master that is ahead or divergent and never pushes it.

## Develop branch

For the first local setup only, create `develop` from the verified canonical
commit while the checkout is clean:

```bash
git switch --no-track -c develop refs/remotes/upstream/master
```

Before host-worktree patch operations or publication, transfer later upstream
commits by rebasing clean `develop` onto the verified local `master`:

```bash
git switch develop
make -C fork-maintenance develop-rebase
```

If Git stops for conflicts, inspect each one against current upstream and the
fork-maintenance intent, edit it, and continue only after staging the exact
resolution:

```bash
git status --short
git add -- <resolved-paths>
git rebase --continue
```

Repeat until the rebase completes. If a correct resolution cannot be proved,
run `git rebase --abort` and stop. Do not begin patch work in a conflicted or
partially rebased checkout.

Merging `master`, `upstream/master`, or any equivalent upstream ref into
`develop` is forbidden. After the rebase, prove the start boundary and resolve
the active patch queue against the new base:

```bash
make -C fork-maintenance patch-start-check
make -C fork-maintenance stack-check STACK=develop
```

When published fork-only commits were replayed, the operator later publishes
the reviewed branch with the exact-SHA `--force-with-lease` procedure in
`publish-develop.md`. Neither this automation nor an agent pushes the rewrite.

When fork-control files are still uncommitted, do not switch or rebase the
dirty checkout. Use `isolated-start-check` and the named workspace flow to
audit and refresh existing cases against verified master first. The clean
rebase above remains required before the resulting control-plane change is
committed or published.

After each completed rebase, reassess the single test-quarantine case on clean
master in all three matrix modes before applying it. Follow
`test-quarantine.md`; a newly green module must leave the quarantine in the
same reviewed cycle.

## Runtime root

Generated state is rooted at:

```text
.artifacts/fork-maintenance/
```

The repository `.gitignore` must ignore `.artifacts/`. Private-state helpers
create owned directories and reject symlinks or unsafe permissions. Do not
copy results into the tracked automation tree.

## Upstream-test image

`test-image` verifies the content-addressed image and never builds it:

```bash
make -C fork-maintenance test-image
```

If missing, build it as a durable job with a never-reused identity:

```bash
IMAGE_RUN=upstream-image-01
make -C fork-maintenance test-image-start IMAGE_RUN="$IMAGE_RUN"
make -C fork-maintenance test-image-wait IMAGE_RUN="$IMAGE_RUN"
make -C fork-maintenance test-image IMAGE_RUN="$IMAGE_RUN"
make -C fork-maintenance test-image-remove IMAGE_RUN="$IMAGE_RUN"
```

The final removal deletes only reviewed transient process/context state. It
keeps the verified image and collected local result. Use `test-image-abort`
only for an unfinished build without collected output, and use
`test-image-cache-remove` only for an explicit cache refresh.

To exercise the image-build lifecycle without replacing the normal content tag,
use a lowercase, hyphenated suffix and a unique run, then remove both through
their exact targets:

```bash
make -C fork-maintenance test-image-start \
  IMAGE_RUN=cycle-image-smoke-01 IMAGE_TAG_SUFFIX=-cycle-smoke
make -C fork-maintenance test-image-wait \
  IMAGE_RUN=cycle-image-smoke-01 IMAGE_TAG_SUFFIX=-cycle-smoke
make -C fork-maintenance test-image-remove \
  IMAGE_RUN=cycle-image-smoke-01 IMAGE_TAG_SUFFIX=-cycle-smoke
make -C fork-maintenance test-image-cache-remove IMAGE_TAG_SUFFIX=-cycle-smoke
```

The suffix changes only the local tag. The same input, workflow, source, and
owner labels are still mandatory, so the isolated image cannot replace normal
acceptance implicitly.
