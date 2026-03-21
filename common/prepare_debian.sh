#!/bin/bash
set -euo pipefail

script_dir="$(cd "$(dirname "$0")" && pwd)"
repo_root="$(cd "$script_dir/.." && pwd)"

if [[ -r "$script_dir/build.env" ]]; then
	source "$script_dir/build.env"
fi

if [[ ! -f /etc/os-release ]]; then
	echo "Unsupported system: /etc/os-release not found" >&2
	exit 1
fi

source /etc/os-release

distribution_id="${ID:-}"
distribution_like="${ID_LIKE:-}"

if [[ "$distribution_id" != "debian" && "$distribution_id" != "ubuntu" && ! " $distribution_like " =~ [[:space:]]debian[[:space:]] ]]; then
	echo "This script currently supports Debian-family distributions with apt" >&2
	exit 1
fi

if ! command -v apt-get >/dev/null 2>&1; then
	echo "Unsupported system: apt-get not found" >&2
	exit 1
fi

if command -v sudo >/dev/null 2>&1; then
	SUDO="sudo"
else
	SUDO=""
fi

apt_install() {
	DEBIAN_FRONTEND=noninteractive $SUDO apt-get install -y "$@"
}

ensure_user_local_bin_on_path() {
	local shell_rc
	for shell_rc in "$HOME/.profile" "$HOME/.bashrc"; do
		if [[ ! -f "$shell_rc" ]]; then
			touch "$shell_rc"
		fi

		if ! grep -Fq 'export PATH="$HOME/.local/bin:$PATH"' "$shell_rc"; then
			echo '' >> "$shell_rc"
			echo 'export PATH="$HOME/.local/bin:$PATH"' >> "$shell_rc"
		fi
	done

	export PATH="$HOME/.local/bin:$PATH"
}

install_or_upgrade_uv() {
	local uv_cmd="${HOME}/.local/bin/uv"

	if [[ -x "$uv_cmd" ]]; then
		"$uv_cmd" self update
	else
		curl -LsSf https://astral.sh/uv/install.sh | sh
	fi

	if [[ ! -x "$uv_cmd" ]]; then
		echo "uv installation failed" >&2
		exit 1
	fi
}

install_or_upgrade_conan() {
	local python_version="${ZETA_PYTHON_VERSION:-3.12}"
	local uv_cmd="${HOME}/.local/bin/uv"
	local conan_cmd="${HOME}/.local/bin/conan"
	local conan_version=""

	"$uv_cmd" python install "$python_version"
	"$uv_cmd" tool uninstall conan >/dev/null 2>&1 || true
	"$uv_cmd" tool install --python "$python_version" "conan>=2,<3"

	if [[ -x "$conan_cmd" ]]; then
		conan_version="$($conan_cmd --version | awk '{print $3}')"
		echo "Using Conan ${conan_version} with Python ${python_version}"
	else
		echo "Conan installation failed" >&2
		exit 1
	fi
	
	if ! $conan_cmd profile path default >/dev/null 2>&1; then
		$conan_cmd profile detect --force >/dev/null 2>&1 || true
	fi
}

update_submodules() {
	if [[ -d "$repo_root/.git" ]] || git -C "$repo_root" rev-parse --git-dir >/dev/null 2>&1; then
		git -C "$repo_root" submodule update --init --recursive
	fi
}

echo "==> Updating apt package index"
$SUDO apt-get update

echo "==> Installing base command-line tools"
apt_install \
	ca-certificates \
	curl \
	git \
	unzip \
	zip \
	tar \
	xz-utils \
	file \
	patch \
	patchelf \
	software-properties-common

echo "==> Installing build toolchain"
apt_install \
	build-essential \
	cmake \
	ninja-build \
	pkg-config \
	ccache \
	gdb

echo "==> Installing source-build helpers for Conan dependencies"
apt_install \
	autoconf \
	automake \
	libtool \
	m4 \
	perl \
	nasm \
	bison \
	flex

echo "==> Installing Python tooling"
apt_install \
	python3 \
	python3-dev

echo "==> Ensuring ~/.local/bin is on PATH"
ensure_user_local_bin_on_path

echo "==> Installing uv"
install_or_upgrade_uv

echo "==> Installing Conan 2 through uv"
install_or_upgrade_conan

echo "==> Initializing repository submodules"
update_submodules

cat <<EOF

Environment preparation completed.

Installed toolchain layers:
1. Base tools: git, curl, archive utilities
2. Native build tools: gcc/g++, cmake, ninja, pkg-config
3. Source-build helpers: autotools, perl, nasm, flex, bison
4. Python toolchain: python3, uv, Conan 2

Suggested next steps:
1. source "$HOME/.profile"
2. cd "$repo_root"
3. ./builder/hpx/cbuild.sh --rebuild --install
4. ./builder/folly/cbuild.sh --rebuild --install
5. ./builder/abseil-cpp/cbuild.sh --rebuild --install
6. ./builder/nng/cbuild.sh --rebuild --install
7. ./builder/zpp/cbuild.sh --rebuild

EOF
