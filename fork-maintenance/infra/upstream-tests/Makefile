SHELL := /bin/bash
.NOTPARALLEL:

override PODMAN := podman
override LAB_ROOT := $(abspath $(CURDIR)/../..)
override PROJECT_ROOT := $(abspath $(LAB_ROOT)/..)
override SOURCE_HOST := $(PROJECT_ROOT)
override RUNNER_HOST := $(CURDIR)
override PRIVATE_STATE := $(RUNNER_HOST)/private_state.py
override JOB := $(RUNNER_HOST)/job.py
override STATE_ROOT := $(PROJECT_ROOT)/.artifacts/fork-maintenance/upstream-tests
override LOG_DIR := $(STATE_ROOT)/logs
override SOURCE_BUNDLE_ROOT := $(STATE_ROOT)/sources

SELECTION ?= stacks/develop
PATCH_MODE ?= patched
IMAGE_TAG_SUFFIX ?=
SOURCE_REMOTE ?= upstream
SOURCE_COMMIT ?=
VERIFY_LIVE_SOURCE ?= 1
export XPRA_LAB_SELECTION := $(SELECTION)
export XPRA_PATCH_MODE := $(PATCH_MODE)

override SOURCE_REF := refs/remotes/$(SOURCE_REMOTE)/master
override SOURCE_TIP_COMMIT := $(shell git -C "$(SOURCE_HOST)" rev-parse "$(SOURCE_REF)" 2>/dev/null)
override BASE_COMMIT := $(if $(strip $(SOURCE_COMMIT)),$(strip $(SOURCE_COMMIT)),$(SOURCE_TIP_COMMIT))
override SOURCE_SHORT := $(shell printf '%s' "$(BASE_COMMIT)" | cut -c1-12)
override SOURCE_BUNDLE := $(SOURCE_BUNDLE_ROOT)/$(SOURCE_TIP_COMMIT)-$(SOURCE_REMOTE).bundle
override WORKFLOW_SHA := $(shell git -C "$(SOURCE_HOST)" show "$(BASE_COMMIT):.github/workflows/test.yml" 2>/dev/null | sha256sum | awk '{print $$1}')
override IMAGE_INPUT_SHA := $(shell { sha256sum .containerignore Containerfile entrypoint.sh selection.py 2>/dev/null; printf '%s\n' "$(WORKFLOW_SHA)"; } | sha256sum | awk '{print $$1}')
override IMAGE_INPUT_SHORT := $(shell printf '%s' "$(IMAGE_INPUT_SHA)" | cut -c1-16)
override RUNNER_SHA := $(shell python3 "$(JOB)" runner-sha 2>/dev/null)
override IMAGE := localhost/xpra-lab-upstream-tests:ubuntu-26.04-$(IMAGE_INPUT_SHORT)$(IMAGE_TAG_SUFFIX)
override CCACHE_VOLUME := xpra-lab-upstream-ccache
override OWNER_ID := xpra-lab-upstream-tests

override SELECTION_NAME := $(subst /,-,$(SELECTION))
BACKGROUND_TARGET ?= full
BACKGROUND_NAME ?= $(SOURCE_SHORT)-$(SELECTION_NAME)-$(PATCH_MODE)-$(BACKGROUND_TARGET)
override BACKGROUND_RECORD_ROOT := $(STATE_ROOT)/runs
override BACKGROUND_RECORD := $(BACKGROUND_RECORD_ROOT)/$(BACKGROUND_NAME).owner
export XPRA_LAB_BACKGROUND_TARGET := $(BACKGROUND_TARGET)
export XPRA_LAB_BACKGROUND_NAME := $(BACKGROUND_NAME)

IMAGE_BACKGROUND_NAME ?=
override IMAGE_BUILD_ROOT := $(STATE_ROOT)/image-builds
override IMAGE_BUILD_CONTEXT := $(IMAGE_BUILD_ROOT)/$(IMAGE_BACKGROUND_NAME)
export XPRA_LAB_IMAGE_BACKGROUND_NAME := $(IMAGE_BACKGROUND_NAME)

override RUN_ARGS := \
	--userns keep-id:uid=1000,gid=1000 \
	--user 1000:1000 \
	--env XPRA_EXPECTED_SOURCE_COMMIT=$(BASE_COMMIT) \
	--env XPRA_EXPECTED_SOURCE_HEAD=$(SOURCE_TIP_COMMIT) \
	--env XPRA_EXPECTED_SOURCE_REF=$(SOURCE_REF) \
	--env XPRA_EXPECTED_WORKFLOW_SHA=$(WORKFLOW_SHA) \
	--env XPRA_LAB_SELECTION \
	--env XPRA_PATCH_MODE \
	--mount type=bind,src=$(SOURCE_BUNDLE),dst=/source.bundle,ro \
	--mount type=bind,src=$(LAB_ROOT)/cases,dst=/lab-host/cases,ro \
	--mount type=bind,src=$(LAB_ROOT)/stacks,dst=/lab-host/stacks,ro \
	--volume $(CCACHE_VOLUME):/home/ubuntu/.cache/ccache:U
override RUN := $(PODMAN) run --rm $(RUN_ARGS)
override RESOLVE_IMAGE = image_id=$$($(PODMAN) image inspect "$(IMAGE)" --format '{{.Id}}'); \
	[[ "$$image_id" =~ ^[0-9a-f]{64}$$ ]]; \
	test "$$($(PODMAN) image inspect "$$image_id" --format '{{index .Labels "io.xpra.lab.image-builder"}}')" = true; \
	test "$$($(PODMAN) image inspect "$$image_id" --format '{{index .Labels "io.xpra.lab.image-input"}}')" = "$(IMAGE_INPUT_SHA)"; \
	test "$$($(PODMAN) image inspect "$$image_id" --format '{{index .Labels "io.xpra.lab.workflow"}}')" = "$(WORKFLOW_SHA)"

.PHONY: help private-state-check private-state-tests inputs-check selection-check ci-policy-check \
	source-check source-check-remote source-snapshot \
	image image-check ci-image ci image-background-start image-background-name-check \
	image-background-status image-background-logs image-background-wait \
	image-background-collect image-background-remove image-background-abort image-remove \
	versions patch-check focused quarantine quarantine-cython quarantine-no-compat \
	wayland libyuv full full-cython full-no-compat run shell background-start background-name-check \
	background-status background-logs background-wait background-collect \
	background-remove background-abort

help:
	@printf '%s\n' \
		'SELECTION=cases/<id> selects its case.toml and fix.patch' \
		'SELECTION=stacks/<id> selects stacks/<id>.toml and its case series' \
		'PATCH_MODE=patched applies all paths; tests-only applies retained tests; clean applies none' \
		'make source-check-remote  Verify the selected cached master against its live remote' \
		'make source-snapshot      Freeze the selected source history as a credential-free Git bundle' \
		'make image                Verify the content-tagged Ubuntu 26.04 image' \
		'make ci XPRA_CI_TARGET=full' \
		'                          Apply stacks/develop and run one selected CI test leg' \
		'make image-background-start IMAGE_BACKGROUND_NAME=unique-name' \
		'    Build a missing image as a durable owned process job' \
		'make image-background-{status,logs,wait,collect,remove,abort} IMAGE_BACKGROUND_NAME=name' \
		'make image-remove            Remove only the exact verified lab image' \
		'make patch-check          Apply and whitespace-check the selection' \
		'make focused              Build and run the selected regression tests' \
		'make quarantine           Confirm declared failures on clean baseline' \
		'make quarantine-cython    Confirm declared failures on clean Cython build' \
		'make quarantine-no-compat Confirm declared failures without compatibility' \
		'make wayland              Build and run native Wayland checks' \
		'make libyuv               Build, link-check, and smoke-test libyuv' \
		'make full                 Run the interpreted upstream workflow leg' \
		'make full-cython          Run the Cython-enabled upstream workflow leg' \
		'make full-no-compat       Run the no-backwards-compatibility workflow leg' \
		'make background-start BACKGROUND_TARGET=full BACKGROUND_NAME=name' \
		'make background-{status,logs,wait,collect,remove} BACKGROUND_NAME=name' \
		'make background-abort BACKGROUND_NAME=name' \
		'    Force-stop and remove only that owned unfinished container job' \
		'XPRA_TEST_COMMAND="command" make run' \
		'make shell                Open a shell in a fresh patched source snapshot'

private-state-check:
	@python3 "$(PRIVATE_STATE)" --project-root "$(PROJECT_ROOT)"

private-state-tests:
	@cd "$(RUNNER_HOST)" && python3 -m unittest -v test_private_state.py

source-snapshot image-remove image-background-start image-background-collect \
	image-background-remove image-background-abort background-start background-collect \
	background-remove background-abort: \
	private-state-check

inputs-check:
	@command -v "$(PODMAN)" >/dev/null 2>&1 || { \
		printf 'Podman is required by the upstream-test runner\n' >&2; exit 2; \
	}
	@case "$(SOURCE_REMOTE)" in upstream|origin) ;; \
		*) printf 'invalid SOURCE_REMOTE: %s\n' "$(SOURCE_REMOTE)" >&2; exit 2 ;; \
	esac
	@case "$(VERIFY_LIVE_SOURCE)" in 0|1) ;; \
		*) printf 'invalid VERIFY_LIVE_SOURCE: %s\n' "$(VERIFY_LIVE_SOURCE)" >&2; exit 2 ;; \
	esac
	@[[ "$(IMAGE_TAG_SUFFIX)" =~ ^(-[a-z0-9]+(-[a-z0-9]+)*)?$$ ]] || { \
		printf 'invalid IMAGE_TAG_SUFFIX: %s\n' "$(IMAGE_TAG_SUFFIX)" >&2; exit 2; \
	}
	@for input in .containerignore Containerfile entrypoint.sh selection.py; do \
		path="$(RUNNER_HOST)/$$input"; \
		test ! -L "$$path" && test -f "$$path" || { \
			printf 'runner input is missing or is a symlink: %s\n' "$$path" >&2; exit 2; \
		}; \
	done
	@for input in Makefile private_state.py job.py ../../tools/background_job.py; do \
		path="$(RUNNER_HOST)/$$input"; \
		test ! -L "$$path" && test -f "$$path" || { \
			printf 'host runner input is missing or is a symlink: %s\n' "$$path" >&2; exit 2; \
		}; \
	done
	@actual=$$(python3 "$(JOB)" runner-sha); \
		[[ "$(RUNNER_SHA)" =~ ^[0-9a-f]{64}$$ ]] && test "$$actual" = "$(RUNNER_SHA)"
	@case "$${XPRA_PATCH_MODE}" in clean|tests-only|patched) ;; \
		*) printf 'invalid PATCH_MODE: %s\n' "$${XPRA_PATCH_MODE}" >&2; exit 2 ;; \
	esac

selection-check: inputs-check
	@python3 "$(RUNNER_HOST)/selection.py" \
		--lab-root "$(LAB_ROOT)" \
		--selection "$${XPRA_LAB_SELECTION}" \
		validate

source-check: inputs-check
	@test -d "$(SOURCE_HOST)/.git" || { printf 'missing Xpra fork checkout: %s\n' "$(SOURCE_HOST)" >&2; exit 2; }
	@[[ "$(SOURCE_TIP_COMMIT)" =~ ^[0-9a-f]{40}$$ ]] || { printf 'invalid cached %s/master commit: %s\n' "$(SOURCE_REMOTE)" "$(SOURCE_TIP_COMMIT)" >&2; exit 2; }
	@[[ "$(BASE_COMMIT)" =~ ^[0-9a-f]{40}$$ ]] || { printf 'invalid selected source commit: %s\n' "$(BASE_COMMIT)" >&2; exit 2; }
	@[[ "$(WORKFLOW_SHA)" =~ ^[0-9a-f]{64}$$ ]] || { printf 'invalid workflow digest: %s\n' "$(WORKFLOW_SHA)" >&2; exit 2; }
	@actual="$$(git -C "$(SOURCE_HOST)" remote get-url "$(SOURCE_REMOTE)")"; \
		actual="$${actual%/}"; actual="$${actual%.git}"; \
		case "$(SOURCE_REMOTE):$$actual" in \
			origin:https://github.com/kogeler/xpra|upstream:https://github.com/Xpra-org/xpra) ;; \
			*) printf '%s has unexpected URL: %s\n' "$(SOURCE_REMOTE)" "$$actual" >&2; exit 2 ;; \
		esac
	@git -C "$(SOURCE_HOST)" cat-file -e "$(BASE_COMMIT)^{commit}"
	@git -C "$(SOURCE_HOST)" cat-file -e "$(BASE_COMMIT):.github/workflows/test.yml"
	@git -C "$(SOURCE_HOST)" archive --format=tar "$(BASE_COMMIT)" >/dev/null
	@test "$$(git -C "$(SOURCE_HOST)" rev-parse "$(SOURCE_REF)")" = "$(SOURCE_TIP_COMMIT)"
	@git -C "$(SOURCE_HOST)" merge-base --is-ancestor "$(BASE_COMMIT)" "$(SOURCE_TIP_COMMIT)"
	@test "$$(git -C "$(SOURCE_HOST)" show "$(BASE_COMMIT):.github/workflows/test.yml" | sha256sum | awk '{print $$1}')" = "$(WORKFLOW_SHA)"

source-check-remote: source-check
	@if test "$(VERIFY_LIVE_SOURCE)" = 1; then \
		remote=$$(git -C "$(SOURCE_HOST)" ls-remote --heads "$(SOURCE_REMOTE)" refs/heads/master | awk '{print $$1}'); \
		test -n "$$remote" && test "$(SOURCE_TIP_COMMIT)" = "$$remote" || { \
			printf 'cached %s/master %s does not match live %s master %s\n' \
				"$(SOURCE_REMOTE)" "$(SOURCE_TIP_COMMIT)" "$(SOURCE_REMOTE)" "$$remote" >&2; \
			exit 2; \
		}; \
	fi

source-snapshot: source-check-remote
	@if test -e "$(SOURCE_BUNDLE)" || test -L "$(SOURCE_BUNDLE)"; then \
		test ! -L "$(SOURCE_BUNDLE)" && test -f "$(SOURCE_BUNDLE)" && test -O "$(SOURCE_BUNDLE)" || { \
			printf 'untrusted source bundle: %s\n' "$(SOURCE_BUNDLE)" >&2; exit 2; \
		}; \
		test "$$(stat -c '%a:%h' "$(SOURCE_BUNDLE)")" = 600:1 || { \
			printf 'unsafe source bundle mode or link count: %s\n' "$(SOURCE_BUNDLE)" >&2; exit 2; \
		}; \
	else \
		set -e; umask 077; \
		tmp_bundle=$$(mktemp "$(SOURCE_BUNDLE_ROOT)/.$(SOURCE_SHORT).XXXXXX"); \
		trap 'rm -f "$$tmp_bundle"' EXIT; \
		test "$$(git -C "$(SOURCE_HOST)" rev-parse "$(SOURCE_REF)")" = "$(SOURCE_TIP_COMMIT)"; \
		git -C "$(SOURCE_HOST)" bundle create "$$tmp_bundle" "$(SOURCE_REF)"; \
		chmod 0600 "$$tmp_bundle"; \
		git -C "$(SOURCE_HOST)" bundle verify "$$tmp_bundle" >/dev/null; \
		test "$$(git bundle list-heads "$$tmp_bundle")" = \
			"$(SOURCE_TIP_COMMIT) $(SOURCE_REF)"; \
		mv -T -n "$$tmp_bundle" "$(SOURCE_BUNDLE)"; \
		if test -e "$$tmp_bundle"; then rm -f "$$tmp_bundle"; fi; \
		trap - EXIT; \
	fi
	@git -C "$(SOURCE_HOST)" bundle verify "$(SOURCE_BUNDLE)" >/dev/null
	@test "$$(git bundle list-heads "$(SOURCE_BUNDLE)")" = \
		"$(SOURCE_TIP_COMMIT) $(SOURCE_REF)"

image image-check: source-check-remote inputs-check
	@$(PODMAN) image exists "$(IMAGE)" || { \
		printf 'required image is missing: %s\n' "$(IMAGE)" >&2; \
		printf '%s\n' 'run make image-background-start, then make image-background-wait' >&2; \
		exit 2; \
	}
	@builder=$$($(PODMAN) image inspect "$(IMAGE)" \
		--format '{{index .Labels "io.xpra.lab.image-builder"}}'); \
		actual=$$($(PODMAN) image inspect "$(IMAGE)" \
			--format '{{index .Labels "io.xpra.lab.image-input"}}'); \
		workflow=$$($(PODMAN) image inspect "$(IMAGE)" \
			--format '{{index .Labels "io.xpra.lab.workflow"}}'); \
		test "$$builder" = true || { \
			printf 'image builder label mismatch for %s\n' "$(IMAGE)" >&2; exit 2; \
		}; \
		test "$$actual" = "$(IMAGE_INPUT_SHA)" || { \
			printf 'image input label mismatch for %s\n' "$(IMAGE)" >&2; exit 2; \
		}; \
		test "$$workflow" = "$(WORKFLOW_SHA)" || { \
			printf 'image workflow label mismatch for %s\n' "$(IMAGE)" >&2; exit 2; \
		}

image-remove: image
	@image_id=$$($(PODMAN) image inspect "$(IMAGE)" --format '{{.Id}}'); \
		test -n "$$image_id"; \
		test "$$($(PODMAN) image inspect "$$image_id" --format '{{index .Labels "io.xpra.lab.image-builder"}}')" = true; \
		test "$$($(PODMAN) image inspect "$$image_id" --format '{{index .Labels "io.xpra.lab.image-input"}}')" = "$(IMAGE_INPUT_SHA)"; \
		test "$$($(PODMAN) image inspect "$$image_id" --format '{{index .Labels "io.xpra.lab.workflow"}}')" = "$(WORKFLOW_SHA)"; \
		$(PODMAN) image rm "$$image_id"

image-background-name-check:
	@[[ "$${XPRA_LAB_IMAGE_BACKGROUND_NAME}" =~ ^[A-Za-z0-9][A-Za-z0-9_.-]*$$ ]] || { \
		printf 'invalid image background name: %s\n' "$${XPRA_LAB_IMAGE_BACKGROUND_NAME}" >&2; exit 2; \
	}

image-background-start: source-check-remote inputs-check image-background-name-check
	@python3 "$(JOB)" image start "$${XPRA_LAB_IMAGE_BACKGROUND_NAME}" \
		--image "$(IMAGE)" --image-input-sha256 "$(IMAGE_INPUT_SHA)" \
		--source "$(BASE_COMMIT)" --workflow-sha256 "$(WORKFLOW_SHA)"

ci-image: source-check-remote inputs-check
	@python3 "$(JOB)" image ensure \
		--image "$(IMAGE)" --image-input-sha256 "$(IMAGE_INPUT_SHA)" \
		--source "$(BASE_COMMIT)" --workflow-sha256 "$(WORKFLOW_SHA)"

ci-policy-check: inputs-check
	@case "$${XPRA_CI_TARGET:-}" in \
		full|full-cython|full-no-compat) ;; \
		*) printf 'invalid CI target: %s\n' "$${XPRA_CI_TARGET:-}" >&2; exit 2 ;; \
	esac
	@test "$(SOURCE_REMOTE):$(VERIFY_LIVE_SOURCE)" = origin:0 || { \
		printf '%s\n' 'CI must use checkout origin/master without a live query' >&2; exit 2; \
	}
	@test -n "$(SOURCE_COMMIT)" || { \
		printf '%s\n' 'CI requires the source commit embedded in pushed develop' >&2; exit 2; \
	}
	@test "$${XPRA_LAB_SELECTION}" = stacks/develop || { \
		printf '%s\n' 'CI must run the complete stacks/develop queue' >&2; exit 2; \
	}
	@test "$${XPRA_PATCH_MODE}" = patched || { \
		printf '%s\n' 'CI must apply the complete patch queue' >&2; exit 2; \
	}
	@embedded="$$(git -C "$(SOURCE_HOST)" merge-base "$(SOURCE_REF)" HEAD)"; \
		test "$$embedded" = "$(BASE_COMMIT)" || { \
			printf 'CI source %s does not match pushed develop boundary %s\n' \
				"$(BASE_COMMIT)" "$$embedded" >&2; exit 2; \
		}

ci: ci-policy-check selection-check source-snapshot ci-image
	@set -e; target="$${XPRA_CI_TARGET}"; $(RESOLVE_IMAGE); \
		printf 'ci_target=%s\n' "$$target"; \
		$(RUN) "$$image_id" bash /opt/xpra-lab-upstream-tests/entrypoint.sh "$$target"

image-background-status image-background-logs image-background-wait \
image-background-collect image-background-remove image-background-abort: image-background-name-check
	@python3 "$(JOB)" image $(patsubst image-background-%,%,$@) \
		"$${XPRA_LAB_IMAGE_BACKGROUND_NAME}"

versions patch-check focused quarantine quarantine-cython quarantine-no-compat \
wayland libyuv full full-cython full-no-compat: selection-check source-snapshot image
	@set -e; $(RESOLVE_IMAGE); \
		$(RUN) "$$image_id" bash /opt/xpra-lab-upstream-tests/entrypoint.sh $@

run: selection-check source-snapshot image
	@test -n "$${XPRA_TEST_COMMAND:-}" || { \
		printf '%s\n' 'usage: XPRA_TEST_COMMAND="command" make run' >&2; exit 2; \
	}
	@set -e; $(RESOLVE_IMAGE); \
		$(PODMAN) run --rm $(RUN_ARGS) --env XPRA_TEST_COMMAND "$$image_id" \
			bash /opt/xpra-lab-upstream-tests/entrypoint.sh run

shell: selection-check source-snapshot image
	@set -e; $(RESOLVE_IMAGE); \
		$(PODMAN) run --rm -it $(RUN_ARGS) "$$image_id" \
			bash /opt/xpra-lab-upstream-tests/entrypoint.sh shell

background-start: selection-check source-snapshot image
	@test "$(BASE_COMMIT)" = "$(SOURCE_TIP_COMMIT)" || { \
		printf '%s\n' 'named local test jobs require the selected master tip' >&2; exit 2; \
	}
	@python3 "$(JOB)" test start "$${XPRA_LAB_BACKGROUND_NAME}" \
		--target "$${XPRA_LAB_BACKGROUND_TARGET}" \
		--selection "$${XPRA_LAB_SELECTION}" --patch-mode "$${XPRA_PATCH_MODE}" \
		--image "$(IMAGE)" --image-input-sha256 "$(IMAGE_INPUT_SHA)" \
		--source "$(BASE_COMMIT)" --source-remote "$(SOURCE_REMOTE)" \
		--workflow-sha256 "$(WORKFLOW_SHA)"

background-name-check:
	@[[ "$${XPRA_LAB_BACKGROUND_NAME}" =~ ^[A-Za-z0-9][A-Za-z0-9_.-]*$$ ]] || { \
		printf 'invalid background name: %s\n' "$${XPRA_LAB_BACKGROUND_NAME}" >&2; exit 2; \
	}

background-status background-logs background-wait background-collect \
background-remove background-abort: background-name-check
	@python3 "$(JOB)" test $(patsubst background-%,%,$@) \
		"$${XPRA_LAB_BACKGROUND_NAME}"
