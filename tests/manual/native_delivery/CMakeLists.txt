cmake_minimum_required(VERSION 3.24)
project(ForgeNativeConsumers LANGUAGES CXX)

enable_testing()
option(FORGE_FULL_NATIVE "Also link consumers of gRPC, HPX and Folly" ON)

function(forge_consumer name dependency)
    add_executable(${name}-consumer consumer.cpp)
    string(TOUPPER "${name}" probe)
    target_compile_definitions(${name}-consumer PRIVATE "FORGE_PROBE_${probe}")
    target_compile_features(${name}-consumer PRIVATE cxx_std_20)
    target_link_libraries(${name}-consumer PRIVATE ${dependency})
    add_test(NAME ${name}-consumer COMMAND ${name}-consumer)
endfunction()

# Resolve Conan OpenSSL before Boost, since the gRPC stage also ships a
# BoringSSL config under the OpenSSL package name.
find_file(_forge_openssl_config OpenSSLConfig.cmake
    PATHS ${CMAKE_PREFIX_PATH} PATH_SUFFIXES lib/cmake/zeta_deps/Release
    NO_DEFAULT_PATH NO_CACHE REQUIRED)
get_filename_component(OpenSSL_DIR "${_forge_openssl_config}" DIRECTORY)
find_package(OpenSSL CONFIG REQUIRED)
find_package(Boost CONFIG REQUIRED)
find_package(nng CONFIG REQUIRED)
forge_consumer(cobalt Boost::cobalt_io_ssl)
forge_consumer(nng nng::nng)

if(FORGE_FULL_NATIVE)
    # Match Forge's gRPC and HPX consumer setup for the pinned packages.
    find_package(Protobuf CONFIG REQUIRED)
    find_package(gRPC CONFIG REQUIRED)
    set(CMAKE_FIND_PACKAGE_PREFER_CONFIG ON)
    set(_hpx_packages "${CMAKE_CURRENT_SOURCE_DIR}/../../../builder/hpx/cmake/conan_compat/packages")
    set(Asio_DIR "${_hpx_packages}")
    set(Hwloc_DIR "${_hpx_packages}")
    set(Jemalloc_DIR "${_hpx_packages}")
    find_package(HPX CONFIG REQUIRED)
    find_package(LibLZMA CONFIG REQUIRED)
    find_package(folly CONFIG REQUIRED)
    forge_consumer(grpc gRPC::grpc++)
    forge_consumer(hpx HPX::hpx)
    forge_consumer(folly Folly::folly)
endif()
