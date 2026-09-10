# Native codec package dependency ownership

## Failure boundary

Ordinary `xpra-codecs` must both contain its native codecs and declare the
shared libraries needed to load those exact modules. Payload membership alone
is insufficient: importing successfully in a builder with development packages
installed can conceal a missing dependency on a user's machine.

At the current embedded source, libva is packaged but excluded from the
`codecs:Depends` ELF scan. The manual `libva2` dependency does not cover
`libva-drm2`, whose `vaGetDisplayDRM` is called by the native bridge.
Installing optional `xpra-codecs-extras` or vendor packages must not be needed
to repair ordinary codec dependencies.

## Source provenance and current decision

The manual reassessment is bound to source
`d95058b0916913fe6ae5296fb702f66d833898b0`. Upstream
`d346004a576` already assigns AOM, JPH, libva, VPL and de265 to their
packages. `e66ae9be2f1` runs `dh_missing --fail-missing` immediately
after installation. Other current upstream changes ship the Weston helper,
integrate systemd units and install the Wireshark dissector at its valid path.
Current `not-installed` deliberately excludes Python package metadata.

The decision is **adapt and narrow**. Remove the old file-list, service,
dissector, egg-info and duplicate missing-file-check changes. Preserve the
upstream payload split and lifecycle policy. No missing-file exception or
package assignment remains owned by this patch.

The residual dependency defect is visible in current rules: the ordinary codec
scan excludes `aom/`, `jph/`, `libva/`, `vpl/` and other codec
paths despite their current package ownership. Its companion extras scan also
uses a manually maintained inverse list. Substitution variables are written for
each scanned package; calculating a dependency under the wrong prefix does not
make the consumer's `Depends` use it.

## Surrounding code and ownership

| Component | Responsibility |
| --- | --- |
| Setup and pkg-config | Select and link the enabled native extensions against the actual distribution libraries. |
| Debian codec `.files` lists | Assign each staged module to its binary package. |
| `dh_movefiles`, `dh_install`, `dh_missing` | Materialize package payloads and reject unassigned staged results. |
| `dh_shlibdeps` | Select a package's actual ELF files and invoke library dependency analysis. |
| `dpkg-shlibdeps` | Resolve those ELF requirements using the distribution's library/symbol metadata. |
| `control` and `dh_gencontrol` | Consume the matching dependency substitution variables. |
| Fork DEB builder and host validator | Inspect the returned real packages, enforce native ownership and reject missing required dependencies. |

The libva encoder and decoder registrations request `libva,libva-drm` on
Linux. Their shared bridge opens the DRM display with `vaGetDisplayDRM`.
Neither Python codec discovery nor a successful build can make the omitted
runtime library dependency optional.

## Patch mechanics

The patch changes only the two codec invocations in
`packaging/debian/xpra/rules`:

```make
dh_shlibdeps -pxpra-codecs -Xpipewire/ -- -pcodecs
dh_shlibdeps -pxpra-codecs-extras -- -pcodecsextras
```

The argument positions are deliberate. Before `--`, debhelper's
`-p<package>` selects the binary package whose payload is scanned.
After `--`, `dpkg-shlibdeps -p<prefix>` selects the substitution-variable
prefix, not a package. The prefixes match the existing `codecs:Depends`
and `codecsextras:Depends` fields in `control`.
See the official [debhelper package-selection options](https://manpages.debian.org/trixie/debhelper/debhelper.7.en.html#SHARED_DEBHELPER_OPTIONS),
[dh_shlibdeps argument forwarding](https://manpages.debian.org/trixie/debhelper/dh_shlibdeps.1.en.html#OPTIONS)
and [dpkg-shlibdeps substitution prefix](https://manpages.debian.org/trixie/dpkg-dev/dpkg-shlibdeps.1.en.html#OPTIONS).

Package ownership replaces inverse codec-name exclusion lists. Each newly
packaged ordinary or extras ELF participates in the correct scan without
requiring every other scan to add its name to a blacklist. Ordinary scanning
now includes libva, JPH, AOM, VPL and x264 where they were actually built.
Extras scanning follows the current extras payload, including AVIF, de265 and
FFmpeg, without pulling those libraries into ordinary codecs.

PipeWire remains the one explicit exception in ordinary codecs: upstream
deliberately places its library in `Recommends` and allows that codec to be
unavailable without it. The patch keeps that policy. NVIDIA/AMD package payloads
are not scanned into either codec prefix; no vendor dependency is introduced.

The default non-codec and separate X11/compression/proc scans stay unchanged.
Existing manual library declarations and distro-specific preprocessing remain
upstream-owned. Actual ELF analysis supplies the missing dependencies and
version bounds; this patch does not guess an OpenJPH SONAME, add loader tricks,
or silence missing-library/symbol errors.

## Failure paths and queue interactions

An unresolved ELF library must fail the real dependency step, not be suppressed
or compensated with the builder's installed environment. Absent optional codec
outputs contribute no ELF input. Files assigned to an unexpected package remain
a payload-ownership failure in the retained package validator.

The [JPH build case](../jph-parallel-build-objects/README.md) now retains only
shared-header incremental invalidation while preserving upstream's disjoint
objects. It does not assign Debian files or dependencies. This case does not
change setup, codec algorithms, parallelism or native buffer ownership.
Both responsibilities are exercised together by complete-stack package builds.

No test-only path is introduced into installed packages. There is no change to
package publication, archive transfer, builder image ownership or the source
freeze boundary.

## Durable regression and package proof

The case names existing `unit.codecs.video_helper_test` for adjacent codec
discovery behavior; it is not a substitute for package output. The durable
failure boundary is the real DEB runner under
`fork-maintenance/infra/deb-packages/`, on both supported distributions.

Before returning output, the builder inventories every actual DEB, rejects
duplicate package identities and overlapping regular payload paths, and locates
five ABI-matched native modules: libva encoder/decoder, libyuv converter and JPH
encoder/decoder. All five must belong to ordinary `xpra-codecs`.

It extracts actual `xpra-common` and `xpra-codecs` payloads into a private
root, imports those exact files with distribution Python, and runs
`dpkg-shlibdeps` on their actual ELF objects. Derived dependency names must
be present in final `xpra-codecs Depends`; the checks explicitly require
libva, libva-drm and libyuv dependencies and do not guess the OpenJPH package
name. This comparison is necessary because native imports in a dependency-rich
builder alone would miss the defect.

The extracted JPH pair also performs a deterministic 32x32 quality-100 lossless
RGB roundtrip. Comparison honors decoded BGRX rowstride and ignores X/row
padding; it makes no alpha-preservation claim. Both image owners are released
on failure as well as success.

The host independently parses the returned ar/control/data archives and
repeats package-set, payload-owner, ABI and required dependency-name checks.
Native imports, pixel execution and actual ELF resolution remain container-side
checks, not claims based on filenames or host emulation.

## Clean control and validation

This packaging-only case owns no test file, so `PATCH_MODE=tests-only` is
unavailable; the focused runner does not support a `clean` patch mode either.
A rejected invocation is not a negative control. The documented substitute is
the current-source analysis above, paired with the supported complete-stack
real-package oracle. The DEB runner has no clean/partial-stack package mode and
must not be bypassed with an ad hoc package probe.

During an upstream refresh, complete the manual-review/composed exit before
runtime checks. Follow the
[development and final-acceptance flow](../../docs/runbooks/validation.md).
Run the affected helper and package-runner controls, and use a real distribution
build when testing the disputed ELF/control boundary. Do not rebuild both
distributions after every intermediate edit.

The final frozen candidate requires:

- the affected codec module and package-runner checks, plus complete offline
  fork-control checks;
- all three full upstream legs and required composed/native checks;
- one valid Ubuntu 26.04 and one valid Debian 13 complete-stack DEB build,
  including the actual five-module imports, dependency closure and JPH pixels;
- all nine complete-stack live profiles through
  `live-all STACK=develop RUN=<fresh-prefix>` and `live-suite-check`,
  with the complete queue on both endpoints.

Historical official-package audits explain the original omission but do not
prove current failure or acceptance. Bind all current results to the exact
source, queue, harness, immutable builder and returned package set.
