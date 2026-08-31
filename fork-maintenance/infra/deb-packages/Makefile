SHELL := /bin/bash
.SHELLFLAGS := -euo pipefail -c

override RUNNER_ROOT := $(CURDIR)
override MAINTENANCE_ROOT := $(abspath $(RUNNER_ROOT)/../..)
override PROJECT_ROOT := $(abspath $(MAINTENANCE_ROOT)/..)
override JOB := $(RUNNER_ROOT)/job.py
override STATE_ROOT := $(PROJECT_ROOT)/.artifacts/fork-maintenance/deb-packages

DISTRO ?=
RUN ?=
SELECTION ?= stacks/develop
override OUTPUT := $(STATE_ROOT)/outputs/$(RUN)-$(DISTRO)-debs.tar
override CONTAINER_NAME := xpra-deb-$(RUN)

.PHONY: help check source-snapshot inputs-check run-name-check start status logs wait collect remove abort ci-release

help:
	@printf '%s\n' \
		'make start RUN=unique DISTRO=ubuntu-26.04' \
		'make start RUN=unique DISTRO=debian-13' \
		'make {status,logs,wait,collect,remove,abort} RUN=unique' \
		'    Freeze the HEAD/master boundary without depending on the current branch name'

check:
	@python3 -m py_compile builder.py job.py
	@PYTHONPATH="$(MAINTENANCE_ROOT)/tools" python3 -m unittest -v test_builder.py test_job.py

source-snapshot:
	@python3 "$(JOB)" source-snapshot

inputs-check:
	@case "$(DISTRO)" in ubuntu-26.04|debian-13) ;; \
		*) printf 'DISTRO must be ubuntu-26.04 or debian-13\n' >&2; exit 2 ;; \
	esac

run-name-check:
	@[[ "$(RUN)" =~ ^[A-Za-z0-9][A-Za-z0-9_.-]*$$ ]] || { \
		printf 'RUN must be a unique package job name\n' >&2; exit 2; \
	}

start: inputs-check run-name-check
	@python3 "$(JOB)" host-check
	@state="$$(python3 "$(JOB)" source-snapshot)"; \
	python3 "$(JOB)" start "$(RUN)" \
		--container-name "$(CONTAINER_NAME)" \
		--distro "$(DISTRO)" --output "$(OUTPUT)" \
		--selection "$(SELECTION)" \
		--source-state "$$state"

status logs wait collect remove abort: run-name-check
	@python3 "$(JOB)" $@ "$(RUN)"

ci-release:
	@python3 "$(JOB)" ci-release \
		--selection "$(SELECTION)"
