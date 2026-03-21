#!/bin/bash
set -e

build_type="Release"
do_install=0
do_rebuild=0
build_tests=1
build_examples=1
build_hpx_examples=0

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
		--no-tests)
			build_tests=0
			;;
		--no-examples)
			build_examples=0
			;;
		--with-hpx-examples)
			build_hpx_examples=1
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

if [[ $build_examples -eq 0 && $build_hpx_examples -eq 1 ]]; then
	echo "--with-hpx-examples requires examples to be enabled" >&2
	exit 2
fi

script_dir="$(cd "$(dirname "$0")" && pwd)"
shared_config="$script_dir/../../common/build.env"
build_root="$script_dir/build"
build_dir="$build_root/$build_type"
conan_generators_dir="$build_dir/conan/build/$build_type/generators"
conan_stamp="$build_dir/.conan.stamp"
configure_stamp="$build_dir/.configure.stamp"

if [[ -f "$shared_config" ]]; then
	source "$shared_config"
fi

cxx_standard="${ZETA_CXX_STANDARD:-20}"
install_prefix="${ZETA_INSTALL_PREFIX:-$HOME/.local}"
builder_root_dir="${ZETA_BUILDER_DIR:-$(cd "$script_dir/.." && pwd)}"
folly_conan_generators_dir="$builder_root_dir/folly/build/$build_type/conan/build/$build_type/generators"
folly_prefix_dir="$install_prefix"
folly_cmake_dir="$folly_prefix_dir/lib/cmake/folly"
source_dir="${ZETA_ZPP_SRC_DIR}"
taskflow_source_dir="${ZETA_TASKFLOW_SRC_DIR}"
rapidjson_source_dir="${ZETA_RAPIDJSON_SRC_DIR}"
cmake_build_tests="OFF"
cmake_build_examples="OFF"
cmake_build_hpx_examples="OFF"
moved_build_dir=0

if [[ -f "$build_dir/CMakeCache.txt" ]]; then
	cached_build_dir="$(grep '^CMAKE_CACHEFILE_DIR:INTERNAL=' "$build_dir/CMakeCache.txt" | cut -d= -f2- || true)"
	if [[ -n "$cached_build_dir" && "$cached_build_dir" != "$build_dir" ]]; then
		moved_build_dir=1
	fi
fi

if [[ ! -d "$source_dir" ]]; then
	echo "zpp source directory not found: $source_dir" >&2
	echo "Set ZETA_ZPP_SRC_DIR to a local checkout or initialize the submodule with: git submodule update --init --recursive 3rd_party/zpp" >&2
	exit 2
fi

if [[ ! -d "$taskflow_source_dir" ]]; then
	echo "Taskflow source directory not found: $taskflow_source_dir" >&2
	echo "Set ZETA_TASKFLOW_SRC_DIR to a local checkout or initialize the submodule with: git submodule update --init --recursive 3rd_party/taskflow" >&2
	exit 2
fi

if [[ ! -d "$rapidjson_source_dir/include/rapidjson" ]]; then
	echo "RapidJSON source directory not found or invalid: $rapidjson_source_dir" >&2
	echo "Set ZETA_RAPIDJSON_SRC_DIR to a local checkout or initialize the submodule with: git submodule update --init --recursive 3rd_party/rapidjson" >&2
	exit 2
fi

if [[ $build_tests -eq 1 ]]; then
	cmake_build_tests="ON"
fi

if [[ $build_examples -eq 1 ]]; then
	cmake_build_examples="ON"
fi

if [[ $build_hpx_examples -eq 1 ]]; then
	cmake_build_hpx_examples="ON"
fi

if [[ $do_rebuild -eq 1 ]]; then
	rm -rf "$build_dir"
fi

mkdir -p "$build_dir"

if [[ $moved_build_dir -eq 1 ]]; then
	rm -rf "$build_dir/conan"
	rm -f "$conan_stamp" "$configure_stamp"
	rm -f "$build_dir/CMakeCache.txt"
	rm -rf "$build_dir/CMakeFiles"
fi

if [[ $do_rebuild -eq 1 ]] || [[ ! -f "$conan_stamp" ]] || [[ "$script_dir/conanfile.py" -nt "$conan_stamp" ]]; then
	conan profile detect --force || true
	rm -rf "$build_dir/conan"

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
elif find "$source_dir" -path "$source_dir/build" -prune -o -path "$source_dir/build_debug" -prune -o -type f \( -name CMakeLists.txt -o -name '*.cmake' -o -name '*.cmake.in' \) -newer "$configure_stamp" -print -quit | grep -q .; then
	needs_configure=1
fi

if [[ $needs_configure -eq 1 ]]; then
	cmake -S "$source_dir" -B "$build_dir" \
		-G Ninja \
		-Wno-dev \
		-DCMAKE_BUILD_TYPE="$build_type" \
		-DCMAKE_TOOLCHAIN_FILE="$conan_generators_dir/conan_toolchain.cmake" \
		-DCMAKE_PREFIX_PATH="$folly_prefix_dir;$folly_conan_generators_dir" \
		-DCMAKE_INSTALL_PREFIX="$install_prefix" \
		-DCMAKE_CXX_STANDARD="$cxx_standard" \
		-Dfolly_DIR="$folly_cmake_dir" \
		-DRAPIDJSON_ROOT="$rapidjson_source_dir/include" \
		-DTASKFLOW_ROOT="$taskflow_source_dir" \
		-DZPP_USE_CONAN=ON \
		-DZPP_BUILD_TESTS="$cmake_build_tests" \
		-DZPP_BUILD_EXAMPLES="$cmake_build_examples" \
		-DZPP_BUILD_HPX_EXAMPLES="$cmake_build_hpx_examples"
	touch "$configure_stamp"
fi

cmake --build "$build_dir" -j"$(nproc)"

if [[ $do_install -eq 1 ]]; then
	cmake --install "$build_dir"
fi
