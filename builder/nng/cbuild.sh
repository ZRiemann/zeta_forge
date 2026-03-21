#!/bin/bash
set -e

build_type="Release"
do_install=0
do_rebuild=0

for arg in "$@"; do
	case "$arg" in
		--BUILD_TYPE=*)
			build_type="${arg#*=}"
			;;
		--install)
			do_install=1
			;;
		--rebuild)
			do_rebuild=1
			;;
		*)
			echo "Unknown argument: $arg" >&2
			exit 2
			;;
	esac
done

case "$build_type" in
	Release|Debug)
		;;
	*)
		echo "Unsupported BUILD_TYPE: $build_type (use Release or Debug)" >&2
		exit 2
		;;
esac

script_dir="$(cd "$(dirname "$0")" && pwd)"
shared_config="$script_dir/../../common/build.env"
build_root="$script_dir/build"
build_dir="$build_root/$build_type"
configure_stamp="$build_dir/.configure.stamp"

if [[ -f "$shared_config" ]]; then
	source "$shared_config"
fi

cxx_standard="${ZETA_CXX_STANDARD:-20}"
install_prefix="${ZETA_INSTALL_PREFIX:-$HOME/.local}"
source_dir="${ZETA_NNG_SRC_DIR}"
moved_build_dir=0

if [[ -f "$build_dir/CMakeCache.txt" ]]; then
	cached_build_dir="$(grep '^CMAKE_CACHEFILE_DIR:INTERNAL=' "$build_dir/CMakeCache.txt" | cut -d= -f2- || true)"
	if [[ -n "$cached_build_dir" && "$cached_build_dir" != "$build_dir" ]]; then
		moved_build_dir=1
	fi
fi

if [[ ! -d "$source_dir" ]]; then
	echo "NNG source directory not found: $source_dir" >&2
	echo "Set ZETA_NNG_SRC_DIR to a local checkout or initialize the submodule with: git submodule update --init --recursive 3rd_party/nng" >&2
	exit 2
fi

if [[ $do_rebuild -eq 1 ]]; then
	rm -rf "$build_dir"
fi

mkdir -p "$build_dir"

if [[ $moved_build_dir -eq 1 ]]; then
	rm -f "$configure_stamp"
	rm -f "$build_dir/CMakeCache.txt"
	rm -rf "$build_dir/CMakeFiles"
fi

if [[ $do_rebuild -eq 1 ]]; then
	rm -f "$build_dir/CMakeCache.txt"
	rm -rf "$build_dir/CMakeFiles"
fi

needs_configure=0
if [[ $do_rebuild -eq 1 ]] || [[ ! -f "$configure_stamp" ]]; then
	needs_configure=1
elif find "$source_dir" -path "$source_dir/build" -prune -o -type f \( -name CMakeLists.txt -o -name '*.cmake' -o -name '*.cmake.in' -o -name '*.h' -o -name '*.c' \) -newer "$configure_stamp" -print -quit | grep -q .; then
	needs_configure=1
fi

if [[ $needs_configure -eq 1 ]]; then
	cmake -S "$source_dir" -B "$build_dir" \
		-G Ninja \
		-Wno-dev \
		-DCMAKE_BUILD_TYPE="$build_type" \
		-DCMAKE_INSTALL_PREFIX="$install_prefix" \
		-DCMAKE_CXX_STANDARD="$cxx_standard" \
		-DBUILD_SHARED_LIBS=OFF \
		-DNNG_TESTS=OFF \
		-DNNG_TOOLS=OFF \
		-DNNG_ENABLE_NNGCAT=OFF \
		-DNNG_ENABLE_TLS=OFF \
		-DNNG_ENABLE_HTTP=ON
	touch "$configure_stamp"
fi

cmake --build "$build_dir" -j"$(nproc)"

if [[ $do_install -eq 1 ]]; then
	cmake --install "$build_dir"
fi