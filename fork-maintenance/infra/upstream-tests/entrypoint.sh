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
RESOLUTION_DIGEST="$INPUTS/selection-resolution.sha256"

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

require_exact_output() {
    local expected=$1 description=$2 actual
    shift 2
    if ! actual=$("$@"); then
        printf 'cannot determine %s\n' "$description" >&2
        return 2
    fi
    test "$actual" = "$expected" || {
        printf '%s is inconsistent\n' "$description" >&2
        return 2
    }
}

file_sha256() {
    sha256sum "$1" | awk '{print $1}'
}

workflow_blob_sha256() {
    git -C "$WORK" show "$EXPECTED_COMMIT:.github/workflows/test.yml" \
        | sha256sum | awk '{print $1}'
}

verified_resolution_patch_rows() {
    local output
    if ! output=$(selection_tool resolution-patches \
        --resolution "$RESOLUTION" \
        --digest-file "$RESOLUTION_DIGEST" \
        --source-commit "$EXPECTED_COMMIT" \
        --selection-sha256 "$SELECTION_DIGEST"); then
        printf 'cannot read verified patch rows for selection %s\n' "$SELECTION" >&2
        return 2
    fi
    test -n "$output" || {
        printf 'verified selection has no patch rows: %s\n' "$SELECTION" >&2
        return 2
    }
    printf '%s\n' "$output"
}

selected_focused_tests() {
    local output
    if ! output=$(selection_tool unit-tests); then
        printf 'cannot read focused unit tests for selection %s\n' "$SELECTION" >&2
        return 2
    fi
    test -n "$output" || {
        printf 'selection has no focused unit tests: %s\n' "$SELECTION" >&2
        return 2
    }
    printf '%s\n' "$output"
}

selected_gate_names() {
    local output
    if ! output=$(selection_tool gates); then
        printf 'cannot read gates for selection %s\n' "$SELECTION" >&2
        return 2
    fi
    printf '%s\n' "$output"
}

validate_inputs() {
    local expected_bundle_heads
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
    expected_bundle_heads="$EXPECTED_SOURCE_HEAD $EXPECTED_SOURCE_REF"
    if ! require_exact_output "$expected_bundle_heads" 'source bundle heads' \
        git bundle list-heads "$SOURCE"; then
        return 2
    fi
    if ! selection_tool validate; then
        printf 'selection %s is invalid\n' "$SELECTION" >&2
        return 2
    fi
    if ! require_exact_output "$SELECTION_DIGEST" 'selection digest' \
        selection_tool digest; then
        return 2
    fi
}

prepare_source() {
    local after before gates_output resolution_sha patch_rows_output
    local count_label expected_patch_count patch_index row row_extra row_index
    local case_slug patch_status patch_path patch_sha
    local -a patch_rows=()
    validate_inputs
    test ! -e "$SOURCE_MIRROR"
    test ! -e "$WORK"
    git clone --quiet --mirror "$SOURCE" "$SOURCE_MIRROR"
    git -C "$SOURCE_MIRROR" bundle verify "$SOURCE" >/dev/null
    git clone --quiet --no-hardlinks --no-checkout "$SOURCE_MIRROR" "$WORK"
    git -C "$WORK" merge-base --is-ancestor "$EXPECTED_COMMIT" "$EXPECTED_SOURCE_HEAD"
    git -C "$WORK" checkout --quiet --detach "$EXPECTED_COMMIT"
    if ! require_exact_output "$EXPECTED_COMMIT" 'checked-out source HEAD' \
        git -C "$WORK" rev-parse HEAD; then
        return 2
    fi
    git -C "$WORK" cat-file -e "$EXPECTED_COMMIT:.github/workflows/test.yml"
    if ! require_exact_output "$EXPECTED_WORKFLOW_SHA" 'source workflow digest' \
        workflow_blob_sha256; then
        return 2
    fi
    if ! require_exact_output '' 'checked-out source status' \
        git -C "$WORK" status --porcelain=v1 --untracked-files=all; then
        return 2
    fi

    if ! selection_tool resolve \
        --source-tree "$WORK" \
        --source-commit "$EXPECTED_COMMIT" > "$RESOLUTION"; then
        printf 'cannot resolve selection %s\n' "$SELECTION" >&2
        return 2
    fi
    if ! resolution_sha=$(python3 - "$RESOLUTION" <<'PY'
import json
import sys

with open(sys.argv[1], encoding="utf-8") as stream:
    print(json.load(stream)["resolution_sha256"])
PY
    ); then
        printf 'cannot read selection resolution digest\n' >&2
        return 2
    fi
    [[ "$resolution_sha" =~ ^[0-9a-f]{64}$ ]]
    printf '%s\n' "$resolution_sha" > "$RESOLUTION_DIGEST"
    if ! patch_rows_output=$(verified_resolution_patch_rows); then
        return 2
    fi
    mapfile -t patch_rows <<<"$patch_rows_output"
    IFS=$'\t' read -r count_label expected_patch_count row_extra <<<"${patch_rows[0]}"
    test "$count_label" = count \
        && [[ "$expected_patch_count" =~ ^[1-9][0-9]*$ ]] \
        && test -z "$row_extra" \
        && test "${#patch_rows[@]}" -eq "$((expected_patch_count + 1))" || {
        printf 'verified patch-row count is inconsistent for selection %s\n' "$SELECTION" >&2
        return 2
    }

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
    if ! gates_output=$(selected_gate_names); then
        return 2
    fi
    if test -n "$gates_output"; then
        sed 's/^/selected_gate=/' <<<"$gates_output"
    fi

    for row_index in "${!patch_rows[@]}"; do
        test "$row_index" -gt 0 || continue
        row=${patch_rows[$row_index]}
        IFS=$'\t' read -r patch_index case_slug patch_status patch_path patch_sha row_extra <<<"$row"
        [[ "$patch_index" =~ ^(0|[1-9][0-9]*)$ ]] \
            && test "$patch_index" -eq "$((row_index - 1))" \
            && test -n "$case_slug" \
            && test -n "$patch_status" \
            && test -n "$patch_path" \
            && [[ "$patch_sha" =~ ^[0-9a-f]{64}$ ]] \
            && test -z "$row_extra" || {
            printf 'verified patch-row order or shape is inconsistent at index %s\n' \
                "$((row_index - 1))" >&2
            return 2
        }
        local patch="$SNAPSHOT_LAB/$patch_path"
        test -f "$patch"
        if ! require_exact_output "$patch_sha" "selected patch digest for $case_slug" \
            file_sha256 "$patch"; then
            return 2
        fi
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
    done
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

require_gate() {
    local gates_output
    if ! gates_output=$(selected_gate_names); then
        return 2
    fi
    grep -Fx "$1" <<<"$gates_output" >/dev/null || {
        printf 'selection %s does not declare the %s gate\n' "$SELECTION" "$1" >&2
        return 2
    }
}

libyuv_patch_mode() {
    printf '%s\n' "$PATCH_MODE"
}

libyuv_smoke_test() {
    local local_test local_tests_output smoke=
    if ! local_tests_output=$(selection_tool local-tests); then
        printf 'cannot read local tests for selection %s\n' "$SELECTION" >&2
        return 2
    fi
    while IFS= read -r local_test; do
        case "$local_test" in
            */libyuv_smoke.py)
                test -z "$smoke" || {
                    printf 'selection %s has multiple libyuv smoke tests\n' "$SELECTION" >&2
                    return 2
                }
                smoke=$local_test
                ;;
        esac
    done <<<"$local_tests_output"
    test -n "$smoke" || {
        printf 'selection %s has no libyuv smoke test\n' "$SELECTION" >&2
        return 2
    }
    printf '%s\n' "$SNAPSHOT_LAB/$smoke"
}

check_focused_native_modules() {
    local xpra_dir clipboard compositor converter events display gates_output keyboard output smoke_mode smoke_test
    xpra_dir=$(installed_xpra_dir)
    test -n "$xpra_dir"
    cd "$WORK/tests/unittests"
    # setup.py cythonize_more includes xpra.net; this installed module binds
    # both the actual compiled mode and the process-wide compatibility policy.
    PYTHONPATH=".:${xpra_dir%/xpra}" python3 - \
        "$xpra_dir" "$CYTHONIZE_MORE" "$XPRA_BACKWARDS_COMPATIBLE" <<'FOCUSED_MODE_PY'
import sys
from importlib.machinery import EXTENSION_SUFFIXES
from pathlib import Path

from xpra.net import common

installed = Path(sys.argv[1]).resolve(strict=True)
module = Path(common.__file__).resolve(strict=True)
if not module.is_relative_to(installed):
    raise SystemExit("focused runtime module is outside the installed Xpra tree")
compiled = any(str(module).endswith(suffix) for suffix in EXTENSION_SUFFIXES)
if compiled != (sys.argv[2] == "with"):
    raise SystemExit("focused runtime compiled mode does not match the named target")
if common.BACKWARDS_COMPATIBLE is not (sys.argv[3] == "1"):
    raise SystemExit("focused runtime compatibility does not match the named target")
print(f"focused_runtime_module={module}")
print(f"focused_runtime_compiled={int(compiled)}")
print(f"focused_runtime_backwards_compatible={int(common.BACKWARDS_COMPATIBLE)}")
FOCUSED_MODE_PY
    if ! gates_output=$(selected_gate_names); then
        return 2
    fi

    if grep -Fx libyuv <<<"$gates_output" >/dev/null; then
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

    if grep -Fx wayland <<<"$gates_output" >/dev/null; then
        clipboard=$(find "$xpra_dir/wayland/server" -maxdepth 1 -name 'clipboard*.so' -print -quit)
        compositor=$(find "$xpra_dir/wayland/server" -maxdepth 1 -name 'compositor*.so' -print -quit)
        events=$(find "$xpra_dir/wayland/server" -maxdepth 1 -name 'events*.so' -print -quit)
        display=$(find "$xpra_dir/wayland/server" -maxdepth 1 -name 'display*.so' -print -quit)
        keyboard=$(find "$xpra_dir/wayland/server" -maxdepth 1 -name 'keyboard*.so' -print -quit)
        check_elf "$clipboard"
        check_elf "$compositor"
        check_elf "$events"
        check_elf "$display"
        check_elf "$keyboard"
        output=$(ldd -r "$clipboard" "$compositor" "$events" "$display" "$keyboard" 2>&1 || true)
        printf '%s\n' "$output"
        if grep -E 'not found|undefined symbol: (wl_list_insert|wl_list_remove|wl_display_flush_clients)' <<<"$output"; then
            return 2
        fi
        PYTHONPATH=".:${xpra_dir%/xpra}" python3 - <<'PY'
from xpra.wayland.server import clipboard, compositor, display, events, keyboard

print(clipboard, compositor, display, events, keyboard)
PY
    fi
}

run_focused() {
    local target=$1 cythonize compat extra_args gates_output selected_output test_path applied_tree
    local -a selected_paths selected_tests
    case "$target" in
        focused) cythonize=without; compat=1 ;;
        focused-cython) cythonize=with; compat=1 ;;
        focused-no-compat) cythonize=without; compat=0 ;;
        *) printf 'invalid focused mode: %s\n' "$target" >&2; return 2 ;;
    esac
    local -x CYTHONIZE_MORE="$cythonize" XPRA_BACKWARDS_COMPATIBLE="$compat"
    case "$PATCH_MODE" in
        patched|tests-only) ;;
        *)
            printf '%s\n' 'focused regressions require PATCH_MODE=patched or tests-only' >&2
            return 2
            ;;
    esac
    prepare_source
    if ! selected_output=$(selected_focused_tests); then
        return 2
    fi
    mapfile -t selected_tests <<<"$selected_output"
    for test_path in "${selected_tests[@]}"; do
        test_path=${test_path//./\/}.py
        [[ "$test_path" == *test.py ]] || {
            printf 'selected unit module is not an executable test: %s\n' "$test_path" >&2
            return 2
        }
        test -f "$WORK/tests/unittests/$test_path" || {
            printf 'selected unit test is missing after patching: %s\n' "$test_path" >&2
            return 2
        }
        selected_paths+=("$test_path")
    done
    extra_args='--with-terminal_client'
    if ! gates_output=$(selected_gate_names); then
        return 2
    fi
    if grep -Fx libyuv <<<"$gates_output" >/dev/null; then
        extra_args+=' --with-csc_libyuv --with-argb'
    fi
    if grep -Fx wayland <<<"$gates_output" >/dev/null; then
        extra_args+=' --with-keyboard --with-wayland_server --with-clipboard --with-dmabuf'
    fi
    if ! applied_tree=$(git -C "$WORK" write-tree); then
        printf '%s\n' 'cannot determine focused applied source tree' >&2
        return 2
    fi
    [[ "$applied_tree" =~ ^[0-9a-f]{40}$ ]] || {
        printf '%s\n' 'invalid focused applied source tree' >&2
        return 2
    }
    printf 'focused_mode=%s\nfocused_cythonize_more=%s\nfocused_backwards_compatible=%s\n' \
        "$target" "$cythonize" "$compat"
    printf 'focused_applied_tree=%s\n' "$applied_tree"
    printf 'focused_unit_test=%s\n' "${selected_tests[@]}"
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
    EXTRA_ARGS='--minimal --with-modules --with-server --with-keyboard --with-wayland_server --with-clipboard --with-dmabuf' \
        python3 setup.py unittests unit/wayland/linkage_test.py

    local xpra_dir clipboard compositor events display keyboard output
    xpra_dir=$(installed_xpra_dir)
    clipboard=$(find "$xpra_dir/wayland/server" -maxdepth 1 -name 'clipboard*.so' -print -quit)
    compositor=$(find "$xpra_dir/wayland/server" -maxdepth 1 -name 'compositor*.so' -print -quit)
    events=$(find "$xpra_dir/wayland/server" -maxdepth 1 -name 'events*.so' -print -quit)
    display=$(find "$xpra_dir/wayland/server" -maxdepth 1 -name 'display*.so' -print -quit)
    keyboard=$(find "$xpra_dir/wayland/server" -maxdepth 1 -name 'keyboard*.so' -print -quit)
    test -n "$clipboard" && test -n "$compositor" && test -n "$events" && test -n "$display" && test -n "$keyboard"
    readelf -d "$clipboard" "$compositor" "$events" "$display" "$keyboard" \
        | grep -E 'File:|NEEDED.*libwayland-server\.so\.0'
    output=$(ldd -r "$clipboard" "$compositor" "$events" "$display" "$keyboard" 2>&1 || true)
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
    local quarantined_output expected_output
    local -a quarantined=() expected=() skip_args=() test_paths=()
    require_gate "$gate"
    test "$PATCH_MODE" = clean || {
        printf '%s\n' 'quarantine reassessment requires PATCH_MODE=clean' >&2
        return 2
    }
    if ! quarantined_output=$(selection_tool quarantined-tests); then
        printf 'cannot read quarantine module union for selection %s\n' "$SELECTION" >&2
        return 2
    fi
    test -n "$quarantined_output" || {
        printf 'selection %s has no quarantined test modules\n' "$SELECTION" >&2
        return 2
    }
    mapfile -t quarantined <<<"$quarantined_output"
    if ! expected_output=$(selection_tool quarantined-tests --gate "$gate"); then
        printf 'cannot read quarantine assignment for gate %s\n' "$gate" >&2
        return 2
    fi
    if test -n "$expected_output"; then
        mapfile -t expected <<<"$expected_output"
    fi
    for module in "${expected[@]}"; do
        skip_args+=(--skip-fail "$module")
    done
    for module in "${quarantined[@]}"; do
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
    python3 - "$output" "$gate" "${#quarantined[@]}" "${expected[@]}" <<'QUARANTINE_SUMMARY_PY'
import re
import sys
from pathlib import Path

output = Path(sys.argv[1]).read_text(encoding="utf-8", errors="replace")
gate = sys.argv[2]
module_count = int(sys.argv[3])
expected = tuple(sys.argv[4:])
markers = [match.start() for match in re.finditer(r"(?m)^test summary:$", output)]
if not markers:
    raise SystemExit("quarantine probe produced no unit-test summary")
summary = output[markers[-1]:]

def count(label: str, *, optional: bool = False) -> int:
    matches = re.findall(rf"(?m)^  {re.escape(label)}: ([0-9]+)$", summary)
    if not matches and optional:
        return 0
    if len(matches) != 1:
        raise SystemExit(f"quarantine probe omitted {label!r}")
    return int(matches[0])

ignored_match = re.search(
    r"(?m)^  ignored failures: [0-9]+\n(?P<items>(?:    - [^\n]+\n)*)",
    summary,
)
ignored = ()
if ignored_match:
    lines = ignored_match.group("items").splitlines()
    parsed = []
    for line in lines:
        match = re.fullmatch(
            r"    - (unit(?:\.[a-z0-9_]+)+) \(exit code=[0-9]+\)",
            line,
        )
        if not match:
            raise SystemExit(f"quarantine probe has malformed ignored failure: {line!r}")
        parsed.append(match.group(1))
    ignored = tuple(parsed)

successful = count("successful tests")
failed = count("failed tests")
skipped = count("skipped tests", optional=True)
ignored_count = count("ignored failures", optional=True)
if failed or skipped:
    raise SystemExit(
        f"quarantine gate {gate} is contaminated: failed={failed}, skipped={skipped}"
    )
if successful != module_count - len(expected):
    raise SystemExit(
        f"quarantine gate {gate} expected {module_count - len(expected)} successful "
        f"modules, observed {successful}"
    )
if ignored_count != len(ignored) or ignored != expected:
    raise SystemExit(
        f"quarantine gate {gate} is stale: expected ignored failures "
        f"{expected!r}, observed {ignored!r}"
    )
print(
    f"quarantine gate {gate} confirmed failures: "
    + (", ".join(ignored) or "<none>")
)
QUARANTINE_SUMMARY_PY
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
    focused|focused-cython|focused-no-compat)
        run_focused "$1"
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
