# Register the seccomp build feature with the standard switch parser

Code: the `Fork-Case: seccomp-build-switch` commit on `develop`
(`make -C fork-maintenance case-show CASE=seccomp-build-switch`).

## Boundary

On the embedded source, `setup.py --minimal --with-seccomp` fails with
`option --with-seccomp not recognized`. The build already defines
`seccomp_ENABLED`, includes the Python package conditionally, and builds its
native extension conditionally, but never registers the command-line switch.
Consequently a minimal build cannot opt into the native filter, and an ordinary
build cannot explicitly opt out using the normal feature interface. Installing
the distribution's Python seccomp bindings does not supply Xpra's `_native`.

This case repairs that build interface. It does not change the automatic Linux
default or assert that every enabled filter is compatible with every decoder.

## Embedded-source context

The maintainer's original [seccomp integration][seccomp-introduction] adds the
feature variable, package toggle and native extension, but omits `SWITCHES`.
Current source retains this omission. His later [Landlock integration][landlock]
demonstrates the existing mechanism for exposing a comparable native feature:
register the feature name and let `filter_argv()` handle it. No new parser,
environment override or dependency-detection path is needed here.

The upstream [VA decoder review][va-review] also requires build variants to
remain independent of unrelated platform integrations. This correction adds
no X11, Wayland or codec prerequisite. The maintainer's [seccomp discussion][seccomp]
distinguishes enabling the filter from resolving hardware decoder constraints;
registering a build switch must not silently relax the runtime policy.

Keep this case until upstream registers the feature through the standard
parser, accepting both explicit states without changing automatic defaults.

## Surrounding code and ownership map

`setup.py` computes `DEFAULT` from `--minimal`, then computes
`seccomp_ENABLED` from that default, Linux support and `pkg_config_exists`.
The `SWITCHES` inventory is the existing owner of explicit feature overrides.
`filter_argv()` consumes registered arguments, updates the corresponding
`<name>_ENABLED` variable and passes only remaining arguments to setuptools.
The setup help output uses the same inventory and reports the resolved value.

Later `toggle_packages(seccomp_ENABLED, "xpra.seccomp")` controls the Python
package, and `tace(seccomp_ENABLED, "xpra.seccomp._native", "libseccomp")`
uses the normal native extension and pkg-config path. Runtime filter selection
belongs to `xpra.scripts.main.configure_seccomp` and `xpra.seccomp`, not setup.
Minimal-build consumers use this interface; release package defaults and runtime
policy remain separate boundaries, not an alternative owner of the parser.

## Mechanism and lifecycle

The sole production change adds `seccomp` to the existing `SWITCHES` list next
to the other native platform features. The existing parser now consumes
`--with-seccomp` / `--without-seccomp` and its standard `--seccomp` /
`--no-seccomp` aliases before setuptools sees them. With no explicit override,
the existing default calculation is untouched. In a minimal build an explicit
enable changes only this feature, rather than enabling unrelated default
features to obtain the native filter incidentally.

An explicit enable follows the same dependency-resolution behavior as other
native feature overrides: missing libseccomp build dependencies must still
fail in the normal build path. This patch neither forges pkg-config results
nor introduces a bundled library. Automatic non-Linux defaults remain false;
requesting an unsupported native build is not converted into silent success.
This is synchronous setup argument processing, with no runtime object,
thread, cleanup hook or new resource lifetime.

## Case-commit and integration ownership

The product commit owns one switch-list entry in `setup.py` and
`unit.seccomp_build_test`. It applies and reverts independently of
`libva-decoder-display-lifetime`: that case repairs decoder display ownership,
not the build parser. Neither requires the other's source change to be
meaningful, so no manifest dependency is declared. Standard package builds can
already enable seccomp automatically; their success does not exercise this
explicit override or make the missing interface correct.

The live Containerfile's exact-DEB installation and its
native-import/runtime-evidence checks are shared fork controls. They do not
belong in this installed-product commit. No existing case owns this setup
feature registration, and no existing case's parser or lifecycle changes.

## Commit scope and non-goals

No native filter implementation, syscall list, runtime default, command-line
runtime option, codec, package ownership or platform detection is changed.
Enabling all features is not a regression for an explicit minimal-build
override. Live acceptance now intentionally installs the real complete DEBs
instead of making a second minimal Xpra build. Disabling seccomp, copying a native
module from another installation or modifying setup's defaults only for the
test harness would also leave the production defect unresolved.

## Regression design and clean control

`unit.seccomp_build_test` runs the actual setup program in a private temporary
source copy, rather than matching the text of `SWITCHES` or mocking the parser.
Each subprocess uses `--minimal`, one explicit option and `--help`; it must
exit successfully and report the requested resolved boolean in the standard
help output. Independent tests cover explicit enable, explicit disable and
the two ordinary boolean aliases. Setup may generate metadata in the private
copy, which is removed by class cleanup; the tested checkout is not rewritten.

On the tests-only embedded source each requested option reaches setuptools
unconsumed and fails as unknown. The same commands with the one-line product
change reach normal setup help with the expected value. No missing new API
or GPU is involved in that negative control. `unit.seccomp_test` remains an
adjacent control of the native filter's existing policy.

The parser regression does not compile `_native`, prove its installed payload,
or install a thread filter. Those are deliberately separate native and live
boundaries below; successful argument parsing alone is not sandbox evidence.

## Durable live or package boundary

Both complete-stack live endpoints install exact accepted distribution DEBs,
not a second source build. Package attestation requires Xpra's actual `_native`
from the common package and binds its loaded bytes to that DEB. The client
then runs the default mode; its complete log must prove an installed `errno`
decode filter before hardware frames count. A rendering success with an
unavailable native module must fail acceptance. This is required across the
live suite, not a case-only endpoint or hardware bypass.

Release package builds retain their normal dependency and installation path.
Where package acceptance exercises this feature, native module inclusion and
runtime import are necessary; recognizing a source option cannot replace
that payload proof. The case introduces no new package split or runtime
dependency name.

## Invariants not to simplify

- Register the feature once in the standard inventory; do not add a special
  second parser or bypass setuptools for this option.
- Preserve the existing no-override and minimal-build defaults.
- Keep Python package inclusion and native compilation on the same resolved
  feature variable, with normal pkg-config dependency resolution.
- Distinguish a parsed option, an installed native module and a successfully
  installed runtime filter; none alone proves the next boundary.

## Required validation

Use [canonical validation](../../docs/runbooks/validation.md): a tests-only
behavioral negative followed by patched focused parser and native-policy
tests, composed focused coverage, the three full upstream modes and full
fork-control checks after candidate freeze. All nine full-stack live profiles
and the final uninterrupted pass remain mandatory. The real package endpoint
installation and runtime filter oracles close the parser test's compilation and
installation blind spots. Static checking follows the embedded Ruff setup;
this setup-list change does not expand the separate scoped mypy contract.

[seccomp-introduction]: https://github.com/Xpra-org/xpra/commit/65dcb6e065af35d9f92c3e52aa1be313ea916e1b
[landlock]: https://github.com/Xpra-org/xpra/commit/473b59975cc12e6be1841099e9500b669cc02277
[va-review]: https://github.com/Xpra-org/xpra/pull/4964#discussion_r3612055724
[seccomp]: https://github.com/Xpra-org/xpra/issues/622#issuecomment-5972499066
