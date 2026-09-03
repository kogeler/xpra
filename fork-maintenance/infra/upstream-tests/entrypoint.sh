#!/usr/bin/env bash
set -euo pipefail

HOST_RUNNER=/opt/xpra-fork-maintenance/upstream-tests
PAYLOAD_HELPER="$HOST_RUNNER/container_payload.py"
INPUTS=/work/payload
SOURCE="$INPUTS/source.bundle"
SNAPSHOT_LAB="$INPUTS/lab"
WORK=/work/xpra
SOURCE_MIRROR=/work/source.git
RESOLUTION="$INPUTS/selection-resolution.json"

if test ! -e "$INPUTS"; then
    python3 "$PAYLOAD_HELPER" extract --destination "$INPUTS"
fi

PATCH_MODE=${XPRA_PATCH_MODE:-patched}
SELECTION=${XPRA_FORK_SELECTION:-}
EXPECTED_COMMIT=${XPRA_EXPECTED_SOURCE_COMMIT:-}
EXPECTED_SOURCE_HEAD=${XPRA_EXPECTED_SOURCE_HEAD:-}
EXPECTED_SOURCE_REF=${XPRA_EXPECTED_SOURCE_REF:-}
EXPECTED_WORKFLOW_SHA=${XPRA_EXPECTED_WORKFLOW_SHA:-}
SELECTION_DIGEST=${XPRA_EXPECTED_SELECTION_SHA:-}

selection_tool() {
    python3 "$HOST_RUNNER/selection.py" \
        --lab-root "$SNAPSHOT_LAB" \
        --selection "$SELECTION" \
        "$@"
}

validate_inputs() {
    case "$PATCH_MODE" in
        clean|tests-only|patched) ;;
        *) printf 'invalid patch mode: %s\n' "$PATCH_MODE" >&2; return 2 ;;
    esac
    [[ "$EXPECTED_COMMIT" =~ ^[0-9a-f]{40}$ ]] || {
        printf 'invalid expected source commit: %s\n' "$EXPECTED_COMMIT" >&2
        return 2
    }
    [[ "$EXPECTED_SOURCE_HEAD" =~ ^[0-9a-f]{40}$ ]] || {
        printf 'invalid expected source bundle head: %s\n' "$EXPECTED_SOURCE_HEAD" >&2
        return 2
    }
    case "$EXPECTED_SOURCE_REF" in
        refs/remotes/origin/master|refs/remotes/upstream/master) ;;
        *)
            printf 'invalid expected source ref: %s\n' "$EXPECTED_SOURCE_REF" >&2
            return 2
            ;;
    esac
    [[ "$EXPECTED_WORKFLOW_SHA" =~ ^[0-9a-f]{64}$ ]] || {
        printf 'invalid expected workflow digest: %s\n' "$EXPECTED_WORKFLOW_SHA" >&2
        return 2
    }
    [[ "$SELECTION_DIGEST" =~ ^[0-9a-f]{64}$ ]] || {
        printf 'invalid selection digest: %s\n' "$SELECTION_DIGEST" >&2
        return 2
    }
    test -f "$SOURCE" && test ! -L "$SOURCE"
    test -d "$SNAPSHOT_LAB" && test ! -L "$SNAPSHOT_LAB"
    test "$(git bundle list-heads "$SOURCE")" = \
        "$EXPECTED_SOURCE_HEAD $EXPECTED_SOURCE_REF"
    selection_tool validate
    test "$(selection_tool digest)" = "$SELECTION_DIGEST"
}

prepare_source() {
    local after before resolution_sha
    validate_inputs
    test ! -e "$SOURCE_MIRROR"
    test ! -e "$WORK"
    git clone --quiet --mirror "$SOURCE" "$SOURCE_MIRROR"
    git -C "$SOURCE_MIRROR" bundle verify "$SOURCE" >/dev/null
    git clone --quiet --no-hardlinks --no-checkout "$SOURCE_MIRROR" "$WORK"
    git -C "$WORK" merge-base --is-ancestor "$EXPECTED_COMMIT" "$EXPECTED_SOURCE_HEAD"
    git -C "$WORK" checkout --quiet --detach "$EXPECTED_COMMIT"
    test "$(git -C "$WORK" rev-parse HEAD)" = "$EXPECTED_COMMIT"
    git -C "$WORK" cat-file -e "$EXPECTED_COMMIT:.github/workflows/test.yml"
    test "$(git -C "$WORK" show "$EXPECTED_COMMIT:.github/workflows/test.yml" | sha256sum | awk '{print $1}')" = "$EXPECTED_WORKFLOW_SHA"
    test -z "$(git -C "$WORK" status --porcelain=v1 --untracked-files=all)"

    selection_tool resolve \
        --source-tree "$WORK" \
        --source-commit "$EXPECTED_COMMIT" > "$RESOLUTION"
    resolution_sha=$(python3 - "$RESOLUTION" <<'PY'
import json
import sys

with open(sys.argv[1], encoding="utf-8") as stream:
    print(json.load(stream)["resolution_sha256"])
PY
)
    [[ "$resolution_sha" =~ ^[0-9a-f]{64}$ ]]
    printf '%s\n' "$resolution_sha" > "$INPUTS/selection-resolution.sha256"

    cd "$WORK"
    printf 'source_commit=%s\nsource_bundle_head=%s\nsource_bundle_ref=%s\nworkflow_sha256=%s\nselection=%s\nselection_sha256=%s\nselection_resolution_sha256=%s\npatch_mode=%s\n' \
        "$EXPECTED_COMMIT" "$EXPECTED_SOURCE_HEAD" "$EXPECTED_SOURCE_REF" \
        "$EXPECTED_WORKFLOW_SHA" "$SELECTION" "$SELECTION_DIGEST" "$resolution_sha" "$PATCH_MODE"
    python3 --version
    cython --version
    gcc --version | head -n 1
    ld --version | head -n 1
    pkg-config --version
    sha256sum "$HOST_RUNNER/entrypoint.sh" "$HOST_RUNNER/selection.py" "$PAYLOAD_HELPER"
    find "$SNAPSHOT_LAB" -type f -print0 | sort -z | xargs -0 sha256sum
    selection_tool gates | sed 's/^/selected_gate=/'

    while IFS=$'\t' read -r case_slug patch_status patch_path patch_sha; do
        test -n "$case_slug" && test -n "$patch_status" && test -n "$patch_path"
        local patch="$SNAPSHOT_LAB/$patch_path"
        test -f "$patch"
        test "$(sha256sum "$patch" | awk '{print $1}')" = "$patch_sha"
        printf 'selected_case=%s selected_patch=%s patch_status=%s patch_sha256=%s\n' \
            "$case_slug" "$patch_path" "$patch_status" "$patch_sha"
        if test "$patch_status" = already-present; then
            git apply --reverse --check --whitespace=error-all "$patch"
        elif test "$patch_status" = apply && test "$PATCH_MODE" = patched; then
            git apply --check --index --whitespace=error-all "$patch"
            git apply --index --whitespace=error-all "$patch"
            git apply --reverse --check "$patch"
        elif test "$patch_status" = apply && test "$PATCH_MODE" = tests-only; then
            before=$(git write-tree)
            git apply --check --index --whitespace=error-all --include='tests/**' "$patch"
            git apply --index --whitespace=error-all --include='tests/**' "$patch"
            git apply --reverse --check --include='tests/**' "$patch"
            after=$(git write-tree)
            test "$before" != "$after" || {
                printf 'case %s contributed no regression tests\n' "$case_slug" >&2
                return 2
            }
        elif test "$patch_status" != apply; then
            printf 'invalid resolved patch status: %s\n' "$patch_status" >&2
            return 2
        fi
    done < <(python3 - "$RESOLUTION" <<'PY'
import json
import sys

with open(sys.argv[1], encoding="utf-8") as stream:
    resolution = json.load(stream)
for entry in resolution["patches"]:
    print(
        entry["case"],
        entry["status"],
        entry["patch"],
        entry["patch_sha256"],
        sep="\t",
    )
PY
)
    git diff --check
    git diff --cached --check
}

installed_xpra_dir() {
    find "$WORK/dist" -type d -path '*/dist-packages/xpra' -print -quit
}

check_elf() {
    local object=$1 output
    test -n "$object" && test -f "$object"
    readelf -d "$object" | grep -E 'NEEDED|SONAME' || true
    output=$(ldd "$object" 2>&1)
    printf '%s\n' "$output"
    if grep -E 'not found' <<<"$output"; then
        return 1
    fi
}

selection_has_case() {
    selection_tool cases | grep -Fx "$1" >/dev/null
}

require_gate() {
    selection_tool gates | grep -Fx "$1" >/dev/null || {
        printf 'selection %s does not declare the %s gate\n' "$SELECTION" "$1" >&2
        return 2
    }
}

libyuv_patch_mode() {
    printf '%s\n' "$PATCH_MODE"
}

libyuv_smoke_test() {
    local smoke
    smoke=$(selection_tool local-tests | grep -E '/libyuv_smoke\.py$' | head -n 1)
    test -n "$smoke"
    printf '%s\n' "$SNAPSHOT_LAB/$smoke"
}

check_focused_native_modules() {
    local xpra_dir converter events display keyboard output smoke_mode smoke_test
    xpra_dir=$(installed_xpra_dir)
    test -n "$xpra_dir"
    cd "$WORK/tests/unittests"

    if selection_tool gates | grep -Fx libyuv >/dev/null; then
        converter=$(find "$xpra_dir/codecs/libyuv" -maxdepth 1 -name 'converter*.so' -print -quit)
        check_elf "$converter"
        PYTHONPATH=".:${xpra_dir%/xpra}" python3 - <<'PY'
from xpra.codecs.libyuv import converter

print(converter)
PY
        smoke_mode=$(libyuv_patch_mode)
        smoke_test=$(libyuv_smoke_test)
        PYTHONPATH=".:${xpra_dir%/xpra}" \
            python3 "$smoke_test" \
            --patch-mode "$smoke_mode"
    fi

    if selection_tool gates | grep -Fx wayland >/dev/null; then
        events=$(find "$xpra_dir/wayland/server" -maxdepth 1 -name 'events*.so' -print -quit)
        display=$(find "$xpra_dir/wayland/server" -maxdepth 1 -name 'display*.so' -print -quit)
        keyboard=$(find "$xpra_dir/wayland/server" -maxdepth 1 -name 'keyboard*.so' -print -quit)
        check_elf "$events"
        check_elf "$display"
        check_elf "$keyboard"
        output=$(ldd -r "$events" "$display" "$keyboard" 2>&1 || true)
        printf '%s\n' "$output"
        if grep -E 'not found|undefined symbol: (wl_list_insert|wl_list_remove|wl_display_flush_clients)' <<<"$output"; then
            return 2
        fi
        PYTHONPATH=".:${xpra_dir%/xpra}" python3 - <<'PY'
from xpra.wayland.server import display, events, keyboard

print(display, events, keyboard)
PY
    fi
}

run_focused() {
    local extra_args test_path
    local -a selected_paths selected_tests
    case "$PATCH_MODE" in
        patched|tests-only) ;;
        *)
            printf '%s\n' 'focused regressions require PATCH_MODE=patched or tests-only' >&2
            return 2
            ;;
    esac
    prepare_source
    mapfile -t selected_tests < <(selection_tool unit-tests)
    test "${#selected_tests[@]}" -gt 0 || {
        printf 'selection has no focused unit tests: %s\n' "$SELECTION" >&2
        return 2
    }
    for test_path in "${selected_tests[@]}"; do
        test_path=${test_path//./\/}.py
        test -f "$WORK/tests/unittests/$test_path" || {
            printf 'selected unit test is missing after patching: %s\n' "$test_path" >&2
            return 2
        }
        selected_paths+=("$test_path")
    done
    extra_args='--with-terminal_client'
    if selection_tool gates | grep -Fx libyuv >/dev/null; then
        extra_args+=' --with-csc_libyuv --with-argb'
    fi
    if selection_tool gates | grep -Fx wayland >/dev/null; then
        extra_args+=' --with-keyboard --with-wayland_server'
    fi
    cd "$WORK"
    CFLAGS='-O0 -g0' \
    CXXFLAGS='-O0 -g0' \
    env \
        EXTRA_ARGS="$extra_args" \
        python3 setup.py unittests "${selected_paths[@]}"
    check_focused_native_modules
}

run_wayland() {
    require_gate wayland
    prepare_source
    cd "$WORK"
    CFLAGS='-O0 -g0' \
    CXXFLAGS='-O0 -g0' \
    EXTRA_ARGS='--minimal --with-modules --with-server --with-keyboard --with-wayland_server' \
        python3 setup.py unittests unit/wayland/linkage_test.py

    local xpra_dir events display keyboard output
    xpra_dir=$(installed_xpra_dir)
    events=$(find "$xpra_dir/wayland/server" -maxdepth 1 -name 'events*.so' -print -quit)
    display=$(find "$xpra_dir/wayland/server" -maxdepth 1 -name 'display*.so' -print -quit)
    keyboard=$(find "$xpra_dir/wayland/server" -maxdepth 1 -name 'keyboard*.so' -print -quit)
    test -n "$events" && test -n "$display" && test -n "$keyboard"
    readelf -d "$events" "$display" "$keyboard" | grep -E 'File:|NEEDED.*libwayland-server\.so\.0'
    output=$(ldd -r "$events" "$display" "$keyboard" 2>&1 || true)
    printf '%s\n' "$output"
    if grep -E 'not found|undefined symbol: (wl_list_insert|wl_list_remove|wl_display_flush_clients)' <<<"$output"; then
        return 2
    fi

    find tests/unittests/unit/wayland -type d -name __pycache__ -prune -exec rm -rf {} +
    cd tests/unittests
    XPRA_MODULE_DIR="$xpra_dir" PYTHONPATH=".:${xpra_dir%/xpra}" \
        python3 unit/run.py unit/wayland
}

run_libyuv() {
    local xpra_dir converter smoke_mode smoke_test
    require_gate libyuv
    prepare_source
    cd "$WORK"
    if pkg-config --exists libyuv; then
        printf '%s\n' 'libyuv.pc is present; fallback linkage path was not isolated' >&2
        return 1
    fi
    test -f /usr/include/libyuv.h
    ldconfig -p | grep 'libyuv\.so'
    CFLAGS='-O0 -g0' \
    CXXFLAGS='-O0 -g0' \
    EXTRA_ARGS='--minimal --with-modules --with-client --with-csc_libyuv --with-argb' \
        python3 setup.py unittests unit/codecs/csc_colorspace_test.py

    xpra_dir=$(installed_xpra_dir)
    converter=$(find "$xpra_dir/codecs/libyuv" -maxdepth 1 -name 'converter*.so' -print -quit)
    check_elf "$converter"
    readelf -d "$converter" | grep -E 'NEEDED.*libyuv\.so\.0'
    ldd "$converter" | grep 'libyuv\.so\.0'
    cd tests/unittests
    smoke_mode=$(libyuv_patch_mode)
    smoke_test=$(libyuv_smoke_test)
    PYTHONPATH=".:${xpra_dir%/xpra}" \
        python3 "$smoke_test" \
        --patch-mode "$smoke_mode"
}

run_full() {
    local cythonize=$1 compat=$2 gate=$3 pyver user_site
    require_gate "$gate"
    prepare_source
    cd "$WORK"
    pyver=$(python3 -c 'import sys; print(f"python{sys.version_info.major}.{sys.version_info.minor}")')
    user_site=$(python3 -m site --user-site)
    mkdir -p "$user_site"
    printf '%s\n' \
        "$WORK/dist/$pyver/usr/lib/$pyver/dist-packages" \
        "$WORK/dist/$pyver/usr/local/lib/$pyver/dist-packages" \
        > "$user_site/xpra-test-build.pth"
    dbus-run-session -- env \
        CYTHONIZE_MORE="$cythonize" \
        EXTRA_ARGS='--with-terminal_client' \
        XPRA_BACKWARDS_COMPATIBLE="$compat" \
        python3 setup.py unittests \
        --skip-fail unit.client.splash_test \
        --skip-slow unit.client.x11_client_test \
        --skip-slow unit.server.subsystem.startdesktop_option_test \
        --skip-slow unit.x11.x11_server_test \
        --skip-slow unit.server.server_auth_test \
        --skip-slow unit.server.shadow_server_test \
        --skip-slow unit.server.subsystem.start_option_test \
        --skip-slow unit.server.subsystem.shadow_option_test
}

run_quarantine() {
    local cythonize=$1 compat=$2 gate=$3 output status module
    local -a quarantined skip_args test_paths
    require_gate "$gate"
    test "$PATCH_MODE" = clean || {
        printf '%s\n' 'quarantine reassessment requires PATCH_MODE=clean' >&2
        return 2
    }
    mapfile -t quarantined < <(selection_tool quarantined-tests)
    test "${#quarantined[@]}" -gt 0 || {
        printf 'selection %s has no quarantined test modules\n' "$SELECTION" >&2
        return 2
    }
    for module in "${quarantined[@]}"; do
        skip_args+=(--skip-fail "$module")
        test_paths+=("${module//./\/}.py")
    done

    prepare_source
    cd "$WORK"
    output=/work/quarantine-summary.log
    set +e
    dbus-run-session -- env \
        CYTHONIZE_MORE="$cythonize" \
        EXTRA_ARGS='--with-terminal_client' \
        XPRA_BACKWARDS_COMPATIBLE="$compat" \
        python3 setup.py unittests \
        "${skip_args[@]}" "${test_paths[@]}" 2>&1 | tee "$output"
    status=${PIPESTATUS[0]}
    set -e
    if test "$status" -ne 0; then
        printf 'quarantine probe failed before producing an accepted test summary: %s\n' "$status" >&2
        return "$status"
    fi
    python3 - "$output" "${quarantined[@]}" <<'PY'
import re
import sys
from pathlib import Path

output = Path(sys.argv[1]).read_text(encoding="utf-8", errors="replace")
expected = tuple(sys.argv[2:])
markers = [match.start() for match in re.finditer(r"(?m)^test summary:$", output)]
if not markers:
    raise SystemExit("quarantine probe produced no unit-test summary")
summary = output[markers[-1]:]

def count(label: str) -> int:
    match = re.search(rf"(?m)^  {re.escape(label)}: ([0-9]+)$", summary)
    if not match:
        raise SystemExit(f"quarantine probe omitted {label!r}")
    return int(match.group(1))

ignored_match = re.search(
    r"(?ms)^  ignored failures: [0-9]+\n(?P<items>(?:    - .+\n)*)",
    summary,
)
ignored = ()
if ignored_match:
    ignored = tuple(
        match.group(1)
        for match in re.finditer(
            r"(?m)^    - (unit(?:\.[a-z0-9_]+)+) \(exit code=[0-9]+\)$",
            ignored_match.group("items"),
        )
    )

if count("successful tests") or count("failed tests"):
    raise SystemExit(
        "quarantine is stale or contaminated: a selected module passed or failed outside --skip-fail"
    )
if count("ignored failures") != len(ignored) or ignored != expected:
    raise SystemExit(
        f"quarantine is stale: expected ignored failures {expected!r}, observed {ignored!r}"
    )
print("quarantine still required for: " + ", ".join(ignored))
PY
}

case "${1:-help}" in
    versions)
        prepare_source
        python3 --version
        cython --version
        python3 -m coverage --version
        pkg-config --modversion wlroots-0.19 wayland-server
        pkg-config --modversion libyuv 2>/dev/null || printf '%s\n' 'libyuv pkg-config metadata: absent'
        ;;
    patch-check)
        prepare_source
        git -C "$WORK" diff --cached --name-status
        git -C "$WORK" ls-files --others --exclude-standard
        ;;
    focused)
        run_focused
        ;;
    wayland)
        run_wayland
        ;;
    libyuv)
        run_libyuv
        ;;
    full)
        run_full without 1 full
        ;;
    full-cython)
        run_full with 1 full-cython
        ;;
    full-no-compat)
        run_full without 0 full-no-compat
        ;;
    quarantine)
        run_quarantine without 1 quarantine
        ;;
    quarantine-cython)
        run_quarantine with 1 quarantine-cython
        ;;
    quarantine-no-compat)
        run_quarantine without 0 quarantine-no-compat
        ;;
    run)
        prepare_source
        cd "$WORK"
        bash -lc "${XPRA_TEST_COMMAND:?XPRA_TEST_COMMAND is required}"
        ;;
    *)
        printf 'unknown target: %s\n' "${1:-}" >&2
        exit 2
        ;;
esac
