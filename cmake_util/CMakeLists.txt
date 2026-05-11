cmake_minimum_required(VERSION 3.15)

project(zeta_cmake_util VERSION 0.2.0 LANGUAGES CXX)

include("${CMAKE_CURRENT_LIST_DIR}/common.cmake")

# Append User define options
#target_compile_options(cxx_options INTERFACE
#    $<${gcc_like_cxx}:$<BUILD_INTERFACE:-Wno-class-memaccess;-Wno-deprecated-declarations>>
#)

message(STATUS "zeta_forge cmake_util helper modules are available")


if(WIN32)
    message(STATUS "System: Windows")
elseif(APPLE)
    message(STATUS "System: macOS")
elseif(UNIX)
    #set(CMAKE_BUILD_WITH_INSTALL_RPATH TRUE)
    message(STATUS "System: Unix")
endif()
