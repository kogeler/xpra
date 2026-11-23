# Accept typedict metadata in the compiled window factory

Code: the `Fork-Case: cython-window-factory-metadata` commit on `develop`
(`make -C fork-maintenance case-show CASE=cython-window-factory-metadata`).

## Boundary

`xpra opengl` (`xpra/scripts/glprobe.py` → `xpra.opengl.window.test_gl_client_window()`)
draws into a real test window created without a client, through
`FakeWindowHost.make_window()`. `test_gl_client_window()` passes the window
metadata as a `typedict`. `make_window()` annotated that parameter as `dict`.
In a build whose pure-Python modules are compiled by Cython (our `full-cython`
leg; the same applies to any `cythonize_more` package), Cython enforces an
exact builtin type for a `dict` annotation and rejects the `dict` subclass:
`TypeError: Argument 'metadata' has incorrect type (expected dict, got
typedict)`. The OpenGL check then reports `success=False` and
`message=Argument 'metadata' has incorrect type ...` instead of completing its
test draw. Our fork's neutral `public_capabilities_test`
(`test_public_opengl_check_completes_a_real_test_window_draw`) caught it in
the compiled upstream-test leg only; pure-Python builds ignore annotations at
runtime and are unaffected.

## Embedded-source context

Source: `d2940c7ddc6edd65aadb00f55ac559045d0c94ed`. `make_window()` and
`FakeWindowHost` come from `07a69c043a` and `9ee98228dd` (2026-11-03). The
maintainer fixed the same defect for the `window_class` argument in
`8c4b099ea4` ("Fix Cython window factory annotation for GTK metaclasses"),
replacing `type` with `Callable[..., Any]` and documenting that "Cython
enforces an exact builtin type". This case applies that fix to the second
argument of the same signature. Reviewed: local upstream history of
`fake_client.py` and `opengl/window.py`. An upstream annotation that accepts
`typedict` retires the case.

## Surrounding code and ownership map

| Owner / symbol | Role |
| --- | --- |
| `xpra.opengl.window.test_gl_client_window()` | Builds `metadata = typedict({...})` and calls `noclient.make_window(window_class, 0, geometry, metadata, ...)`. |
| `FakeWindowHost.make_window()` | Wraps the metadata in `typedict(metadata)` for the window constructor and registers the window with the fake window subsystem. |
| Other callers | Examples and tests pass `dict` or `typedict`; both are mappings. |

## Mechanism and lifecycle

The only change is the parameter annotation (`Mapping[str, Any]`, imported
from `collections.abc`) and the docstring line explaining it. Cython treats
an ABC or `typing` annotation as a generic object and performs no exact type
check, as for `Callable[..., Any]`. The body already copies the mapping with
`typedict(metadata)`, so every accepted input is normalized before the
window sees it. There is no runtime behaviour change in pure-Python builds.

## Case-commit and integration ownership

The commit changes `xpra/client/gui/fake_client.py` and adds
`tests/unittests/unit/client/fake_window_metadata_test.py`. No dependency.
`video-decoder-capability-loss` also edits `fake_client.py`: it adds a
`FakeWindowSubsystem.video_decoders_changed()` stub, in a separate hunk with
a separate concern.

## Commit scope and non-goals

Unchanged: the window class annotation, the metadata copy, every caller, and
other `dict` annotations in client code. Those are reached with real `dict`
values, or with `typedict` only in pure-Python paths that our compiled leg
does not show failing; changing them without a demonstrated failure would be
speculative.

## Regression design and clean control

`unit.client.fake_window_metadata_test` calls the real
`FakeWindowHost.make_window()` with a `typedict` and a recording window
class. It checks that the window is constructed once with a `typedict` copy
equal to the input and is registered under its ID. In pure-Python mode it
passes with and without the fix, because annotations are not enforced. The
clean control is therefore the compiled build: `TARGET=focused-cython` with
`PATCH_MODE=tests-only` must raise the exact `TypeError`, and patched must
pass. `full-cython` reruns it with the neutral OpenGL check. Blind spot: a
real GL draw is proved only by the neutral `public_capabilities_test` in the
compiled leg and by the live OpenGL profile.

## Durable live or package boundary

The live DEBs are built without `cythonize_more`, so no live profile reaches
the compiled annotation; `live-wayland-opengl-h264-hardware` only shows that
the uncompiled OpenGL path is unchanged.

## Invariants not to simplify

- Do not annotate the parameter with `dict` or `typedict` again, which Cython
  checks exactly; keep a mapping ABC or an untyped parameter.
- Keep the `typedict(metadata)` copy, which is what makes a plain mapping
  acceptable to the window constructors.

## Required validation

Follow [canonical validation](../../docs/runbooks/validation.md):
`case-check`, the case's `focused` target patched, `focused-cython` patched
and tests-only (the compiled clean control must fail), the stack `full`,
`full-cython` and `full-no-compat` legs, new DEBs and the live acceptance
pass for the composed stack.
