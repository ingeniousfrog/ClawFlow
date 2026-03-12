#!/bin/sh
set -eu

can_run_python() {
    candidate="$1"
    [ -n "$candidate" ] || return 1
    if [ -x "$candidate" ]; then
        return 0
    fi
    command -v "$candidate" >/dev/null 2>&1
}

python_has_pytest() {
    candidate="$1"
    "$candidate" -c "import pytest" >/dev/null 2>&1
}

resolve_python() {
    for candidate in \
        "${PYTHON:-}" \
        "./.venv/bin/python" \
        "python3.12" \
        "python3" \
        "python"
    do
        if ! can_run_python "$candidate"; then
            continue
        fi
        if python_has_pytest "$candidate"; then
            printf '%s\n' "$candidate"
            return 0
        fi
    done
    return 1
}

PYTHON_BIN="$(resolve_python || true)"

if [ -z "$PYTHON_BIN" ]; then
    echo "No usable Python interpreter with pytest was found." >&2
    echo "Run 'pip install -e \".[dev]\"' in your env, or pass PYTHON=/path/to/python." >&2
    exit 1
fi

PYTHONPATH=. "$PYTHON_BIN" -m pytest -q "$@"
