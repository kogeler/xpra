# Independently owned JPH parallel-build objects

## Boundary

Each independently scheduled extension must own every writable compiler output
which it produces. The JPH encoder and decoder are separate Python extensions,
so their native object paths must remain disjoint even though both use the same
handwritten C++ implementation.

The Cython-generated encoder and decoder translation units each include
`jph.cpp`. Their compiler outputs are respectively `encoder.o` and `decoder.o`;
there is no separately compiled, shared `jph.o`. Both extensions retain their
own definitions of the unchanged glue functions and link against the selected
OpenJPH library. Parallel `build_ext` remains enabled for the entire build.

This is a build-output ownership case, not a codec-algorithm, package-policy,
runtime-loader or global build-scheduling change. A successful serial build is
not proof of its parallel ownership contract.

## Source provenance

At embedded source commit `212038243d0067b6860ebe7d6953692179ef353f`,
the JPH extension registrations originate in the upstream codec addition
`7119afca852281fb2a03c546d7df8761bbf95718`; the handwritten implementation
was added by `f0b4976f751de8dc8b0a41882c067f648d03be6a`. Both registrations
name `xpra/codecs/jph/jph.cpp` as an additional source. Current upstream codec
changes retain that arrangement while evolving dimension validation, quality
selection and codec discovery independently.

Setuptools' parallel extension builder shares a compiler and `build_temp`
directory across extension workers. Its object naming follows the source path,
not the owning extension name: both original registrations therefore produce
the same writable `build_temp/xpra/codecs/jph/jph.o`. An extension can be linking
that object while the other compiles it. This collision follows directly from
the actual extension and compiler plans; proving it does not require assuming
one particular compiler/LTO timing or making a sporadic failure occur again.

Upstream history supplies technical provenance only. The fork's isolated
workflow and [validation procedure](../../docs/runbooks/validation.md) govern
editing and acceptance.

## Surrounding code and ownership

| Component | Responsibility |
| --- | --- |
| `setup.py` feature selection | Resolves the `jph`, `jph_encoder` and `jph_decoder` switches and installed-library availability. |
| `setup.py` `ace` / `tace` | Builds each extension declaration, including its source language and pkg-config compiler/linker flags. |
| Cython generation and dependency discovery | Produces one C++ translation unit for each `.pyx` and records the header and included implementation as build dependencies. |
| Setuptools `build_ext` and compiler | Schedules independent extensions and maps their source paths to writable object paths. |
| `encoder.pyx` / `decoder.pyx` | Expose Python codec APIs and supply separately owned translation units. |
| `jph.h` / `jph.cpp` | Declare the C interface and implement the OpenJPH bridge, error handling and buffer allocation. |
| `setup.py` clean / `MANIFEST.in` | Preserve handwritten implementation inputs while removing generated files and constructing source distributions. |
| DEB build and package validation | Compile and link the real native pair, assign its modules to `xpra-codecs`, and exercise the extracted package payload. |

These responsibilities must remain distinct. A packaging manifest cannot make
two compiler workers own the same object safely. Conversely, disjoint object
names cannot establish that the resulting native modules are linked, packaged,
importable or lossless.

## Feature selection and extension registration

`setup.py` chooses `libopenjph` when that pkg-config name is available, otherwise
the `openjph` name. Automatic enablement still depends on the current upstream
default-feature policy and the minimum supported library version. Explicit
`--with-jph` selects both roles; the encoder and decoder remain independently
selectable. This patch changes neither the dependency threshold nor the
authority of pkg-config over include paths, libraries and linker flags.

`tace` conditionally delegates to `ace`. The latter converts the Python module
name to its `.pyx` source and constructs a Cython extension with the requested
C++ language. The JPH registrations now contain only their own module names;
they no longer append a second translation unit to both extensions. The
surrounding extension machinery, compiler selection, C++ flags and parallelism
are unchanged.

The generated extensions still import the normal Xpra buffer and image support
modules. There is no extra shared glue library, cross-extension import,
registration order, preload step or build helper in the installed package.
Loading only the decoder must not require the encoder extension to exist, and
vice versa.

## Translation units and incremental dependencies

Each `.pyx` retains its existing `cdef extern from "jph.h"` declarations. A
separate `cdef extern from "jph.cpp": pass` instructs Cython to include the
implementation in that extension's generated C++ source. Keeping the header
declarations separate preserves the existing C interface and its `nogil`
annotations. The included implementation itself already includes its header
and the real OpenJPH headers.

The handwritten implementation is shared as a read-only input, not as a writable
build artifact. The encoder's generated source owns one object, and the
decoder's generated source owns another. Each final extension contains its own
copy of the glue just as it does when linked from a separately compiled helper;
the patch does not introduce shared mutable runtime state between them.

Cython's dependency discovery includes files referenced by `cdef extern from`.
Both the header and implementation therefore remain inputs of each extension's
native build. Changing `jph.cpp` must invalidate both extension builds even
though it is no longer listed as a separately compiled source. This dependency
is essential: removing the shared object without retaining implementation
dependency tracking would permit a stale incremental binary.

The focused test inspects the dependencies produced by the real Cython setup
path, not a hand-constructed `Extension`. It also asks the actual configured
compiler to derive object filenames from the resulting sources. Neither
dependency nor object expectations are inferred from patch text.

## Codec and buffer contracts remain unchanged

The encoder is a picture-codec API, not a persistent video encoder. Its
`encode("jph", image, options)` returns the normal seven-field picture result
containing a `Compressed` payload, dimensions, options and 24-bit color depth.
It translates supported packed RGB/BGR channel layouts into the existing C++
bridge. Quality 100 selects the unchanged reversible OpenJPH encoding path.
Input stride validation, optional scaling, content-type quantizer policy and
buffer lifetime stay with the existing encoder code.

The decoder consumes a codestream and returns a packed `ImageWrapper` in
`BGRX` format. Dimensions and rowstride come from the codestream and retain the
existing validation before exposing pixels. Native allocation is handed to the
normal `MemBuf` owner; including the implementation does not transfer ownership
between extensions or alter release behavior.

The codec carries RGB color, not an alpha-preservation contract. A lossless
roundtrip comparison must compare the three color channels with the reported
rowstride and ignore the padding byte of `BGRX`. It must not treat unused X bytes
as alpha, require their identity, or infer alpha support from the encoder's
acceptance of four-byte input layouts.

## Clean, source distribution and package boundaries

`setup.py` already lists handwritten `jph.cpp` among sources which `clean` must
preserve. `jph.h` is not a generated Cython output. `MANIFEST.in` already carries
both files and includes the `.pyx` inputs, so no generated wrapper file or
manifest exception is needed. Clean and source-distribution operations must
retain those exact input bytes; generated encoder/decoder C++ files remain
disposable build results.

The standalone case applies to the embedded source without other production
patches. The complete queue also contains `x11-client-clipboard-events`, which
changes a separate GTK X11 packaging decision in `setup.py`; the two cases must
compose without absorbing each other's setup changes. The
`debian-libva-codecs-package` case owns binary-package assignment and libva/libyuv
requirements, not JPH compiler-output ownership. This case does not change
Debian rules, dependency installation or codec package membership.

The real Ubuntu 26.04 and Debian 13 DEB boundaries build with their distribution
OpenJPH development packages and normal parallelism. They must validate actual
JPH encoder/decoder modules in the extracted `xpra-codecs` payload, correct
Python ABI, native imports, library dependencies and a deterministic quality-100
RGB roundtrip. Distribution library versions need not be equal; each result
must bind its actual toolchain and runtime dependencies.

## Regression design

`unit.codecs.jph_build_test` creates a disposable source copy below the test
source's build directory. It invokes the actual setup option parser, extension
registration and Cython generation in a separate Python process. The public
`--skip-build` planning route avoids requiring OpenJPH development packages in
every upstream-test image; it neither fabricates pkg-config responses nor
substitutes mock extension declarations. Unrelated features are explicitly
disabled because the existing `--with-cython` alias enables a broad feature
group, even alongside `--minimal`.

The test exercises four boundaries:

- With both roles enabled, the compiler-derived object sets have exactly one
  extension owner per object. On clean source this fails specifically on the
  shared `jph.o`, rather than an unavailable native dependency.
- Both actual extension plans retain the shared implementation and header as
  sources or discovered dependencies.
- Encoder-only and decoder-only selections contain exactly the requested JPH
  extension, preserving independent feature toggles.
- The actual `clean` and `sdist` paths retain and archive the exact handwritten
  helper/header and `.pyx` inputs.

This is a deterministic build-plan and source-distribution control. It does not
compile OpenJPH glue, perform LTO, import a JPH native extension or prove a codec
roundtrip. Those distinct positive requirements belong to the real package
boundary above. The selected existing
`unit.codecs.lossless_roundtrip_test` checks surrounding picture-codec behavior
and exercises JPH when present, but its optional codec discovery is not a
substitute for mandatory packaged JPH imports and pixels.

## Patch ownership and invariants

`fix.patch` owns only `setup.py`, the two JPH `.pyx` files and the new focused
build test. Preserve these invariants during adaptation:

- No compiler-output path is written by two independent extension workers.
- Shared helper and header bytes remain unchanged and remain dependencies of
  both roles.
- C++ language, library discovery, compiler/linker options and independent
  encoder/decoder selection retain upstream semantics.
- Generated files are not committed; clean and sdist preserve all handwritten
  inputs needed to regenerate each extension.
- Build planning cannot masquerade as native compilation, package validation
  or runtime pixel proof.
- No global serialization, timeout, retry, preload, environment workaround or
  per-application behavior is introduced.

## Required validation

Follow the [two-phase validation runbook](../../docs/runbooks/validation.md).
During development run the focused build regression immediately after an atomic
edit, together with the manifest's affected picture-codec module. Retain a
named tests-only clean control which fails on the shared-object assertion and a
patched control in the same frozen image; neither may fail before reaching that
assertion because of a fixture or setup error. Resolve the standalone case and
its complete-stack composition before final acceptance.

The changed `.pyx` files and the focused planning control already require real
Cython generation in ordinary focused mode. Compiled-Python or compatibility
modes are additional dimensions when their boundaries change, not substitutes
for C++ compiler/linker execution. On the frozen complete queue, fill the final
full-suite and fork-control obligations and both real parallel DEB builds,
including the mandatory extracted-package JPH native-pair roundtrip. Keep the
original clean failure and each result's exact source, image and toolchain
identities.

This build-only case declares no atomic display/live gate. Existing case-owned
runtime gates and the enclosing complete-stack live obligations remain with
their established owners; a JPH build fix neither weakens them nor creates an
unrelated live scenario.
