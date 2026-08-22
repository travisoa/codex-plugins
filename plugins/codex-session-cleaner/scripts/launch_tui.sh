#!/bin/sh
set -eu

PLUGIN_ROOT=$(CDPATH= cd -- "$(dirname -- "$0")/.." && pwd)
exec /usr/bin/env python3 "$PLUGIN_ROOT/tui/session_tui.py" "$@"
