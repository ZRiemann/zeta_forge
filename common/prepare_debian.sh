#!/bin/bash
set -euo pipefail

script_dir="$(cd "$(dirname "$0")" && pwd)"
python_entry="$script_dir/prepare_debian.py"
bootstrap_script="${ZSA_BOOTSTRAP_SCRIPT:-}"

if [[ -z "$bootstrap_script" ]]; then
	candidate_bootstrap="$script_dir/../../zsa/util/bootstrap_debian.sh"
	if [[ -f "$candidate_bootstrap" ]]; then
		bootstrap_script="$candidate_bootstrap"
	fi
fi

run_bootstrap() {
	if [[ -z "$bootstrap_script" ]]; then
		return 1
	fi
	echo "python3 not found; delegating host bootstrap to $bootstrap_script --minimal" >&2
	bash "$bootstrap_script" --minimal
}

if command -v python3 >/dev/null 2>&1; then
	exec python3 "$python_entry" "$@"
fi

if ! run_bootstrap; then
	echo "python3 is unavailable and no zsa bootstrap script was found." >&2
	echo "Run ../zsa/util/bootstrap_debian.sh --minimal first, or set ZSA_BOOTSTRAP_SCRIPT to the bootstrap_debian.sh path." >&2
	exit 1
fi

if ! command -v python3 >/dev/null 2>&1; then
	echo "python3 is still unavailable after running the zsa bootstrap script" >&2
	exit 1
fi

exec python3 "$python_entry" "$@"
