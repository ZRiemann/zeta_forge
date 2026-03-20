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
user_toolchain="$script_dir/cmake/conan_user_toolchain.cmake"
conan_generators_dir="$build_dir/conan/build/$build_type/generators"
conan_stamp="$build_dir/.conan.stamp"
configure_stamp="$build_dir/.configure.stamp"

if [[ -f "$shared_config" ]]; then
  source "$shared_config"
fi

cxx_standard="${ZETA_CXX_STANDARD:-20}"
source_dir="${ZETA_HPX_SRC_DIR}"

if [[ ! -d "$source_dir" ]]; then
  echo "HPX source directory not found: $source_dir" >&2
  echo "Set ZETA_HPX_SRC_DIR to a local checkout or initialize the submodule with: git submodule update --init --recursive 3rd_party/hpx" >&2
  exit 2
fi

if [[ $do_rebuild -eq 1 ]]; then
  rm -rf "$build_dir"
fi

mkdir -p "$build_dir"

if [[ $do_rebuild -eq 1 ]] || [[ ! -f "$conan_stamp" ]] || [[ "$script_dir/conanfile.py" -nt "$conan_stamp" ]] || [[ "$user_toolchain" -nt "$conan_stamp" ]]; then
  conan profile detect --force || true

  conan install "$script_dir/conanfile.py" \
    --output-folder="$build_dir/conan" \
    --build=missing \
    -s build_type="$build_type" \
    -s compiler.cppstd="$cxx_standard" \
    -c tools.cmake.cmaketoolchain:generator=Ninja \
    -c "tools.cmake.cmaketoolchain:user_toolchain=[\"$user_toolchain\"]" \
    -c 'tools.cmake.cmaketoolchain:enabled_blocks=["user_toolchain", "generic_system", "compilers", "android_system", "apple_system", "fpic", "arch_flags", "linker_scripts", "rpath_link_flags", "libcxx", "vs_runtime", "vs_debugger_environment", "parallel", "extra_flags", "cmake_flags_init", "extra_variables", "try_compile", "find_paths", "pkg_config", "rpath", "shared", "output_dirs", "variables", "preprocessor"]'
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
    -DCMAKE_BUILD_TYPE="$build_type" \
    -DCMAKE_TOOLCHAIN_FILE="$conan_generators_dir/conan_toolchain.cmake" \
    -DHPX_WITH_MALLOC=jemalloc \
    -DHPX_WITH_NETWORKING=ON \
    -DHPX_WITH_PARCELPORT_MPI=ON \
    -DCMAKE_INSTALL_PREFIX="$HOME/.local" \
    -DHPX_WITH_FETCH_ASIO=OFF \
    -DHPX_WITH_FETCH_BOOST=OFF \
    -DHPX_WITH_FETCH_HWLOC=OFF \
    -DHPX_WITH_PKGCONFIG=OFF \
    -DBUILD_SHARED_LIBS=OFF \
    -DHPX_WITH_STATIC_LINKING=ON \
    -DHPX_WITH_CXX_STANDARD="$cxx_standard"
  touch "$configure_stamp"
fi

cmake --build "$build_dir" -j"$(nproc)"

if [[ $do_install -eq 1 ]]; then
  cmake --install "$build_dir"
fi