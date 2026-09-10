# JPH build ownership and shared-header dependencies

## Boundary

The JPH encoder and decoder are independently built Python extensions. Each
must own its writable compiler objects, and every handwritten input which can
change its native code must participate in incremental-build invalidation.
The current upstream role split already provides disjoint objects. This case
retains only the missing dependency on their shared `jph_common.h` header.

This is a build-dependency case, not a codec algorithm, runtime-loader,
package-assignment or global scheduling change. A successful fresh or serial
build cannot prove that an existing extension will be rebuilt after its common
header changes.

## Source provenance and current decision

The manual review is bound to embedded source
`d95058b0916913fe6ae5296fb702f66d833898b0`. Upstream `13b67e313a6`
replaced the historical shared `jph.cpp` with `jph_encode.cpp` and
`jph_decode.cpp`, their role-specific headers, and `jph_common.h`.
The encoder and decoder registrations now compile different handwritten
sources, so the original shared writable `jph.o` collision no longer exists.

The decision is **adapt and narrow**, not retain the historical implementation.
Do not restore the removed `cdef extern from "jph.cpp"` inclusions or make
both extensions contain the whole old bridge. No production Cython hunk remains
in this case.

Both handwritten C++ sources include `jph_common.h`, but the clean setup
declarations omit it from `Extension.depends`. Cython discovers the
role-specific headers named by the `.pyx` extern declarations; it does not
recursively discover arbitrary C++ preprocessor include chains. The native
`build_ext` up-to-date check compares the output with `sources + depends`
before calling the compiler. Therefore, changing only this common header can
leave both installed extension outputs incorrectly considered up to date.

The retained production change adds the common header to both existing
extension declarations. Current source and build-tool code establish the
residual defect; applicability of the old patch would not establish necessity.
Upstream history is technical provenance only. The fork's
[validation procedure](../../docs/runbooks/validation.md) governs acceptance.

## Surrounding code and ownership

| Component | Responsibility |
| --- | --- |
| `setup.py` feature selection | Selects JPH roles and the available OpenJPH pkg-config name/version. |
| `setup.py` `ace` / `tace` | Registers each real extension, passing its sources, dependencies, C++ language and library flags. |
| Cython dependency discovery | Generates each wrapper and discovers its directly declared native header dependencies. |
| Native `build_ext` | Decides whether each extension output is current before invoking its compiler. |
| Compiler object naming | Maps each generated or handwritten source to a writable object path. |
| `jph_encode.cpp` / `jph_decode.cpp` | Independently implement the two native codec roles. |
| `jph_common.h` | Supplies common implementation inputs to both roles. |
| Clean and source distribution | Preserve all handwritten inputs required to regenerate both extensions. |
| Real DEB validation | Proves actual compilation, linking, package ownership, imports and lossless RGB pixels. |

A correct package manifest cannot repair an omitted incremental dependency.
Conversely, a correct dependency graph cannot establish native linkage or
pixel correctness. These are separate acceptance boundaries.

## Feature selection and extension registration

Library discovery remains upstream-owned: setup prefers `libopenjph` when
available and otherwise uses `openjph`, retaining its existing minimum-version
and default-feature policy. `--with-jph` selects both roles; encoder-only and
decoder-only selections remain independent.

Each registration retains its current generated wrapper plus its role-specific
handwritten C++ source. The added `depends=["xpra/codecs/jph/jph_common.h"]`
passes through `tace`, `ace`, and the actual Cython extension constructor.
Cython preserves this explicit dependency while adding the role header it
discovers from the corresponding extern declaration.

The encoder's writable objects belong only to the encoder; the decoder's
objects belong only to the decoder. Shared header bytes are read-only inputs,
not shared compiler outputs. A header change must invalidate every selected
role, while an unchanged input set must still allow the normal up-to-date
skip. There is no forced rebuild, global serialization or compiler lock.

The patch leaves C++ language selection, standard flags, pkg-config include and
link options, parallel extension scheduling and codec discovery unchanged.
Neither role imports or preloads the other. It introduces no shared glue
library, runtime registration order or installed build helper.

## Codec and buffer contracts remain unchanged

The encoder remains a picture codec with the existing seven-field result,
packed RGB/BGR input conversion and quality-100 reversible encoding. Dimension,
stride, scaling, content-type quantizer and buffer-lifetime rules stay with
current upstream code.

The decoder returns its normal packed `BGRX` `ImageWrapper`, using the
codestream's validated dimensions and the normal `MemBuf` allocation owner.
Declaring a build dependency neither changes native allocations nor transfers
ownership between extensions.

JPH's lossless contract here is RGB color, not alpha preservation. Roundtrip
validation compares the three color channels using the returned rowstride.
The unused X byte is neither alpha nor a required byte-identical channel;
acceptance of four-byte input layouts does not establish alpha support.

## Clean, source distribution and queue interactions

Current setup already protects both handwritten role `.cpp` files from
`clean`; the three headers are not disposable Cython outputs.
`MANIFEST.in` already includes all five handwritten files and the normal
`.pyx` inputs. This case changes neither clean policy nor source distribution
membership. Their exact bytes must survive clean and archive creation.

Generated encoder/decoder wrappers remain disposable build results. They are
not committed or shipped as a substitute for the handwritten inputs.

The case applies independently to the embedded source. The refreshed
`x11-client-clipboard-events` no longer owns its absorbed setup packaging
hunk. `debian-libva-codecs-package` owns the remaining binary-package
dependency boundary, not JPH source/object registration. The complete queue
must preserve both responsibilities without importing Debian policy into setup.

Ubuntu 26.04 and Debian 13 package builds use their actual distribution
OpenJPH development packages and normal parallel compilation. Both must find
the real JPH encoder and decoder in ordinary `xpra-codecs`, bind the matching
Python ABI, import both modules, derive their ELF dependencies and complete the
deterministic quality-100 RGB roundtrip. Library versions need not match across
distributions; each result must bind its real inputs.

## Regression design and clean control

`unit.codecs.jph_build_test` copies only the required source inputs into a
private temporary build tree. Its separate setup processes run the actual
option parser, extension registration and Cython generation. The public
`--skip-build` route avoids requiring OpenJPH development packages for this
planning control; it does not fabricate pkg-config output or substitute
hand-constructed extension declarations. Unrelated feature groups are
explicitly disabled, including aliases otherwise enabled by `--with-cython`.

Five focused boundaries are retained:

- With both roles selected, actual compiler-derived object filenames have one
  extension owner each. This is a positive preservation control on the new
  clean base, not the current negative oracle.
- Each actual extension plan contains its own handwritten source/header and
  the common header as sources or dependencies; it never compiles the opposite
  role's handwritten source.
- The actual native `build_ext.build_extension` up-to-date branch skips an
  unchanged extension and reaches compilation after only the common header's
  timestamp advances. The test intercepts the compiler's compile entry with a
  sentinel exception, after the production skip decision, so no native compile
  or link is performed. The output marker and timestamp changes belong only to
  the private copy; the header timestamp is restored afterward.
- Encoder-only and decoder-only plans contain exactly the requested role.
- Actual clean and sdist operations preserve and archive the exact two
  handwritten C++ sources, three headers and two `.pyx` inputs.

The tests-only clean control must reach the missing common-header dependency
and/or the incorrect changed-header skip. It must not fail because old
`jph.cpp`/`jph.h` names disappeared, a new API is missing, or native
development packages are unavailable. Both role configurations are covered.

The compiler-entry interception makes this a deterministic dependency/skip
control, not native compilation, LTO, codec loading or pixel evidence.
`unit.codecs.lossless_roundtrip_test` remains the affected picture-codec
module; its optional codec discovery does not replace mandatory packaged JPH
imports and the native-pair RGB roundtrip.

## Patch ownership and invariants

`fix.patch` owns only the setup dependency additions and the case-owned focused
build test. Keep these invariants when adapting it:

- Writable object paths remain independently owned, following the upstream
  role split rather than the retired shared-source architecture.
- Common-header changes invalidate every selected role; unchanged inputs retain
  incremental skipping.
- Role implementations, header bytes, codec APIs and buffer ownership remain
  unchanged.
- Library discovery, C++ options and independent feature toggles retain upstream
  semantics.
- Clean/sdist retain every handwritten dependency, without committing generated
  wrappers or forcing all builds to run.
- Planning and static checks never masquerade as native or package acceptance.

## Required validation

During an explicit upstream refresh, complete the incremental manual-review
and composed-review exit before executing this regression or real packages.
Static checks and applicability checks may run during review. After that exit,
follow the [development and final-acceptance flow](../../docs/runbooks/validation.md):
run a non-vacuous tests-only clean control and the patched focused build and
picture-codec modules on the frozen inputs.

The setup probe performs real Cython generation but no C++ compilation.
Compiled-Python and no-compat modes cover their respective execution paths;
neither replaces the two real parallel DEB builds and mandatory extracted
native-pair roundtrip. Resolve the atomic case and complete queue, then fill
the required fork-control, focused/native and three full upstream legs.

This build-only case needs no invented atomic display fixture. It still
requires the complete nine-profile live suite through
`make -C fork-maintenance live-all STACK=develop RUN=<fresh-prefix>` and
`live-suite-check`, with the entire queue on both endpoints. Existing runtime
owners and their warning/pixel/input oracles remain unchanged. Retain exact
source, queue, image and toolchain identities; do not reuse historical
shared-object failure output as current acceptance evidence.
