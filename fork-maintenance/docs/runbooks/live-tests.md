# Run Direct Xpra And Physical-GPU Tests

## Preconditions

Create and verify the hash-locked analysis environment:

```bash
make -C fork-maintenance live-venv
make -C fork-maintenance live-venv-check
```

Verify Podman, the private process-supervisor state, render-node access,
application inputs, repository identity, and current patch resolution:

```bash
make -C fork-maintenance doctor
make -C fork-maintenance isolated-start-check
make -C fork-maintenance workspace-create \
  STACK=develop WORKSPACE=live-preflight-01 PATCH_MODE=patched
make -C fork-maintenance workspace-remove WORKSPACE=live-preflight-01
```

The runner freezes live `upstream/master` and applies `stacks/develop.toml` in
its build context. The host develop source need not be patched.

Do not rerun a live profile solely for a proven comment, copyright, or
documentation-only patch refresh on the same master. First verify by exact
old/new applied-tree comparison that paths, modes, executable data,
configuration, test assertions, runner behavior, and live assertions are
unchanged, then run only resolution, whitespace, and fork-control checks and
record the proof. Any semantic difference requires the declared live gates.

For a clean-master live control, set `STACK=` so no selection is applied. The
run still freezes and records the same live master commit.

## RGB control

Run RGB before H.264 when isolating transport and presentation:

```bash
make -C fork-maintenance live-rgb \
  STACK=develop RUN=develop-rgb-01
make -C fork-maintenance live-wait RUN=develop-rgb-01
make -C fork-maintenance live-remove RUN=develop-rgb-01
```

The profile requires direct RGB transport, visible nonuniform pixels, real
input response, ordered lifecycle, and complete owned-object cleanup. RGB must
not silently become video.

## Adaptive Wayland H.264

The active queue's publication-grade graphics boundary uses frame-aware alpha
policy:

```bash
make -C fork-maintenance live-start \
  STACK=develop APPLICATION=zed ENCODING=h264 \
  H264_CLIENT_POLICY=adaptive-alpha \
  RUN=develop-h264-adaptive-01
make -C fork-maintenance live-wait RUN=develop-h264-adaptive-01
make -C fork-maintenance live-remove RUN=develop-h264-adaptive-01
```

Both endpoints advertise H.264 and alpha-capable picture encodings while
preferring H.264. Opaque `RGBX`/`BGRX` codec-aligned regions must use the
hardware H.264 path. An odd dimension may produce only the exact positive
one-pixel, non-alpha lossless RGB edge tied to the same window and flush
sequence. Full-window, interior, larger, or alpha-bearing RGB fallback fails.

`RGBA`/`BGRA` frames must use an alpha-capable picture encoding and must never
pass silently through H.264. The profile also proves the real application
Vulkan path, VA-API encode/decode, selected GPU presentation, visible pixels,
input, lifecycle, and cleanup. Software codec, renderer, reconnect, or
transport fallback is not acceptance.

Fallback policies remain diagnostic only:

```bash
make -C fork-maintenance live-start \
  STACK=develop ENCODING=h264 H264_CLIENT_POLICY=fallback-auto \
  RUN=develop-fallback-auto-01
```

They cannot satisfy the hardware-H.264 gate.

## Xpra-only lifecycle profiles

These profiles use direct TCP and deliberately exclude SSH ControlMaster or
parent-product orchestration.

Real detach:

```bash
make -C fork-maintenance live-xpra-detach \
  STACK=develop RUN=xpra-detach-01
make -C fork-maintenance live-wait RUN=xpra-detach-01
make -C fork-maintenance live-remove RUN=xpra-detach-01
```

Abrupt established transport loss through a run-owned single-connection TCP
proxy:

```bash
make -C fork-maintenance live-xpra-transport-loss \
  STACK=develop RUN=xpra-transport-loss-01
make -C fork-maintenance live-wait RUN=xpra-transport-loss-01
make -C fork-maintenance live-remove RUN=xpra-transport-loss-01
```

Multi-window Vulkan plus alpha-capable input fixture under strict H.264:

```bash
make -C fork-maintenance live-xpra-hardware \
  STACK=develop RUN=xpra-hardware-01
make -C fork-maintenance live-wait RUN=xpra-hardware-01
make -C fork-maintenance live-remove RUN=xpra-hardware-01
```

Do not weaken strict assertions, enable picture fallback, or encode a known
failure as expected success. A future fix passes the unchanged profile under a
new run identity.

## Evidence boundary

Only named-job execution is acceptance-capable. Foreground `run.py` output is
diagnostic. `live-wait` requires successful supervisor completion, hashed log,
exact report binding, current runner/supervisor digests, and no run-owned
Podman objects.

Every report, screenshot, trace, status, and log remains under ignored
`.artifacts/fork-maintenance/`. Never copy a final-looking report into the
tracked tree.

Use `live-status`, `live-logs`, `live-wait`, `live-collect`, and `live-remove`
for the normal lifecycle. If an unfinished run must be discarded before
collection, use `make -C fork-maintenance live-abort RUN=name`; it verifies the
recorded process identity, stops only its process group, removes exact labelled
Podman objects and partial results, and publishes no acceptance evidence.

Prefix every live `RUN` with the current cycle identity. After all profiles are
collected, reviewed, and individually removed, use the final two-phase cleanup
in `cycle-cleanup.md` to delete the retained cycle results.
