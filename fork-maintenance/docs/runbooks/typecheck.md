# Scoped downstream type checking

Mypy is a development tool, not an installed Xpra dependency. It checks only
the explicit downstream-owned Python files in `infra/typecheck/mypy.ini`, in
an exact full-stack workspace. Imported upstream modules supply type context
with `follow_imports=silent`; their own diagnostics are not emitted. We do not
run mypy across the upstream project, maintain a global error baseline, add
blanket ignores, or repair unrelated upstream typing to obtain a green check.
This uses mypy's documented [explicit file scopes and import handling](https://mypy.readthedocs.io/en/stable/running_mypy.html).

The initial strict scope is `xpra/server/source/queued_packet.py`, the real
mixed clipboard/draw queue narrowing function. It accepts sequences rather than
assuming `Packet`, and only draw packets gain typed accessors. This is useful
but deliberately partial coverage: the rest of the existing patches, dynamic
mixin interfaces, GI/Cython bindings and OpenGL code are **not** claimed to be
type-checked. In particular, ndarray truth-value errors, event ordering and
cleanup races still need real regression/native/live tests.

Create the separate tool environment and a finalized complete-stack workspace:

```bash
python3 -m venv .artifacts/fork-maintenance/venvs/typecheck
.artifacts/fork-maintenance/venvs/typecheck/bin/python -m pip install \
  -r fork-maintenance/infra/typecheck/requirements.txt
make -C fork-maintenance workspace-create \
  STACK=develop PATCH_MODE=patched WORKSPACE=<unique-name>
make -C fork-maintenance typecheck WORKSPACE=<unique-name>
```

The wrapper requires mypy 2.3.1, verifies the workspace against the current
forward-applied queue before and after checking, validates every scope path's
active-patch ownership, and reports exact source/selection/workspace identities.
No host Xpra source is modified. A missing file or tool is a failure, not a skip.
`MYPY_PYTHON=/absolute/path/to/python` may select an existing matching environment.

The command also runs a negative control using mypy's shadow-file facility
against the actual module: restoring `packet.get_type()` before narrowing must
fail with `attr-defined`. It does not modify workspace or installed code.
Changing the function requires maintaining this non-vacuous control.
The ordinary offline `check` also exercises scope ownership, stale/wrong
workspaces, missing/unsafe source paths, input mutation and mypy failure
propagation without requiring the optional typecheck environment.

Expand the explicit scope only when the real implementation can be checked
without upstream-wide repairs or untyped stand-ins. Do not report success for
an unexamined file merely because its functions became `Any`. New typed modules
must remain small production boundaries, not test-only replicas. Run this gate
when a scoped file, its imported type contract, or the checking infrastructure
changes. It supplements focused/native checks and the mandatory complete
nine-profile live suite; it never replaces them.
