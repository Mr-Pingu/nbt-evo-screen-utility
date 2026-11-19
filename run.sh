#!/bin/sh
set -eu

cd "$(dirname "$0")"

python_cmd=
for candidate in python3 python; do
    if command -v "$candidate" >/dev/null 2>&1 &&
       "$candidate" -c 'import sys; raise SystemExit(sys.version_info < (3, 9))' >/dev/null 2>&1; then
        python_cmd=$candidate
        break
    fi
done
if [ -z "$python_cmd" ]; then
    printf '%s\n' 'Python 3.9 or newer is required.' >&2
    exit 1
fi

if [ ! -x .venv/bin/python ]; then
    "$python_cmd" -m venv .venv
fi
if ! .venv/bin/python -c 'import tkinter' >/dev/null 2>&1; then
    printf '%s\n' 'Tkinter is required. On Linux, install your distribution package (often python3-tk).' >&2
    exit 1
fi

if ! .venv/bin/python -c 'import paramiko, PIL' >/dev/null 2>&1; then
    printf '%s\n' 'Installing missing Python dependencies from requirements.txt...'
    if ! .venv/bin/python -m pip install --disable-pip-version-check -r requirements.txt; then
        printf '%s\n' 'Dependency installation failed. Check your internet connection and try again.' >&2
        exit 1
    fi
fi
exec .venv/bin/python hu_stream.py "$@"
