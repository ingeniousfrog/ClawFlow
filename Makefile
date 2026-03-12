PYTHON ?= python3

.PHONY: test

test:
	PYTHON="$(PYTHON)" sh scripts/test.sh

