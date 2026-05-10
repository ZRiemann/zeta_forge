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

candidate_bootstrap="${ZSA_BOOTSTRAP_SCRIPT:-$script_dir/../../zsa/util/bootstrap_debian.sh}"

echo "python3 is unavailable; zeta_forge cannot run prepare_debian.py yet." >&2
echo "Please prepare a minimal Debian/Ubuntu build environment first, including:" >&2
echo "  python3 python3-dev curl git build-essential cmake ninja-build pkg-config" >&2

if [[ -f "$candidate_bootstrap" ]]; then
	echo "Optional: a sibling zsa bootstrap script was found; you may run:" >&2
	echo "  $candidate_bootstrap --minimal" >&2
fi

exit 1
