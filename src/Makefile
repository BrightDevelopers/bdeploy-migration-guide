PYTHON ?= python3
SOURCE_DIR := $(dir $(abspath $(lastword $(MAKEFILE_LIST))))
RUN := cd $(SOURCE_DIR) && PYTHONDONTWRITEBYTECODE=1 $(PYTHON)

.DEFAULT_GOAL := help
.PHONY: help build test run-tests live-check lifecycle clean

help:
	@echo "Targets:"
	@echo "  build       byte-compile the client and tests (syntax check)"
	@echo "  test        run the offline unit tests against the in-process fake server"
	@echo "  run-tests   build, then test"
	@echo "  live-check  read-only check against BSN.cloud (needs BS_CLIENT_ID, BS_SECRET, BS_NETWORK)"
	@echo "  lifecycle   live check, then create/update/delete a throwaway setup and device (also needs BS_USERNAME)"
	@echo "  clean       remove Python caches"

build:
	$(RUN) -m compileall -q bdeploy tests

test:
	$(RUN) -m unittest discover -s tests -t . -v

run-tests: build test

live-check:
	$(RUN) -m bdeploy check

lifecycle:
	$(RUN) -m bdeploy lifecycle

clean:
	find $(SOURCE_DIR) -name __pycache__ -type d -prune -exec rm -rf {} +
