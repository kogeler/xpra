# Publish Develop And Change The Fork Default

## Authority

The operator owns every refresh-result commit and signature, push, remote
branch creation, and default-branch change. The sole earlier exception is the
canonical **Autonomous Upstream Refresh and Full Queue Adaptation** runbook's
one reviewed preservation commit before fetch/rebase when non-ignored
pre-existing work must be retained. Its agent directive is:

```text
Execute autonomous-upstream-refresh PRIMARY_CASE=<slug> against the current fork master.
```

It is not a shell command, and `PRIMARY_CASE` does not narrow the full-queue
scope. Agents and automation may otherwise prepare and audit local state, but
never execute remote mutations.

Do not publish an applied patch worktree. Clean `develop` contains the patch
queue representation and automation only.

## Final local gate

Immediately before handoff:

```bash
git switch develop
make -C fork-maintenance isolated-start-check
make -C fork-maintenance stack-check STACK=develop
make -C fork-maintenance ci-layout-check
make -C fork-maintenance develop-check
(
  set -euo pipefail
  mapfile -t bases < <(git merge-base --all refs/remotes/origin/master HEAD)
  test "${#bases[@]}" -eq 1
  base=${bases[0]}
  git status --short --branch
  git log --oneline --decorate "$base"..develop
  git diff --stat "$base"..develop
  git diff --check "$base"..develop
)
```

This gate uses the unique source merge base already embedded in current
`develop`. It does not fetch, compare live master refs, require master
freshness/equality, or rebase. A newer upstream tip is not a publication
blocker for the already adapted queue; the operator owns the decision whether
and when to begin a separate upstream-refresh cycle. That cycle follows
[`upstream-refresh.md`](upstream-refresh.md).

`develop-check` requires one embedded linear source boundary, rejects merge
commits above it, and rejects committed Xpra source copies outside the patch
queue. Review that the branch contains no results, reports, screenshots, status
files, local paths, credentials, or publication drafts.

`ci-layout-check` must show that every workflow from the embedded source is a
byte-identical disabled rename and that the only executable workflows are the
full-SHA-pinned thin `develop` and `master-sync` callers plus the manual,
branch-agnostic `deb-packages` caller. Resolve this boundary before push; an
inherited newly active upstream workflow is a publication blocker.

## Validation summary

Use the final-acceptance ledger from [`validation.md`](validation.md). Verify
that development results reused here still match their final requirements and
that every missing or invalidated gate was completed. Publication review is
not a reason to repeat an unchanged accepted workload.

The handoff states:

- exact `master` and `develop` commits;
- exact embedded source commit;
- ordered active cases and their current resolution;
- a current-source keep/adapt/retire conclusion for every pre-refresh
  production case, with the primary case documented in greatest detail;
- all three clean quarantine reassessment results on this source when required;
- after every upstream rebase, the complete offline suite, production
  tests-only controls or documented no-test semantic substitutes,
  focused/native gates and both real resulting-stack package builds, all three
  full author-test legs, every production case's declared live gates with its
  atomic case selection, and all seven fixed positive live jobs with the
  complete stack selection actually completed on this base;
- any required gates still outstanding;
- whether local commits are signed as required.

Do not convert historical runs into current claims. All detailed output stays
local under `.artifacts/fork-maintenance/`; the commit or external release text
contains a concise outcome only.

## Commits

No target creates a new content commit automatically. Invoking
[`upstream-refresh.md`](upstream-refresh.md) authorizes the agent to create
exactly one direct preservation commit at the start, before fetch/rebase, iff
exhaustive review finds legitimate non-ignored changes. It must contain all and
only that complete reviewed tracked and untracked set, must contain no secret,
generated artifact, unexplained content, or applied Xpra source, includes
reviewed legitimate user work even when unrelated to the refresh, and needs no
additional confirmation. An already clean checkout gets no empty commit.

After that boundary, the agent creates no intermediate, prerequisite, adapted
case, retirement, quarantine, CI-layout, documentation, or final result commit
during the refresh. `develop-rebase` does replay the pre-existing series and
changes commit identities, but that replay is not a second direct content
commit. Dirty reviewed results are handed to the operator with
`develop-check` explicitly outstanding. Do not change Git signing
configuration; if the configured start commit cannot be created, stop before
fetch/rebase rather than weakening signing policy or making a later commit.

After the operator creates or amends a result commit, recheck parent,
tree/diff, subject, and signature because the commit identity changed.

## Operator-only push

After review, the operator resolves the exact remote command. For the initial
publication its expected shape is:

```bash
git push --set-upstream origin develop
```

For an ordinary later publication that did not rewrite published commits, use
a normal fast-forward push. If and only if an operator-selected upstream
refresh rebased already published fork-only commits, capture and verify the
exact current remote SHA, then use an explicit lease for that one ref:

```bash
expected_develop=$(git ls-remote --heads origin refs/heads/develop | awk '{print $1}')
test -n "$expected_develop"
git push \
  --force-with-lease=refs/heads/develop:"$expected_develop" \
  origin develop:develop
```

A lease mismatch stops publication and requires a fresh audit; do not override
it. Plain `--force`, an unspecified lease, and merge-based upstream transfer
are forbidden. Automation deliberately has no push target, and agents never
run either publication command.

After the operator pushes, a read-only audit may compare:

```bash
git ls-remote --heads origin refs/heads/develop
git rev-parse develop
```

The commits must match exactly.

## Operator-only default branch change

Only after `develop` is published and audited does the operator change the
fork's default branch, using GitHub UI or an equivalent command such as:

```bash
gh repo edit kogeler/xpra --default-branch develop
```

This does not change the role of `master`: it remains the protected,
operator-maintained fork reference and may lag canonical upstream between
explicit refreshes. Recheck the repository setting read-only afterward. Do not
delete master, change upstream's default, or repoint patch bases to develop.

## Upstream pull requests

If an active downstream patch is later proposed upstream, prepare a separate
atomic topic branch from verified `upstream/master`. Do not use the complete
develop branch or its automation diff as the PR. Publishing that topic branch
and creating the PR remain operator-only actions and require a separate,
explicit task.
