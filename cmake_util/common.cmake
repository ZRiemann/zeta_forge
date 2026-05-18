cmake_minimum_required(VERSION 3.15)
# use ALL_PROXY=socks5://xxxxx:9090

option(ENABLE_LTO "Enable Link Time Optimization (IPO)" ON)
option(ENABLE_THIN_LTO "Prefer ThinLTO when supported (Clang/LLVM)" OFF)
option(ENABLE_FAT_LTO_OBJECTS "Build fat LTO objects (for library distribution)" OFF)

include(CheckIPOSupported)

if(ENABLE_LTO)
  check_ipo_supported(RESULT have_ipo OUTPUT ipo_err)
  if(have_ipo)
    set(CMAKE_INTERPROCEDURAL_OPTIMIZATION_RELEASE ON)
    set(CMAKE_INTERPROCEDURAL_OPTIMIZATION_RELWITHDEBINFO ON)
    message(STATUS "LTO/IPO enabled via CMake property")
  else()
    message(WARNING "IPO not supported: ${ipo_err}")
    set(ENABLE_LTO_FALLBACK_FLAGS ON)
  endif()
else()
    message(WARNING "LTO NOT ENABLED")
endif()

# Require at least C++20 for coroutine support (folly::coro requires C++20)
if(NOT DEFINED CMAKE_CXX_STANDARD OR "${CMAKE_CXX_STANDARD}" STREQUAL "")
  set(CMAKE_CXX_STANDARD 20)
endif()

if(CMAKE_CXX_STANDARD LESS 20)
  message(FATAL_ERROR "zpp requires at least C++20; got CMAKE_CXX_STANDARD=${CMAKE_CXX_STANDARD}")
endif()

set(CMAKE_CXX_STANDARD_REQUIRED ON)
set(CMAKE_CXX_EXTENSIONS OFF)

# config c++ flags
add_library(cxx_options INTERFACE)
target_compile_features(cxx_options INTERFACE cxx_std_${CMAKE_CXX_STANDARD})

set(gcc_like_cxx $<COMPILE_LANG_AND_ID:CXX,ARMClang,AppleClang,Clang,GNU,LCC>)
set(msvc_cxx $<COMPILE_LANG_AND_ID:CXX,MSVC>)
target_compile_options(cxx_options INTERFACE
                       $<${gcc_like_cxx}:$<BUILD_INTERFACE:-Wall>> #;-Wextra;-Wshadow;-Wformat=2;-Wunused;-Wno-class-memaccess;-Wno-deprecated-declarations>>
                       $<${msvc_cxx}:$<BUILD_INTERFACE:/W3>>
                       $<$<AND:${gcc_like_cxx},$<CONFIG:Debug>>:$<BUILD_INTERFACE:-O0;-g3;-fno-omit-frame-pointer>>
                      )
# strip 缩小Release版本程序的体积
if(CMAKE_BUILD_TYPE STREQUAL "Release")
  set(CMAKE_EXE_LINKER_FLAGS_RELEASE "${CMAKE_EXE_LINKER_FLAGS_RELEASE} -s")
  set(CMAKE_SHARED_LINKER_FLAGS_RELEASE "${CMAKE_SHARED_LINKER_FLAGS_RELEASE} -s")
endif()

# zeta_forge builders pass CMAKE_PREFIX_PATH explicitly. For manual CMake
# invocations, prefer the configured forge install prefix if it is available.
if(DEFINED ENV{ZETA_INSTALL_PREFIX} AND NOT "$ENV{ZETA_INSTALL_PREFIX}" STREQUAL "")
  list(PREPEND CMAKE_PREFIX_PATH "$ENV{ZETA_INSTALL_PREFIX}")
endif()

# spdlog 输出相对路径
add_compile_options(-fmacro-prefix-map=${CMAKE_SOURCE_DIR}= -ffile-prefix-map=${CMAKE_SOURCE_DIR}=)

# config c++ definitions
string(TIMESTAMP COMPILE_TIME %Y-%m-%d_%H:%M:%S)
set(build_time ${COMPILE_TIME})
message(STATUS "COMPILE_TIME: ${COMPILE_TIME}")

find_package(Git QUIET)
set(GIT_VERSION_STRING "unknown")
set(GIT_COMMIT_HASH "unknown")
set(GIT_BRANCH "unknown")
set(GIT_DIRTY "")

if(GIT_FOUND)
    execute_process(
    COMMAND "${GIT_EXECUTABLE}" rev-parse --is-inside-work-tree
    WORKING_DIRECTORY "${CMAKE_SOURCE_DIR}"
    RESULT_VARIABLE _inside_repo_res
    OUTPUT_VARIABLE _inside_repo_out
    ERROR_QUIET
    OUTPUT_STRIP_TRAILING_WHITESPACE
  )
  if(_inside_repo_res EQUAL 0 AND _inside_repo_out STREQUAL "true")
    # 1) 优先用 git describe（需要 tag 存在），--always 确保无 tag 时也有输出
    execute_process(
      COMMAND "${GIT_EXECUTABLE}" describe --tags --dirty --always --abbrev=7
      WORKING_DIRECTORY "${CMAKE_SOURCE_DIR}"
      RESULT_VARIABLE _desc_res
      OUTPUT_VARIABLE _desc_out
      ERROR_QUIET
      OUTPUT_STRIP_TRAILING_WHITESPACE
    )
    if(_desc_res EQUAL 0 AND NOT _desc_out STREQUAL "")
      set(GIT_VERSION_STRING "${_desc_out}")
    endif()

    # 2) 获取短哈希
    execute_process(
      COMMAND "${GIT_EXECUTABLE}" rev-parse --short=7 HEAD
      WORKING_DIRECTORY "${CMAKE_SOURCE_DIR}"
      RESULT_VARIABLE _hash_res
      OUTPUT_VARIABLE _hash_out
      ERROR_QUIET
      OUTPUT_STRIP_TRAILING_WHITESPACE
    )
    if(_hash_res EQUAL 0 AND NOT _hash_out STREQUAL "")
      set(GIT_COMMIT_HASH "${_hash_out}")
    endif()

    # 3) 当前分支名（在某些 CI 上可能是 HEAD 或 detached）
    execute_process(
      COMMAND "${GIT_EXECUTABLE}" rev-parse --abbrev-ref HEAD
      WORKING_DIRECTORY "${CMAKE_SOURCE_DIR}"
      RESULT_VARIABLE _branch_res
      OUTPUT_VARIABLE _branch_out
      ERROR_QUIET
      OUTPUT_STRIP_TRAILING_WHITESPACE
    )
    if(_branch_res EQUAL 0 AND NOT _branch_out STREQUAL "")
      set(GIT_BRANCH "${_branch_out}")
    endif()

    # 4) 是否有未提交修改（dirty 标记）
    execute_process(
      COMMAND "${GIT_EXECUTABLE}" diff --quiet
      WORKING_DIRECTORY "${CMAKE_SOURCE_DIR}"
      RESULT_VARIABLE _diff_res
      ERROR_QUIET
    )
    if(NOT _diff_res EQUAL 0)
      set(GIT_DIRTY "-dirty")
    endif()
  endif()
else()
    message(WARNING "Git not found!")
endif()
# 最终的版本字符串，示例：v1.2.3-4-gabc1234-dirty 或 abc1234-dirty
if(GIT_VERSION_STRING STREQUAL "unknown" AND NOT GIT_COMMIT_HASH STREQUAL "unknown")
  set(GIT_VERSION_STRING "${GIT_COMMIT_HASH}")
endif()
if(NOT GIT_DIRTY STREQUAL "")
  set(GIT_VERSION_STRING "${GIT_VERSION_STRING}${GIT_DIRTY}")
endif()



# 检查文件是否存在
set(CONFIG_TEMPLATE "${CMAKE_CURRENT_SOURCE_DIR}/CMakeConfig.h.in")
if(EXISTS "${CONFIG_TEMPLATE}")
    message(STATUS "Found configuration template: ${CONFIG_TEMPLATE}")
    # 文件存在，使用 configure_file 命令
    configure_file(
        "${CONFIG_TEMPLATE}"
        "${CMAKE_CURRENT_BINARY_DIR}/CMakeConfig.h"
        @ONLY  # 只替换 @VAR@ 格式的变量
    )
    # 可选：将生成的头文件目录添加到包含路径
    include_directories("${CMAKE_CURRENT_BINARY_DIR}")
else()
    message(STATUS "Configuration template not found: ${CONFIG_TEMPLATE}")
    # 可选：处理文件不存在的情况
endif()

#list(APPEND CMAKE_MODULE_PATH "${CMAKE_CURRENT_SOURCE_DIR}/cmake")

# Provide helper to create run_<target> custom targets
include(${CMAKE_CURRENT_LIST_DIR}/add_run_target.cmake)
