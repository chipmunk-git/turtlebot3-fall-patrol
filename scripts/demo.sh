#!/usr/bin/env bash
set -e
exec /usr/bin/python3 "$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)/demo_launcher.py" "$@"
