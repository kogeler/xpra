# Keep Every Case As One Commit On `develop`

## Model

`develop` is the upstream base (`master`) plus the fork's own commits. There is
no other branch and no Git worktree: every operation below runs in the single
`develop` checkout. The fork's commits belong to exactly one of two classes,
which never touch the same files:

| Class | Paths | Message |
| --- | --- | --- |
| **control commit** | only the control paths: `AGENTS.md`, `.gitignore`, `.github/workflows/`, `.github/upstream-workflows/`, `fork-maintenance/` | no `Fork-Case` trailer |
| **case commit** | only product paths (everything else: `xpra/`, `tests/`, `packaging/`, `setup.py`, ...) | exactly one `Fork-Case: <slug>` trailer |

A commit which mixes both classes, a product change without a trailer, a merge
commit and an unsquashed `fixup!`/`squash!`/`amend!` commit are invalid.

Every case is **exactly one case commit**. Its code change and its focused
regression tests live in that commit; its documentation lives in
`fork-maintenance/cases/<slug>/` (`case.toml`, `README.md`, optional local
`tests/`), which is changed by control commits. A case directory exists if and
only if its commit exists, with one exception: the permanent
`upstream-test-quarantine` directory also exists while no quarantine commit
does (see [`test-quarantine.md`](test-quarantine.md)).

The two classes may appear in any order; nothing reorders them. The relative
order of the case commits is the stack order, and a case which declares
`dependencies` must come after each of them. No patch file is stored anywhere
in the tracked tree: test, package and live runners take each selected case
commit's diff at run time (see [Runners](#runners)).

## Case identity

A commit SHA changes whenever `develop` is rebased or a case is reworked, so the
stable identity of a case is its slug in the `Fork-Case` trailer. Never store a
case commit SHA in a tracked file. Resolve it when needed:

```bash
make -C fork-maintenance case-list                  # every case: slug, commit, subject
make -C fork-maintenance case-show CASE=<slug>      # the commit with its stat
make -s -C fork-maintenance case-commit CASE=<slug> # only the SHA, for scripts
```

Case READMEs, session records and ledgers refer to the case by slug ("the
`Fork-Case: <slug>` commit"). While the quarantine is inactive it has no commit:
`case-list` shows it without one and `case-show` reports that none exists. A ledger may record SHAs of a given moment, for
example the case map before a rebase, because it is a point-in-time record.

## Commit message

```text
<imperative subject, at most 72 characters>

<why the change is needed and what it changes; at most a short paragraph>
See fork-maintenance/cases/<slug>/README.md.

Fork-Case: <slug>
```

Agent commits are never signed and never name an agent (see
[Agent commits](#agent-commits)). Other trailers may follow `Fork-Case`, but
never an agent co-author. Every new
downstream-authored source or test file carries
`Copyright (C) <current-year> kogeler` in its native comment syntax; keep
required notices on copied or derived content and add the `kogeler` line; never
attribute a downstream-created file to an upstream maintainer.

## Case manifest

`cases/<slug>/case.toml` uses schema 2. Everything that can be derived from the
commit (subject, patch digest, touched paths) is not stored:

```toml
schema = 2
slug = "<slug>"
kind = "production"            # or "test-quarantine" for the one duty case
title = "<one-line description>"
dependencies = []              # slugs whose commits must precede this one

[tests]
list = ["unit.<module>_test", "focused", "full", "full-cython", "full-no-compat"]

[evidence]
required_gates = ["live-rgb", "live-h264", ...]   # the nine live gates
```

`stacks/develop.toml` (schema 2) keeps only the stack-level `[tests]` list; the
stack's case series is the order of the case commits in `develop`.

The quarantine case (`kind = "test-quarantine"`) adds a `[quarantine]` table
with `modules` and the three `quarantine*` gate lists; while it has no commit,
all its lists stay empty (see [`test-quarantine.md`](test-quarantine.md)).

## Everyday operations

Work in the `develop` checkout. Uncommitted control-path changes may stay in the
tree: the rewriting commands below stash and restore them (`--autostash`).
Product paths must be clean before a rewrite or a runner start: commit the
change (as a fixup) first. Runners always test committed `HEAD`.

### Change a case

```bash
# edit the product files of the case
git add -- <paths>
git -c commit.gpgsign=false commit --fixup="$(make -s -C fork-maintenance case-commit CASE=<slug>)"
make -C fork-maintenance develop-squash
make -C fork-maintenance case-check CASE=<slug>
```

`git commit --fixup=amend:<sha>` also replaces the commit message (for example
a new subject). `develop-squash` folds every pending `fixup!`/`amend!`/`squash!`
commit into its target with a non-interactive `git rebase --autosquash` from the
upstream base; the commits above the target are replayed, so their SHAs change.
A conflict while squashing means the edit overlaps another case: the cases are
not independent. Abort (`git rebase --abort`), then resolve it in the design,
not by a merge. `--fixup=amend:` opens an editor for the new message; supply it
non-interactively through `GIT_EDITOR` (a command which writes the message
file).

### Add a case

```bash
make -C fork-maintenance case-new CASE=<slug>
# complete case.toml and README.md (control commit or uncommitted control work)
# implement the product change and its regression tests
git add -- <paths>
git -c commit.gpgsign=false commit -m "<subject>" -m "<body>" \
  --trailer "Fork-Case: <slug>"
make -C fork-maintenance case-check CASE=<slug>
```

`case-new` only creates the directory with a schema-2 manifest template and the
README skeleton required by [`case-documentation.md`](case-documentation.md).

### Retire a case

```bash
make -C fork-maintenance case-drop CASE=<slug>
git rm -r -- fork-maintenance/cases/<slug>
```

`case-drop` removes the case commit from history with
`git rebase --onto <commit>^ <commit>` (the drop of the agreed model; it equals
a revert squashed into the original commit) and refuses while another case
declares it as a dependency. Remove the case directory and every reference to
the slug in the same change; once that control change is committed,
`develop-check` passes again. The quarantine is the exception: deactivating it
drops its commit but keeps its directory with emptied lists
([`test-quarantine.md`](test-quarantine.md)).

### Split, merge or reword cases

Use an explicit non-interactive `git rebase -i` sequence from the upstream base
(`GIT_SEQUENCE_EDITOR` writing the prepared todo list): `edit` to split a commit
(`git reset HEAD^`, then one commit per resulting case), `fixup` to merge two
cases into one, `reword` or `--fixup=amend:` to change a message. Update the
case directories in the same change.

## Checks

| Target | Proves |
| --- | --- |
| `case-check CASE=<slug>` | Manifest schema 2 is valid; exactly one commit carries the trailer; it touches only product paths (the quarantine commit exactly the files of its `quarantine.modules`); cherry-picked onto the upstream base on its own it merges without a conflict and is neither already present nor ambiguous; reverting it from `HEAD` is conflict-free. It reports the cases that depend on it. |
| `stack-check STACK=develop` | Every case commit resolves in order on the upstream base, and base plus all case diffs reproduces exactly the product tree of `HEAD`. |
| `develop-check` | Clean tree (so uncommitted control work, for example a new or removed case directory, must be committed first; until then the handoff reports `develop-check` as outstanding); every fork commit is a valid control or case commit; no merges and no pending fixups; no stored `fix.patch`; case directories and case commits correspond one to one (the inactive `upstream-test-quarantine` directory excepted); dependencies precede their consumers; every `case-check`, `stack-check` and `ci-layout-check`. |

The independence and removability proofs are in-memory merges
(`git merge-tree --write-tree --merge-base=...`); they never check out another
tree or create a branch.

## Runners

Upstream tests, DEB builds and live profiles keep their selectors and modes:
`CASE=<slug>` or `STACK=develop`, with `PATCH_MODE=clean`, `tests-only` or
`patched` (see [`upstream-tests.md`](upstream-tests.md)). At start they freeze
the selection into their private payload: the manifests plus one diff per
selected case, written as that case's `fix.patch` inside the payload only
(together with `selection-source.json`, which records base, `HEAD`, series and
commits):

- `STACK=develop`: each case commit's own diff against its parent, in commit
  order, so applying them in order to the base reproduces `HEAD` exactly;
- `CASE=<slug>`: the case commit cherry-picked onto the bare upstream base in
  memory (`git merge-tree`), diffed against the base. A commit's own diff
  carries context lines from the cases before it, which need not exist on the
  bare base; the three-way merge applies only the case's change, and a
  conflict means the case is not independent.

The selection digest binds those bytes, so a changed case diff is a new
candidate; a SHA which changed only because an unrelated commit was rewritten
keeps the same bytes.
`tests-only` applies only the `tests/` part of the case diff to the clean
upstream base.

## Upstream refresh

[`upstream-refresh.md`](upstream-refresh.md) is canonical. In short:

1. Record the case map (`case-list`) and the old `develop` tip in the ledger.
2. `make -C fork-maintenance develop-rebase` rebases the clean `develop` onto
   local `master` in the checkout (it refuses any uncommitted change). At a
   conflict, resolve it inside the commit being replayed (a case commit, or a
   control commit such as the relocated upstream workflows) and continue with
   `git -c commit.gpgsign=false rebase --continue`. Mid-rebase the control files
   of the tree may be old or absent; read runbooks with
   `git show ORIG_HEAD:<path>` and run tooling only after the rebase ends.
3. A case whose diff is already upstream byte for byte disappears by itself
   (Git skips commits with an identical patch); remove its directory as for any
   retirement. Retire the other fully upstream cases with `case-drop`. Rework
   the rest with fixups and `develop-squash`. A conflict resolution which would
   leave a case commit empty keeps it as an empty placeholder
   (`git commit --allow-empty -C REBASE_HEAD`); the tooling accepts it, but its
   `case-check` and `develop-check` fail until it is rebuilt with a fixup or
   retired with `case-drop`.
4. Review each case with `git range-diff <old-sha>^! <new-sha>^!` against the
   recorded map, then `develop-check` and the validation of
   [`validation.md`](validation.md).

Rollback is always possible: `git reset --keep <recorded tip>` or the reflog.

## Publication

Any rewrite of a case (fixup, squash, drop, rebase) changes `develop` history,
so the operator publishes `develop` with `--force-with-lease` whenever needed;
see [`publish-develop.md`](publish-develop.md). Nothing in this workflow pushes.

## Agent commits

The operator's global Git configuration signs every commit with a hardware
security token (`commit.gpgsign = true` with an ED25519-SK key). An agent cannot
operate the token: a signing attempt blocks or opens a PIN prompt. Therefore
every Git command of an agent which creates or rewrites a commit disables
signing for that one command:

```bash
git -c commit.gpgsign=false commit ...
git -c commit.gpgsign=false rebase ...            # also --continue, --autosquash
git -c commit.gpgsign=false cherry-pick ...
```

The Make targets which rewrite history (`develop-rebase`, `develop-squash`,
`case-drop`) already pass it. Never turn signing off in the global, user or
repository configuration, and never ask the operator for the token.

Commits and pull-request texts created by an agent never name an agent as author
or co-author: no `Co-Authored-By:` line for an AI agent and no "generated with"
note, whatever a tool suggests by default. The author is the operator's
configured Git identity. Check the messages of new commits before handing off.

## Commit authority

Case commits are the storage of case work: the agent creates, fixes up, squashes,
drops and rebases them itself, always unsigned, as part of the task. Control
commits follow the operator's instructions; uncommitted control work survives
every rewrite through `--autostash`. The agent never pushes.
