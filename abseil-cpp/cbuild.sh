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
shared_config="$script_dir/../common/build.env"
build_root="$script_dir/build"
build_dir="$build_root/$build_type"
conan_generators_dir="$build_dir/conan/build/$build_type/generators"
conan_stamp="$build_dir/.conan.stamp"
configure_stamp="$build_dir/.configure.stamp"

if [[ -f "$shared_config" ]]; then
	source "$shared_config"
fi

cxx_standard="${ZETA_CXX_STANDARD:-20}"
source_dir="${ZETA_ABSEIL_SRC_DIR}"

if [[ ! -d "$source_dir" ]]; then
	echo "Abseil source directory not found: $source_dir" >&2
	echo "Set ZETA_ABSEIL_SRC_DIR to a local checkout or initialize the submodule with: git submodule update --init --recursive 3rd_party/abseil-cpp" >&2
	exit 2
fi

if [[ $do_rebuild -eq 1 ]]; then
	rm -rf "$build_dir"
fi

mkdir -p "$build_dir"

if [[ $do_rebuild -eq 1 ]] || [[ ! -f "$conan_stamp" ]] || [[ "$script_dir/conanfile.py" -nt "$conan_stamp" ]]; then
	conan profile detect --force || true

	conan install "$script_dir/conanfile.py" \
		--output-folder="$build_dir/conan" \
		--build=missing \
		-s build_type="$build_type" \
		-s compiler.cppstd="$cxx_standard" \
		-c tools.cmake.cmaketoolchain:generator=Ninja
	touch "$conan_stamp"
fi

if [[ $do_rebuild -eq 1 ]]; then
	rm -f "$build_dir/CMakeCache.txt"
	rm -rf "$build_dir/CMakeFiles"
fi

needs_configure=0
if [[ $do_rebuild -eq 1 ]] || [[ ! -f "$configure_stamp" ]] || [[ "$conan_generators_dir/conan_toolchain.cmake" -nt "$configure_stamp" ]]; then
	needs_configure=1
elif find "$source_dir" -path "$source_dir/build" -prune -o -type f \( -name CMakeLists.txt -o -name '*.cmake' -o -name '*.cmake.in' \) -newer "$configure_stamp" -print -quit | grep -q .; then
	needs_configure=1
fi

if [[ $needs_configure -eq 1 ]]; then
	cmake -S "$source_dir" -B "$build_dir" \
		-G Ninja \
		-Wno-dev \
		-DCMAKE_BUILD_TYPE="$build_type" \
		-DCMAKE_TOOLCHAIN_FILE="$conan_generators_dir/conan_toolchain.cmake" \
		-DCMAKE_INSTALL_PREFIX="$HOME/.local" \
		-DCMAKE_CXX_STANDARD="$cxx_standard" \
		-DBUILD_TESTING=OFF \
		-DABSL_BUILD_TESTING=OFF \
		-DABSL_BUILD_TEST_HELPERS=OFF \
		-DABSL_USE_GOOGLETEST_HEAD=OFF \
		-DABSL_BUILD_MONOLITHIC_SHARED_LIBS=OFF
	touch "$configure_stamp"
fi

cmake --build "$build_dir" -j"$(nproc)"

if [[ $do_install -eq 1 ]]; then
	cmake --install "$build_dir"
fi