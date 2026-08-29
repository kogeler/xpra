# Publish Develop And Change The Fork Default

## Authority

The operator owns every commit signature, push, remote branch creation, and
default-branch change. Agents and automation may prepare and audit local state,
but never execute those remote mutations.

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
base=$(git merge-base refs/remotes/origin/master HEAD)
git status --short --branch
git log --oneline --decorate "$base"..develop
git diff --stat "$base"..develop
git diff --check "$base"..develop
```

This gate uses the unique source merge base already embedded in current
`develop`. It does not fetch, compare live master refs, require master
freshness/equality, or rebase. A newer upstream tip is not a publication
blocker for the already adapted queue; the operator owns the decision whether
and when to begin a separate upstream-refresh cycle.

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

The handoff states:

- exact `master` and `develop` commits;
- exact embedded source commit;
- ordered active cases and their current resolution;
- all three clean quarantine reassessment results on this source when required;
- focused, native, full, and live jobs actually completed on this base;
- any required gates still outstanding;
- whether local commits are signed as required.

Do not convert historical runs into current claims. All detailed output stays
local under `.artifacts/fork-maintenance/`; the commit or external release text
contains a concise outcome only.

## Commits

No target creates a new content commit automatically. `develop-rebase` does
replay existing commits and changes their local identities. When the user
explicitly authorizes an agent commit in the current conversation, create only
the requested local commit(s) after the gates pass. Do not change Git signing
configuration. If the required hardware key is unavailable, leave an
explicitly identified unsigned handoff for the operator to rewrite and
re-audit.

After any signing amendment, recheck parent, tree/diff, subject, and signature
because the commit identity changed.

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
