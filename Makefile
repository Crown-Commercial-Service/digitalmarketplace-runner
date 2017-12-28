SHELL := /bin/bash
VIRTUALENV_ROOT := $(shell [ -z $$VIRTUAL_ENV ] && echo $$(pwd)/venv || echo $$VIRTUAL_ENV)
.DEFAULT_GOAL := run

.PHONY: virtualenv
virtualenv:
	[ -z $$VIRTUAL_ENV ] && [ ! -d venv ] && python3 -m venv venv || true

.PHONY: install
install: virtualenv
	${VIRTUALENV_ROOT}/bin/pip install .

.PHONY: uninstall
uninstall:
	${VIRTUALENV_ROOT}/bin/pip uninstall .

.PHONY: download
download: install
	${VIRTUALENV_ROOT}/bin/python main.py --download

.PHONY: run
run:
	${VIRTUALENV_ROOT}/bin/python main.py ${ARGS}

.PHONY: all
run-all: install
	${VIRTUALENV_ROOT}/bin/python main.py --all ${ARGS}

.PHONY: nix
nix: install
	${VIRTUALENV_ROOT}/bin/python main.py --nix ${ARGS}

.PHONY: nix-all
nix-all: install
	${VIRTUALENV_ROOT}/bin/python main.py --nix --all ${ARGS}
