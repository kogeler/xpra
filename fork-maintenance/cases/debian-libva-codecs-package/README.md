# Package Native libva Codecs In xpra-codecs

## Failure boundary

The Debian build detects and compiles the native libva encoder and decoder,
but `xpra-codecs.files` does not claim the resulting `xpra/codecs/libva`
directory. `dh_movefiles` therefore leaves those modules in `debian/tmp`, and
the published `xpra-codecs` package cannot provide native libva H.264 coding.

The upstream build route and the official repository build scripts use the same
Debian packaging manifests. A signed-repository audit of official beta version
`6.6-r42421-1` found libyuv in `xpra-codecs` but no packaged libva modules in any
Xpra binary package. The downstream failure is therefore not caused by invoking
a different Debian build entry point; it is an unclosed payload-ownership
boundary in the shared packaging metadata.

## Patch boundary

The patch assigns the complete `xpra/codecs/libva` directory to the ordinary
`xpra-codecs` package. It does not move the modules into, or add a consumer
dependency on, the AMD, NVIDIA, or extras codec packages.

The patch also enables `dh_missing --fail-missing`, so every file installed in
`debian/tmp` must be assigned to a binary package rather than relying on a
libva-specific omission check. The first real fail-closed build also exposed
the other staged results that lacked explicit ownership. The patch assigns the
compiled AOM, JPEG 2000, oneVPL, and de265 codec trees according to the existing
upstream package split, assigns Python package metadata and the Weston helper,
and records only the deliberately uninstalled service/dissector artifacts in
`not-installed`.

The DEB runner then validates the whole package
inventory twice: once inside the builder before output and again on the host
after extraction from the returned tar. It rejects duplicate package names and
overlapping payload ownership, resolves the required native Python modules from
the actual package contents, and verifies that they belong to `xpra-codecs`.

The builder extracts the actual `xpra-common` and `xpra-codecs` DEBs into a
private root, imports the three native modules with the distro Python, and uses
`dpkg-shlibdeps` on those packaged ELF files to prove that every dynamically
resolved dependency is present in the final `Depends` field. This validates a
usable installed capability rather than only selected filenames.

## Required validation

- `unit.codecs.video_helper_test`;
- `make -C fork-maintenance/infra/deb-packages check`;
- the complete offline `make -C fork-maintenance check`;
- one fresh Ubuntu 26.04 and one fresh Debian 13 package build;
- installation and import of the packaged libva encoder, libva decoder, and
  libyuv converter in fresh matching containers.
