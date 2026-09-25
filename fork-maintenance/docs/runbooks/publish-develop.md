# Publish Develop And Change The Fork Default

## Authority

The operator performs Git operations outside the case commits, or explicitly
delegates them to the agent, who owns their correct execution; pushing is never
delegated. Case commits are the storage of case work: the agent creates, fixes
up, squashes, drops and rebases them in the local `develop` itself
([commit authority](case-commits.md#commit-authority)). That includes the local
`develop` rebase onto existing local `master` through the
**Autonomous Upstream Refresh and Full Queue Adaptation** runbook:

```text
Execute autonomous-upstream-refresh against the current fork master.
```

It is not a shell command. Every case requires equally deep manual review;
the older optional `PRIMARY_CASE=<slug>` spelling affects only starting order,
never depth or scope. Fetch, branch preparation, remote configuration and
preservation/result control commits need separate explicit requests. The
operator pushes; the agent never pushes.

`develop` is the patched product itself: the upstream base plus control
commits and one commit per case ([model](case-commits.md#model)). Only its
committed history is published; results, reports and uncommitted work are
never part of it.

## Final local gate

Immediately before handoff:

```bash
test "$(git branch --show-current)" = develop
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
blocker for the already adapted stack; the operator owns the decision whether
and when to begin a separate upstream-refresh cycle. That cycle follows
[`upstream-refresh.md`](upstream-refresh.md).

`develop-check` requires a clean tree and one embedded linear source boundary,
rejects merge commits and pending `fixup!` commits above it, requires every
fork commit to be a valid control or case commit, and requires case
directories and case commits to correspond one to one (see
[checks](case-commits.md#checks)). Review that the branch contains no results,
reports, screenshots, status files, local paths, credentials, or publication
drafts.

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
- ordered active cases (`case-list`) and their `case-check` result;
- whether case commits were rewritten since the last publication, so that the
  operator knows a `--force-with-lease` push is needed;
- the whole-queue manual-review exit record before the first runtime test;
- an equally detailed current-code correctness/necessity analysis and
  keep/adapt/retire conclusion for every pre-refresh production case,
  including uncovered risks, implemented changes and migrated test ownership;
- how subsequent runtime results confirmed or reopened those conclusions;
- all three clean quarantine reassessment results on this source when required;
- after every upstream rebase, the complete offline suite, production
  tests-only controls or documented no-test semantic substitutes,
  focused/native gates and both real resulting-stack package builds, all three
  full author-test legs and all nine fixed positive live jobs with the
  complete stack selection actually completed on this base;
- any required gates still outstanding;
- for any case validation, the complete current nine-profile live suite with
  all case commits on both endpoints, never case-only or clean-endpoint live
  evidence;
- that agent-created commits are unsigned by operator policy;
- the path of the distilled session record, and that the session is closed.

Do not convert historical runs into current claims. Detailed output stays local
under `.artifacts/fork-maintenance/` only until the
[session close](session-close.md), which precedes any control commit;
afterwards only the distilled session record remains. The commit or external
release text contains a concise outcome only.

## Commits

Case commits are created and rewritten by the agent as part of the task,
always unsigned (`git -c commit.gpgsign=false ...`), following
[`case-commits.md`](case-commits.md). No target or runbook creates a control
commit automatically: a control commit requires an explicit operator request,
including any proposed preservation commit before a rebase. Every rewrite
(`develop-squash`, `case-drop`, `develop-rebase`) requires clean product paths
and carries uncommitted control work through `--autostash`. Reviewed dirty
control work is handed off with `develop-check` outstanding. Preserve signing
configuration unless changing it is explicitly requested.

After the operator creates or amends a control commit, recheck parent,
tree/diff, subject, and signature because the commit identity changed. After
any case rewrite, rerun `stack-check` and review the changed case commits with
`case-show`.

## Explicit publication

After review, the operator resolves the exact remote command. For the initial
publication its expected shape is:

```bash
git push --set-upstream origin develop
```

For an ordinary later publication that did not rewrite published commits, use
a normal fast-forward push. When any case rewrite (fixup and squash, drop,
split or merge, or an upstream-refresh rebase) replaced already published
commits, capture and verify the exact current remote SHA, then use an explicit
lease for that one ref:

```bash
expected_develop=$(git ls-remote --heads origin refs/heads/develop | awk '{print $1}')
test -n "$expected_develop"
git push \
  --force-with-lease=refs/heads/develop:"$expected_develop" \
  origin develop:develop
```

A lease mismatch stops publication and requires a fresh audit; do not override
it. Plain `--force`, an unspecified lease, and merge-based upstream transfer
are forbidden. Automation has no push target. The operator runs these
commands; the agent never pushes.

After the operator pushes, a read-only audit may compare:

```bash
git ls-remote --heads origin refs/heads/develop
git rev-parse develop
```

The commits must match exactly.

## Explicit default branch change

Only after `develop` is published and audited does the operator change the
fork's default branch, using GitHub UI or an equivalent command such as:

```bash
gh repo edit kogeler/xpra --default-branch develop
```

This does not change the role of `master`: it remains the protected,
operator-maintained fork reference and may lag canonical upstream between
explicit refreshes. Recheck the repository setting read-only afterward. Do not
delete master, change upstream's default, or use `develop` as the upstream
base of the case commits.

## Upstream pull requests

If an active case is later proposed upstream, its case commit (`case-show`) is
the atomic source of that change. Do not use the complete develop branch or
its automation diff as the PR. The maintenance workflow creates no topic
branch or worktree for it: preparing, publishing and opening the PR is a
separate explicit operator task.
