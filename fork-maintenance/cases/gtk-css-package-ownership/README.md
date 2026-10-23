# Shared GTK CSS belongs to the common package

Code: the `Fork-Case: gtk-css-package-ownership` commit on `develop`
(`make -C fork-maintenance case-show CASE=gtk-css-package-ownership`).

## Boundary

Installing the Debian GTK3 client without `xpra-server` omits
`/usr/share/xpra/css`. Importing `xpra.client.gtk3.client_base` calls
`inject_css_overrides()` with its default enabled policy; `load_css()` reports
the missing directory and the client loses its application styles. The same
ownership error exists in the RPM split. Installing the full metapackage hides
the error because it pulls in the server. Source installs also hide it because
they do not apply binary package ownership lists.

The correction makes existing shared CSS available to all its consumers. It
does not attribute a codec, detach or native teardown failure to CSS.

## Embedded-source context

Current `setup.py` installs CSS under `data_ENABLED`, not `server_ENABLED`.
Both package formats nevertheless assign the directory to their server
subpackage. The common package already owns `xpra.gtk`, icons and other shared
resources, and both clients and servers require it.

Antoine Martin's [closed GTK3 split issue #3797](https://github.com/Xpra-org/xpra/issues/3797)
and its maintainer-authored Debian split preserve installation of non-GTK
clients without a GTK client. Put the inert styles beside their existing
shared loader; do not add a server dependency, force GTK installation, or
duplicate the resource in two packages. The common package already has these
shared assets; no new toolkit dependency is introduced.

The maintainer accepted [PR #5013](https://github.com/Xpra-org/xpra/pull/5013)
for a prior file transfer between split packages. His discussion explicitly
questions why dpkg needs transfer metadata even during a joint upgrade. The
resulting current `xpra-wayland` stanza uses versioned `Breaks` and `Replaces`.
Reuse that mechanism for CSS moving from server to common rather than assuming
that apt's upgrade order avoids overwrite conflicts. This precedent is not
approval of this new patch.

## Surrounding code and ownership map

`fs/share/xpra/css/*.css` are build inputs. `setup.py` places them in the shared
data directory. Debian's `*.files` lists and RPM's `%files` sections assign
binary ownership; the package manager, not the Python client, installs and
removes them. `xpra.platform.posix.paths.do_get_resources_dir()` discovers the
installed resource directory. `xpra.gtk.css_overrides.load_css()` consumes its
`css` child for the GTK client and shared dialogs, including splash styling.
The loader remains in common and its main-thread/provider lifecycle is
unchanged. There is no new runtime state or synchronization.

## Mechanism and lifecycle

Move the one directory declaration from server to common in both formats.
Fresh client-only installations then receive the styles through their
existing exact-version common dependency. Full installations still have one
owner and the same paths and bytes; removing the server no longer removes
styles used by the remaining client.

Debian common declares `Breaks` and `Replaces` against older server versions,
using `${binary:Version}` like the existing Wayland transition. `Replaces`
permits common to take files before the old server archive is upgraded;
`Breaks` prevents keeping that old server configured after the transfer.
The server's existing exact-version common dependency stays intact. Do not
replace these relationships with `Conflicts` or `--force-overwrite`.
RPM retains its existing exact-version common dependency; only its `%files`
ownership changes. Data-disabled/source builds, non-POSIX bundling and the
runtime CSS opt-out are unaffected. An absent source data payload or a failed
package build is not repaired by this case.

## Case-commit and integration ownership

This commit owns the Debian common/server file lists, the common transition
metadata, the equivalent RPM ownership move and
`unit.gtk_css_packaging_test`. It has no case dependencies: none of the codec,
protocol, clipboard or cleanup cases establish resource ownership. Composed
live runs still require every active case on both endpoints.

The exact-DEB live installer and its nonroot resource readability guards live
in fork controls, not this product commit. In particular, do not weaken those
guards or install the server on the client to make the old package split pass.

## Commit scope and non-goals

No CSS contents, loader behavior, toolkit defaults, native module, protocol or
service activation changes. Moving the loader into the GTK client would break
shared dialog consumers; duplicating styles would create payload overlap.
Changing every data-file owner or revisiting the split architecture is outside
this atomic fix. This case does not publish a release or claim RPM runtime
validation from a DEB build.

## Regression design and clean control

`unit.gtk_css_packaging_test` reads real current package inputs. It requires a
nonempty source CSS set and verifies that each Debian CSS file matches exactly
one declaration, in common; it separately checks the RPM section and the two
Debian transition fields. On clean source it identifies the actual server
owner and missing relationships, not a missing API or test dependency.

These are source-level package contract tests, with no mocked package manager
or GTK. They do not execute debhelper, RPM macro expansion, an upgrade or CSS
loading. Real DEB builds and installed client-only live endpoints close the
fresh-install/runtime gap. `infra/live/package_runtime.py:css_upgrade_probe`
also feeds the built DEB's actual transfer fields into minimal no-script
packages and runs real dpkg in isolated private roots. An older server owns a
CSS file plus an unrelated sentinel. Without `Replaces`, unpacking common must
fail specifically on overwriting the file. With the built transfer metadata,
common must take sole ownership; upgrading server and configuring both must
succeed and preserve the new CSS bytes. Its attestation binds the DEB hash.
These packages isolate metadata from dependency resolution and service hooks;
they are not an upgrade of a previously published full release. Real release
upgrades and RPM builds/upgrades remain distinct platform limits. In particular,
the first fixed release must have a strictly newer package version: a same-
version rebuild is not covered by the versioned `Breaks`/`Replaces` relation.

## Durable live or package boundary

The package runner builds the complete queue independently for Ubuntu and
Debian and checks regular-file ownership and nonoverlap in actual DEBs.
Live endpoints install exactly those accepted artifacts. The client package
set excludes both server packages. Both endpoint images require the installed
CSS header-bar file and icon to be readable as the runtime user, then use the
packaged GTK code and resources for the live workload. This reaches the
configuration missed by a full metapackage or a second source installation.
All nine profiles remain required; a build-stage file test is not live
acceptance. Raw run outcomes and pending checks belong in the session ledger.

## Invariants not to simplify

- Shared loader and styles must remain reachable through common without a
  server or GTK-client dependency cycle.
- Each resource has exactly one current binary owner in each format.
- Debian transfer metadata must accompany ownership moves even when both
  packages normally upgrade together.
- A client-only packaged runtime, not an all-components source installation,
  must exercise resource discovery and readability.

## Required validation

Follow [canonical validation](../../docs/runbooks/validation.md): the focused
module and its tests-only negative control, composed focused inventory, all
three full unit legs, both real DEB builds, and the complete nine-profile
packaged live suite, including the built-metadata dpkg positive and negative
controls on both distributions. No downstream scoped mypy module changes; static lint does not prove
package payloads or upgrade safety. Retire this case only when upstream assigns
shared CSS correctly in both package formats and preserves safe Debian file
transfer, not merely when a full install renders successfully.
