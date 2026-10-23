# Keep the preloaded VA display alive for sandboxed decoding

Code: the `Fork-Case: libva-decoder-display-lifetime` commit on `develop`
(`make -C fork-maintenance case-show CASE=libva-decoder-display-lifetime`).

## Boundary

On Linux the default client seccomp configuration installs an `errno` filter
on the decode thread after codec loading. A client which advertises libva
H.264 from its successful startup probe then tries to create its first window
decoder. Previously that creation reopened the render node and reinitialized
VA. The filter denies `openat`, yielding `LIBVA_DEC_NOT_AVAILABLE` despite
advertised support. With only libva enabled, no decoder remains for that
encoding. This is a resource-lifecycle defect, not missing hardware capability
or a reason to disable the filter. Unfiltered clients also unnecessarily paid
for one driver initialization per decoder context.

This case keeps hardware decoding available without granting file access.
Runtime capability-loss notification/fallback is a separate behavior; this
case does not make a failed GPU infallible or prevent unrelated invalid streams.

## Embedded-source context

The embedded Xpra source has a dedicated `Decode` subsystem and preloads all
codecs on its worker before installing `xpra.seccomp.draw`. Maintainer changes
`ec39a2eb2d` cache adapter probing, while `0280b399df` skips NVIDIA's VA wrapper;
neither retains the initialized adapter for window contexts. In
`va_decode.c:try_device` successful probing still terminates the display;
`libva_decoder_create` opens it again. Keep the case until upstream preserves
an initialized display across that boundary, forbids implicit reopen during
retirement, and safely handles overlapping old/new module generations.

The upstream maintainer explicitly identifies hardware decoder resource access
as a remaining [seccomp restriction][seccomp-review]. This case addresses the
demonstrated repeated-open failure, not every possible driver syscall. Keeping
the display is a candidate design, not evidence that a particular driver's
submission, mapping and teardown are safe under the unchanged filter. Actual
post-filter physical decoding remains necessary on the hardware covered by
acceptance; a mocked VA boundary cannot establish driver compatibility.

The maintainer's [VA decoder review][va-review] calls for separate corrections
and preserving builds without X11. His [cleanup review][cleanup-review] and
resulting `d1ca74f` / `aae59ec` changes preserve early invalidation and cleanup
by the resource-owning worker instead of moving ownership to make a local
diff look simpler. Here publication is likewise retired immediately while
decoder resources retain their actual owner. The implementation adds no X11
dependency, server-encoder cleanup rule or codec-selection policy. These are
constraints derived from prior reviews, not approval of this new patch.

## Surrounding code and ownership map

| Entry/resource | Owner and consumers |
| --- | --- |
| `Decode.preload` | Calls `Encodings.load_all_codecs` before the thread filter; other codec consumers wait for loading. |
| `loader.load_codec` / `decoder.init_module` | Configures logging, explicitly probes and publishes one display generation. |
| `try_device` / `LibVADisplay` | Opens the DRM descriptor and initializes the VA driver; owns one module reference. Rejected adapters are terminated/closed immediately. |
| `Decoder.init_context` / `libva_decoder_create` | Borrows the published generation through a retained reference; owns independent config, surfaces, context and parser state. |
| `Decoder.decompress_image` | Runs VA submission, sync, map and copying under its backing's decoder-operation ownership; does not acquire a new display. |
| `Decoder.clean` / `libva_decoder_destroy` | Destroys only this decoder's resources, then releases its generation reference. |
| `Encodings.cleanup` / `unload_codecs` / `cleanup_module` | Retires the module generation. Actual terminate/close waits for its last decoder. |

The Cython `display_lock` serializes startup, capability reads, creation,
destruction and retirement, including `nogil` native calls. It protects C
generation pointers/reference counts without introducing platform-specific
mutex APIs. It is not a replacement for the backing's per-decoder lock:
concurrent decode and clean of the *same* context still require that owner.
Decoding on distinct contexts can proceed independently; an extant decoder
retains its generation while module cleanup runs.

The existing VPL decoder's `_cache_lock` and `_cache_shutdown` demonstrate the
same project-level need to coordinate native state with late `Decoder.clean()`
after module teardown. VPL owns one cached decoder, whereas libva now owns a
shared display used by multiple simultaneous contexts; reusing that cache
literally would impose the wrong ownership. One Cython lock serializes the
existing entrypoints across their `nogil` regions. Adding C mutexes as well,
or a process-wide codec manager, would duplicate this boundary. The display
reference count is necessary because the decoder worker may outlive the short
join in `Decode.cleanup`, not speculative protection against arbitrary C API
callers. Only this wrapper and the explicit serialized C regression use that
API in the project.

## Mechanism and lifecycle

Successful probing transfers its already initialized display and descriptor to
a heap-owned generation with one module lease. A rejected/unsupported adapter
still closes immediately; allocation failure also closes before returning.
Capabilities are published only for the selected adapter. Repeated explicit
startup preserves the current probe result and does not open another device.

Creation requires an available published generation. It no longer invokes
startup, nor calls `libva_open_display`. After parser allocations succeed it
retains the generation and copies immutable device/vendor/profile metadata.
Config, surfaces and context creation remain unchanged. Any later setup failure
uses normal decoder destruction: initialized resources are freed and only the
new decoder lease is released, leaving the module usable for another context.

Cleanup first unpublishes the generation, resets availability/probe and clears
cached metadata/capabilities, then releases the module lease. Repeated cleanup
is harmless. Capability queries and decoder construction never implicitly
probe; calls after retirement report unavailable instead of reopening from a
filtered or late callback. Only explicit `init_module` may initialize again.
That initialization must occur before filtering, as the existing preload
contract requires; it is not a sandbox escape or permission to reconnect by
initializing codecs on a filtered worker.

An old decoder may outlive retirement and explicit initialization of a new
generation. It retains the *old* generation, not a reference to a mutable
global display. Its cleanup cannot terminate the new generation. The last
reference terminates VA once, closes its exact descriptor once and frees the
generation. Windows retains its existing Win32 adapter selection and fd=-1;
only the shared lifetime changes. Driver/GPU-loss errors remain errors and do
not trigger hidden reprobes.

## Case-commit and integration ownership

The product commit owns `va_decode.c`, its Cython wrapper, the deterministic
C/Python regression pair and the corresponding hardware-decoder explanation in
`docs/Usage/Seccomp.md` and `xpra.seccomp.draw` comments. Those explanations do
not change the syscall policy. It does not modify the encoder or common open helper:
encoder contexts remain independently owned and are not decode-thread consumers.
There is no case dependency. `video-pipeline-cleanup-race` protects server-side
encoder publication; it does not own client VA displays. The subsurface case
owns backing transactions and per-context operations, which this case preserves.
The packet error and shutdown cases likewise retain their distinct boundaries.

Live image build/filter verification and test-image dependencies are shared
control inputs, not installed product changes. Queue composition must still
exercise old/new decoder cleanup alongside window close and application exit.

## Commit scope and non-goals

No protocol, codec preference, encoding list, driver selection, CSC, bitstream
parser, output format, rendering policy or package ownership changes. No
`open`/`openat` permission is added. Keeping only a descriptor without
`vaInitialize` would not preload the driver; disabling seccomp or silently
removing libva would hide the advertised/runtime mismatch. A process-global
display pointer without generation leases would instead create use-after-free
on cleanup. The test substitutes VA entrypoints, not actual GPU behavior;
supported-driver execution remains a physical acceptance obligation.

Retaining only the descriptor and calling `vaInitialize` later is insufficient:
driver loading can still perform file access after the filter. A single global
display terminated unconditionally by `cleanup_module` invalidates extant
contexts, while retaining it forever discards explicit module cleanup. Removing
the default filter or broadening it to permit opens changes the security policy
instead of repairing lifetime. Dropping libva from advertised capabilities is
the fallback design only if real supported-driver constraints make keeping it
unsafe; it is not silently substituted for the requested hardware path.

## Regression design and clean control

`unit.codecs.libva_display_test` compiles the actual `va_decode.c` with official
VA headers and a linked deterministic driver boundary. It uses real opens of
`/dev/null` as the selected device, real descriptor closure, and real Xpra
seccomp installation in separate subprocesses. Missing compiler, headers,
library or native filter fails rather than skipping. No new production API is
needed by the tests-only clean control.

- `errno`: preload, install the real decode allowlist, create/destroy a context;
  an independent open canary must still return EPERM. Clean production fails
  context creation, not import/setup. `log` permits the same operation and
  canary, distinguishing the deny action from device discovery.
- `shared`: two simultaneous contexts use one initialized display; freeing
  either context must not terminate the module's display.
- `retired`: cleanup with two outstanding decoders leaves them valid, blocks
  implicit resurrection and clears metadata; explicit reinit followed by old
  cleanup must release only the corresponding generation.
- `partial`: independently fail config, surfaces and context setup; check
  resource draining and successful subsequent creation on the same display.
- `reprobe`: successful generation, unsupported reprobe, then successful
  explicit initialization must not expose stale capabilities or leak a display.

The fake VA driver asserts every call uses a live display/descriptor and that
termination sees zero resources. It does not submit compressed video, prove
driver thread/syscall requirements, or exercise the Cython lock. Adjacent
`decode_test`, `seccomp_encoding_test` and `seccomp_test` preserve worker
ordering and the security policy. Native wrapper/build and physical live
execution close the linkage and actual decode blind spots; compiled/no-compat
focused and final legs retain their normal obligations.

## Durable live or package boundary

The complete-stack H.264 profiles use libva on the physical render node with
client software CSC disabled and native OpenGL presentation. Their saved
H.264 packets, VA context/submit/sync evidence, exact libva decoder logs,
hardware presentation and pixel/input/lifecycle checks remain required.
The client build must contain Xpra's own native seccomp module, and runtime
evidence must prove the default `errno` decode policy is installed before
accepted libva frames. A Python seccomp package, startup capability list, or
successful unfiltered self-test alone proves none of those conditions.

Every endpoint contains the entire queue. All nine profiles remain mandatory;
H.264 exercises the disputed resource path, while other profiles challenge
cleanup and the now-enforced shared filter. Real release packages must retain
native decoder linkage and imports on Ubuntu and Debian. Transient results and
any unavailable physical driver-family coverage belong in the cycle ledger,
never in claims that a designed check already passed.

## Invariants not to simplify

- Open and initialize during explicit unfiltered startup, not first context.
- Keep a module lease even between windows; decoder destruction cannot close it.
- Retire publication separately from freeing resources; keep generations distinct.
- Serialize every reference transfer even when the VA operation releases the GIL.
- Capability reads and late creates must not call startup after cleanup.
- Preserve file denial and require its canary; a green render is not sandbox proof.

## Required validation

Physical deployment acceptance targets AMD/radeonsi. Intel/iHD and a second
GPU vendor are not required gates or blockers for this deployment. This scope
does not disable other drivers or claim that untested drivers were validated.
The default seccomp policy and all AMD hardware-decoding checks remain required.

Follow [canonical validation](../../docs/runbooks/validation.md): tests-only
behavioral negative plus patched focused regression/adjacent modules, composed
focused checks, compiled and no-compat coverage, full fork-control suite and
all three upstream full legs after candidate freeze. Build/import the actual
native decoder; mocks alone do not accept native changes. Run the full
nine-profile live loop on both complete-stack endpoints, then one uninterrupted
pass and `live-suite-check`. Applicable native package boundaries require
real extracted-module imports, not merely source inclusion. Static checks use
the current Ruff configuration; this C/Cython boundary does not add modules to
the separate scoped Python mypy contract.

[seccomp-review]: https://github.com/Xpra-org/xpra/issues/622#issuecomment-5972499066
[va-review]: https://github.com/Xpra-org/xpra/pull/4964#pullrequestreview-4732149183
[cleanup-review]: https://github.com/Xpra-org/xpra/pull/5019#discussion_r3855149170
