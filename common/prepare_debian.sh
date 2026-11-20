#!/bin/bash
set -euo pipefail

script_path="${BASH_SOURCE[0]}"
case "$script_path" in
	*/*) script_dir="$(cd "${script_path%/*}" && pwd)" ;;
	*) script_dir="$(pwd)" ;;
esac
python_entry="$script_dir/prepare_debian.py"

if command -v python3 >/dev/null 2>&1; then
	exec python3 "$python_entry" "$@"
fi

echo "python3 is unavailable; zeta_forge cannot run prepare_debian.py yet." >&2
echo "Please prepare a minimal Debian/Ubuntu build environment first, including:" >&2
echo "  python3 python3-dev curl git build-essential cmake ninja-build pkg-config" >&2

exit 1
