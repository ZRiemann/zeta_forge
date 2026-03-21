#!/bin/bash
set -euo pipefail

script_dir="$(cd "$(dirname "$0")" && pwd)"
python_entry="$script_dir/prepare_debian.py"

if command -v python3 >/dev/null 2>&1; then
	exec python3 "$python_entry" "$@"
fi

if ! command -v apt-get >/dev/null 2>&1; then
	echo "python3 is not installed and apt-get is unavailable" >&2
	exit 1
fi

if command -v sudo >/dev/null 2>&1; then
	SUDO="sudo"
else
	SUDO=""
fi

echo "python3 not found; bootstrapping python3 with apt-get" >&2
$SUDO apt-get update
DEBIAN_FRONTEND=noninteractive $SUDO apt-get install -y python3

exec python3 "$python_entry" "$@"
