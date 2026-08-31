SHELL := /bin/bash
.SHELLFLAGS := -euo pipefail -c
.DEFAULT_GOAL := help
.NOTPARALLEL:

override PYTHON := python3
AUTOMATION_ROOT := $(abspath $(CURDIR))
REPOSITORY_ROOT := $(abspath $(AUTOMATION_ROOT)/..)
CONTRIB := $(PYTHON) $(AUTOMATION_ROOT)/tools/contrib.py
LIVE_REQUIREMENTS := $(AUTOMATION_ROOT)/infra/live/requirements.txt
LIVE_VENV := $(shell $(PYTHON) $(AUTOMATION_ROOT)/infra/live/job.py environment-path)
LIVE_PYTHON := $(LIVE_VENV)/bin/python
LIVE_JOB_BOOTSTRAP := $(PYTHON) $(AUTOMATION_ROOT)/infra/live/job.py
LIVE_JOB := $(LIVE_PYTHON) $(AUTOMATION_ROOT)/infra/live/job.py
LIVE_CONFIG := $(PYTHON) $(AUTOMATION_ROOT)/infra/live/live_config.py
UPSTREAM_TESTS := $(AUTOMATION_ROOT)/infra/upstream-tests
DEB_PACKAGES := $(AUTOMATION_ROOT)/infra/deb-packages
RUFF ?= ruff

CASE ?=
STACK ?= $(if $(strip $(CASE)),,develop)
RUN ?=
IMAGE_RUN ?=
WORKSPACE ?=
CYCLE ?=
CONFIRM ?=
TARGET ?= focused
PATCH_MODE ?= patched
IMAGE_TAG_SUFFIX ?=
ENCODING ?= rgb
H264_CLIENT_POLICY ?= strict
ALPHA_SCENARIOS ?= default
APPLICATION ?= zed
LIFECYCLE ?= application-exit
NETWORK_PROFILE ?= $(shell $(LIVE_CONFIG) default-profile)
RENDER_NODE ?=
ZED_DIRECTORY ?=
ALLOW_PATH_CHANGE ?= 0

SELECTOR = $(if $(strip $(CASE)),cases/$(strip $(CASE)),$(if $(strip $(STACK)),stacks/$(strip $(STACK))))
export XPRA_FORK_CASE := $(CASE)
export XPRA_FORK_STACK := $(STACK)
export XPRA_FORK_RUN := $(RUN)
export XPRA_FORK_IMAGE_RUN := $(IMAGE_RUN)
export XPRA_FORK_TARGET := $(TARGET)
export XPRA_FORK_PATCH_MODE := $(PATCH_MODE)
export XPRA_FORK_ENCODING := $(ENCODING)
export XPRA_FORK_H264_CLIENT_POLICY := $(H264_CLIENT_POLICY)
export XPRA_FORK_ALPHA_SCENARIOS := $(ALPHA_SCENARIOS)
export XPRA_FORK_APPLICATION := $(APPLICATION)
export XPRA_FORK_LIFECYCLE := $(LIFECYCLE)
export XPRA_FORK_NETWORK_PROFILE := $(NETWORK_PROFILE)
export XPRA_FORK_RENDER_NODE := $(RENDER_NODE)
export XPRA_FORK_ZED_DIRECTORY := $(ZED_DIRECTORY)
export XPRA_FORK_SELECTOR := $(SELECTOR)

AUTOMATION_PYTHON := $(shell find tools infra cases -type f -name '*.py' -print 2>/dev/null | sort)
AUTOMATION_SHELL := infra/live/start_hardware_fixture.sh infra/live/start_zed.sh \
	infra/upstream-tests/entrypoint.sh

.PHONY: help check lint contributor-tests clean artifact-boundary doctor \
	live-venv live-venv-check list repo-status repo-sync master-update develop-rebase \
	patch-start-check isolated-start-check develop-check ci-layout-check ci-prepare ci-master-sync ci-upstream-tests ci-deb-release \
	case-new case-recover case-name-check stack-name-check selector-check workspace-selector-check optional-selector-check \
	patch-check patch-apply patch-unapply patch-update stack-check stack-apply stack-unapply \
	workspace-name-check workspace-create workspace-status workspace-diff workspace-stage \
	workspace-update workspace-remove workspace-recover \
	cycle-name-check cycle-clean-plan cycle-clean \
	run-name-check image-run-name-check test-target-check live-options-check \
	deb-policy-check \
	test-image test-image-start test-image-status test-image-logs test-image-wait \
	test-image-collect test-image-remove test-image-abort test-image-cache-remove test-versions \
	test-start test-status test-logs test-wait test-collect test-remove test-abort \
	test-patch-check test-focused test-quarantine test-quarantine-cython \
	test-quarantine-no-compat test-wayland test-libyuv \
	test-full test-full-cython test-full-no-compat \
	live-profile-list live-start live-status live-logs live-wait live-collect live-remove live-abort \
	live-rgb live-h264 live-xpra-detach live-xpra-transport-loss live-xpra-hardware \
	live-xpra-opengl-hardware \
	deb-start deb-status deb-logs deb-wait deb-collect deb-remove deb-abort

help:
	@printf '%s\n' \
		'make -C fork-maintenance check' \
		'    Offline static and unit checks for the in-repository automation' \
		'make -C fork-maintenance repo-sync' \
		'    Explicit upstream refresh: fetch and verify equal fork/canonical master refs' \
		'make -C fork-maintenance master-update' \
		'    Explicit upstream refresh: fast-forward local master to fetched origin/master' \
		'make -C fork-maintenance develop-rebase' \
		'    Explicit upstream refresh: rebase clean develop onto local master' \
		'make -C fork-maintenance patch-start-check' \
		'    Verify an explicitly selected sync/rebase cycle before host patch integration' \
		'make -C fork-maintenance isolated-start-check' \
		'    Locate the source commit already embedded in current develop; no network access' \
		'make -C fork-maintenance case-new CASE=short-behavior-name' \
		'make -C fork-maintenance case-recover CASE=short-behavior-name' \
		'    Create a draft case or recover exact interrupted create, update, or update-removal state' \
		'make -C fork-maintenance workspace-create CASE=name WORKSPACE=unique-name' \
		'    Export the embedded source and apply the case, or start a clean draft, below .artifacts/' \
		'make -C fork-maintenance workspace-{status,diff,stage,update,remove} WORKSPACE=name' \
		'    Inspect, refresh, or remove the exact isolated workspace' \
		'make -C fork-maintenance workspace-recover WORKSPACE=name' \
		'    Recover exact marker-backed interrupted create, direct removal, or fingerprint cleanup state' \
		'make -C fork-maintenance cycle-clean-plan CYCLE=cycle-name' \
		'make -C fork-maintenance cycle-clean CYCLE=cycle-name CONFIRM=printed-sha256' \
		'    Review, then remove exact finalized cycle results and workspaces' \
		'make -C fork-maintenance develop-check' \
		'    Verify clean linear develop, embedded source boundary, queue, and ignore boundary' \
		'make -C fork-maintenance ci-layout-check' \
		'    Verify byte-identical disabled upstream workflows and all thin fork workflows' \
		'make -C fork-maintenance ci-master-sync' \
		'    Hosted-only non-forced fast-forward of fork master from upstream master' \
		'make -C fork-maintenance ci-upstream-tests' \
		'    With XPRA_CI_TARGET=full|full-cython|full-no-compat, apply stacks/develop' \
		'    and run that one Ubuntu 26.04 unit-test leg' \
		'make -C fork-maintenance ci-deb-release' \
		'    Hosted manual workflow: build patched Ubuntu 26.04 and Debian 13 DEB tars and publish a release' \
		'make -C fork-maintenance patch-check CASE=name' \
		'make -C fork-maintenance stack-check STACK=develop' \
		'    Resolve one patch or the active queue against the embedded source' \
		'make -C fork-maintenance patch-apply CASE=name' \
		'make -C fork-maintenance patch-update CASE=name' \
		'make -C fork-maintenance patch-unapply CASE=name' \
		'    Apply to the current non-master branch, refresh from its staged diff, remove it' \
		'make -C fork-maintenance stack-apply STACK=develop' \
		'make -C fork-maintenance stack-unapply STACK=develop' \
		'    Apply or remove the complete queue in declared order' \
		'make -C fork-maintenance test-start STACK=develop TARGET=focused RUN=unique-name' \
		'make -C fork-maintenance test-wait RUN=unique-name' \
		'    Run a durable embedded-source container test job' \
		'make -C fork-maintenance test-abort RUN=unique-name' \
		'    Discard only that owned uncollected running/lost or stale completed test job' \
		'make -C fork-maintenance test-image-abort IMAGE_RUN=unique-name' \
		'    Discard an owned uncollected running/lost or stale completed image build' \
		'    IMAGE_TAG_SUFFIX isolates smoke builds' \
		'make -C fork-maintenance live-h264 STACK=develop RUN=unique-name' \
		'make -C fork-maintenance live-profile-list' \
		'    List tracked client network profiles; profiles.yml supplies the default' \
		'make -C fork-maintenance live-xpra-opengl-hardware STACK=develop RUN=unique-name' \
		'make -C fork-maintenance live-wait RUN=unique-name' \
		'make -C fork-maintenance live-abort RUN=unique-name' \
		'    Run or abort a durable physical-GPU profile; outputs stay below .artifacts/' \
		'make -C fork-maintenance deb-start DISTRO=ubuntu-26.04 RUN=unique-name' \
		'make -C fork-maintenance deb-{status,logs,wait,collect,remove,abort} RUN=unique-name' \
		'    Build one patched DEB tar as an owned mount-free background job'

check: artifact-boundary ci-layout-check
	@$(PYTHON) -m py_compile $(AUTOMATION_PYTHON)
	@bash -n $(AUTOMATION_SHELL)
	@for manifest in stacks/*.toml; do \
		test -e "$$manifest" || continue; \
		slug=$$(basename "$$manifest" .toml); \
		$(PYTHON) infra/upstream-tests/selection.py \
			--lab-root "$(AUTOMATION_ROOT)" --selection "stacks/$$slug" validate; \
	done
	@$(CONTRIB) case-list >/dev/null
	@$(PYTHON) tools/test_contrib.py
	@$(PYTHON) tools/test_background_job.py
	@PYTHONPATH="$(AUTOMATION_ROOT)/tools" $(PYTHON) tools/test_container_payload.py
	@PYTHONPATH="$(AUTOMATION_ROOT)/tools" $(PYTHON) tools/test_podman_policy.py
	@$(MAKE) --no-print-directory -C "$(DEB_PACKAGES)" check
	@$(PYTHON) -m unittest discover -s infra/live -p 'test_job.py'
	@$(PYTHON) -m unittest discover -s infra/upstream-tests -p 'test_*.py'
	@command -v "$(RUFF)" >/dev/null 2>&1 || { printf 'ruff is required for check\n' >&2; exit 2; }
	@$(RUFF) check tools infra cases

lint:
	@command -v "$(RUFF)" >/dev/null 2>&1 || { printf 'ruff is required for lint\n' >&2; exit 2; }
	@$(RUFF) check tools infra cases

contributor-tests:
	@$(PYTHON) tools/test_contrib.py

artifact-boundary:
	@$(CONTRIB) doctor >/dev/null

clean:
	@find tools infra cases -type f -path '*/__pycache__/*' -delete
	@find tools infra cases -depth -type d -name __pycache__ -empty -delete

doctor:
	@$(CONTRIB) doctor
	@$(LIVE_JOB_BOOTSTRAP) environment-check
	@$(LIVE_JOB) doctor

live-venv:
	@$(LIVE_JOB_BOOTSTRAP) environment-create

live-venv-check:
	@$(LIVE_JOB_BOOTSTRAP) environment-check

list:
	@$(CONTRIB) case-list

repo-status:
	@$(CONTRIB) repo-status

repo-sync:
	@$(CONTRIB) repo-sync

master-update:
	@$(CONTRIB) master-update

develop-rebase:
	@$(CONTRIB) develop-rebase

patch-start-check:
	@$(CONTRIB) patch-start-check

isolated-start-check:
	@$(CONTRIB) isolated-start-check

ci-layout-check:
	@$(CONTRIB) ci-layout-check

ci-prepare:
	@$(CONTRIB) ci-prepare

ci-master-sync:
	@$(CONTRIB) ci-master-sync

ci-upstream-tests:
	@preflight="$$( $(CONTRIB) ci-prepare )"; \
		printf '%s\n' "$$preflight"; \
		source_commit="$$(sed -n 's/^source_commit=//p' <<<"$$preflight")"; \
		[[ "$$source_commit" =~ ^[0-9a-f]{40}$$ ]]; \
		$(MAKE) --no-print-directory -C "$(UPSTREAM_TESTS)" ci \
			SELECTION=stacks/develop PATCH_MODE=patched \
			SOURCE_REMOTE=origin SOURCE_COMMIT="$$source_commit"

ci-deb-release:
	@$(CONTRIB) ci-deb-prepare
	@$(MAKE) --no-print-directory -C "$(DEB_PACKAGES)" ci-release \
		SELECTION=stacks/develop

develop-check:
	@$(CONTRIB) develop-check

case-name-check:
	@[[ "$${XPRA_FORK_CASE}" =~ ^[a-z0-9]+(-[a-z0-9]+)*$$ ]] || { \
		printf 'CASE must be a lowercase behavior slug\n' >&2; exit 2; \
	}

stack-name-check:
	@[[ "$${XPRA_FORK_STACK}" =~ ^[a-z0-9]+(-[a-z0-9]+)*$$ ]] || { \
		printf 'STACK must be a lowercase behavior slug\n' >&2; exit 2; \
	}

selector-check:
	@case "$${XPRA_FORK_CASE:+case}:$${XPRA_FORK_STACK:+stack}" in \
		case:|:stack) ;; \
		*) printf 'set exactly one of CASE or STACK\n' >&2; exit 2 ;; \
	esac
	@$(PYTHON) infra/upstream-tests/selection.py \
		--lab-root "$(AUTOMATION_ROOT)" --selection "$${XPRA_FORK_SELECTOR}" validate

workspace-selector-check:
	@case "$${XPRA_FORK_CASE:+case}:$${XPRA_FORK_STACK:+stack}" in \
		case:|:stack) ;; \
		*) printf 'set exactly one of CASE or STACK\n' >&2; exit 2 ;; \
	esac
	@[[ "$${XPRA_FORK_SELECTOR}" =~ ^(cases|stacks)/[a-z0-9]+(-[a-z0-9]+)*$$ ]] || { \
		printf 'invalid workspace selector: %s\n' "$${XPRA_FORK_SELECTOR}" >&2; exit 2; \
	}

optional-selector-check:
	@case "$${XPRA_FORK_CASE:+case}:$${XPRA_FORK_STACK:+stack}" in \
		:|case:|:stack) ;; \
		*) printf 'set at most one of CASE or STACK\n' >&2; exit 2 ;; \
	esac
	@if [[ -n "$${XPRA_FORK_SELECTOR}" ]]; then \
		$(PYTHON) infra/upstream-tests/selection.py \
			--lab-root "$(AUTOMATION_ROOT)" --selection "$${XPRA_FORK_SELECTOR}" validate; \
	fi

case-new: case-name-check
	@$(CONTRIB) case-new "$${XPRA_FORK_CASE}"

case-recover: case-name-check
	@$(CONTRIB) case-recover "$${XPRA_FORK_CASE}"

patch-check patch-apply patch-unapply: case-name-check
	@$(CONTRIB) $@ "$${XPRA_FORK_CASE}"

patch-update: case-name-check
	@set -- $(CONTRIB) patch-update "$${XPRA_FORK_CASE}"; \
	case "$(ALLOW_PATH_CHANGE)" in \
		0) ;; \
		1) set -- "$$@" --allow-path-change ;; \
		*) printf 'ALLOW_PATH_CHANGE must be 0 or 1\n' >&2; exit 2 ;; \
	esac; \
	"$$@"

stack-check stack-apply stack-unapply: stack-name-check
	@$(CONTRIB) $@ "$${XPRA_FORK_STACK}"

workspace-name-check:
	@[[ "$(WORKSPACE)" =~ ^[A-Za-z0-9][A-Za-z0-9_.-]*$$ ]] || { \
		printf 'WORKSPACE must contain only letters, digits, dot, underscore, and dash\n' >&2; exit 2; \
	}

workspace-create: workspace-selector-check workspace-name-check
	@$(CONTRIB) workspace-create "$(WORKSPACE)" "$${XPRA_FORK_SELECTOR}" \
		--patch-mode "$(PATCH_MODE)"

workspace-status workspace-diff workspace-remove workspace-recover: workspace-name-check
	@$(CONTRIB) $@ "$(WORKSPACE)"

workspace-stage workspace-update: workspace-name-check
	@set -- $(CONTRIB) $@ "$(WORKSPACE)"; \
	case "$(ALLOW_PATH_CHANGE)" in \
		0) ;; \
		1) set -- "$$@" --allow-path-change ;; \
		*) printf 'ALLOW_PATH_CHANGE must be 0 or 1\n' >&2; exit 2 ;; \
	esac; \
	"$$@"

cycle-name-check:
	@[[ "$(CYCLE)" =~ ^[a-z0-9]+(-[a-z0-9]+)*$$ ]] || { \
		printf 'CYCLE must use lowercase words separated by single hyphens\n' >&2; exit 2; \
	}

cycle-clean-plan: cycle-name-check
	@$(CONTRIB) cycle-clean-plan "$(CYCLE)"

cycle-clean: cycle-name-check
	@[[ "$(CONFIRM)" =~ ^[0-9a-f]{64}$$ ]] || { \
		printf 'CONFIRM must be the SHA-256 printed by cycle-clean-plan\n' >&2; exit 2; \
	}
	@$(CONTRIB) cycle-clean "$(CYCLE)" --confirm "$(CONFIRM)"

run-name-check:
	@[[ "$${XPRA_FORK_RUN}" =~ ^[A-Za-z0-9][A-Za-z0-9_.-]*$$ ]] || { \
		printf 'RUN must contain only letters, digits, dot, underscore, and dash\n' >&2; exit 2; \
	}

image-run-name-check:
	@[[ "$${XPRA_FORK_IMAGE_RUN}" =~ ^[A-Za-z0-9][A-Za-z0-9_.-]*$$ ]] || { \
		printf 'IMAGE_RUN must contain only letters, digits, dot, underscore, and dash\n' >&2; exit 2; \
	}

test-target-check:
	@case "$${XPRA_FORK_TARGET}" in \
		versions|patch-check|focused|quarantine|quarantine-cython|quarantine-no-compat|wayland|libyuv|full|full-cython|full-no-compat) ;; \
		*) printf 'unsupported TARGET: %s\n' "$${XPRA_FORK_TARGET}" >&2; exit 2 ;; \
	esac
	@case "$${XPRA_FORK_PATCH_MODE}" in clean|tests-only|patched) ;; \
		*) printf 'PATCH_MODE must be clean, tests-only, or patched\n' >&2; exit 2 ;; \
	esac

live-options-check:
	@$(PYTHON) infra/live/profiles.py \
		"$${XPRA_FORK_APPLICATION}" "$${XPRA_FORK_LIFECYCLE}" \
		"$${XPRA_FORK_ENCODING}" "$${XPRA_FORK_H264_CLIENT_POLICY}" \
		"$${XPRA_FORK_ALPHA_SCENARIOS}" "$${XPRA_FORK_NETWORK_PROFILE}"

live-profile-list:
	@$(LIVE_CONFIG) list-profiles

test-image:
	@$(MAKE) --no-print-directory -C "$(UPSTREAM_TESTS)" image \
		IMAGE_TAG_SUFFIX="$(IMAGE_TAG_SUFFIX)"

test-image-cache-remove:
	@$(MAKE) --no-print-directory -C "$(UPSTREAM_TESTS)" image-remove \
		IMAGE_TAG_SUFFIX="$(IMAGE_TAG_SUFFIX)"

test-image-start test-image-status test-image-logs test-image-wait test-image-collect \
test-image-remove test-image-abort: image-run-name-check
	@$(MAKE) --no-print-directory -C "$(UPSTREAM_TESTS)" \
		image-background-$(patsubst test-image-%,%,$@) \
		IMAGE_BACKGROUND_NAME="$${XPRA_FORK_IMAGE_RUN}" \
		IMAGE_TAG_SUFFIX="$(IMAGE_TAG_SUFFIX)"

test-versions:
	@$(MAKE) --no-print-directory -C "$(UPSTREAM_TESTS)" versions

test-start: isolated-start-check selector-check run-name-check test-target-check
	@$(MAKE) --no-print-directory -C "$(UPSTREAM_TESTS)" background-start \
		SELECTION="$${XPRA_FORK_SELECTOR}" PATCH_MODE="$${XPRA_FORK_PATCH_MODE}" \
		BACKGROUND_TARGET="$${XPRA_FORK_TARGET}" BACKGROUND_NAME="$${XPRA_FORK_RUN}"

test-status test-logs test-wait test-collect test-remove test-abort: run-name-check
	@$(MAKE) --no-print-directory -C "$(UPSTREAM_TESTS)" \
		background-$(patsubst test-%,%,$@) BACKGROUND_NAME="$${XPRA_FORK_RUN}"

test-patch-check test-focused test-quarantine test-quarantine-cython \
test-quarantine-no-compat test-wayland test-libyuv test-full test-full-cython \
test-full-no-compat:
	@$(MAKE) --no-print-directory test-start TARGET="$(patsubst test-%,%,$@)"

live-start: isolated-start-check selector-check run-name-check live-options-check
	@set -- $(LIVE_JOB) start "$${XPRA_FORK_RUN}" \
		--application "$${XPRA_FORK_APPLICATION}" \
		--encoding "$${XPRA_FORK_ENCODING}" \
		--h264-client-policy "$${XPRA_FORK_H264_CLIENT_POLICY}" \
		--lifecycle "$${XPRA_FORK_LIFECYCLE}" \
		--alpha-scenarios "$${XPRA_FORK_ALPHA_SCENARIOS}" \
		--network-profile "$${XPRA_FORK_NETWORK_PROFILE}" \
		--selection "$${XPRA_FORK_SELECTOR}"; \
	if [[ -n "$${XPRA_FORK_RENDER_NODE}" ]]; then set -- "$$@" --render-node "$${XPRA_FORK_RENDER_NODE}"; fi; \
	if [[ -n "$${XPRA_FORK_ZED_DIRECTORY}" ]]; then set -- "$$@" --zed-directory "$${XPRA_FORK_ZED_DIRECTORY}"; fi; \
	"$$@"

live-status live-logs live-wait live-collect live-remove live-abort: run-name-check
	@$(LIVE_JOB) $(patsubst live-%,%,$@) "$${XPRA_FORK_RUN}"

live-rgb:
	@$(MAKE) --no-print-directory live-start \
		APPLICATION=zed LIFECYCLE=application-exit ENCODING=rgb \
		H264_CLIENT_POLICY=strict ALPHA_SCENARIOS=default

live-h264:
	@$(MAKE) --no-print-directory live-start \
		APPLICATION=zed LIFECYCLE=application-exit ENCODING=h264 \
		H264_CLIENT_POLICY=adaptive-alpha ALPHA_SCENARIOS=default

live-xpra-detach:
	@$(MAKE) --no-print-directory live-start \
		APPLICATION=gtk LIFECYCLE=detach ENCODING=rgb \
		H264_CLIENT_POLICY=strict ALPHA_SCENARIOS=default

live-xpra-transport-loss:
	@$(MAKE) --no-print-directory live-start \
		APPLICATION=gtk LIFECYCLE=transport-loss ENCODING=rgb \
		H264_CLIENT_POLICY=strict ALPHA_SCENARIOS=default

live-xpra-hardware:
	@$(MAKE) --no-print-directory live-start \
		APPLICATION=hardware LIFECYCLE=application-exit ENCODING=h264 \
		H264_CLIENT_POLICY=adaptive-alpha ALPHA_SCENARIOS=default

live-xpra-opengl-hardware:
	@$(MAKE) --no-print-directory live-start \
		APPLICATION=opengl LIFECYCLE=application-exit ENCODING=h264 \
		H264_CLIENT_POLICY=adaptive-alpha ALPHA_SCENARIOS=default

deb-policy-check:
	@test -z "$(CASE)" && test "$(STACK)" = develop || { \
		printf '%s\n' 'DEB packages must apply the complete stacks/develop queue' >&2; \
		exit 2; \
	}

deb-start: deb-policy-check run-name-check
	@$(MAKE) --no-print-directory -C "$(DEB_PACKAGES)" start \
		RUN="$${XPRA_FORK_RUN}" DISTRO="$(DISTRO)" \
		SELECTION=stacks/develop

deb-status deb-logs deb-wait deb-collect deb-remove deb-abort: run-name-check
	@$(MAKE) --no-print-directory -C "$(DEB_PACKAGES)" \
		$(patsubst deb-%,%,$@) RUN="$${XPRA_FORK_RUN}"
