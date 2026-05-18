message(WARNING "Legacy CPM fallback module: nng.cmake is kept for old projects only. Modern ZetaX projects should use zeta_forge managed package configs.")

find_package(nng QUIET CONFIG)
if(NOT TARGET nng::nng)
    find_package(NNG QUIET)
endif()

#sudo apt update
#sudo apt install libmbedtls-dev
#sudo apt install libnng-dev
#dpkg -l | grep mbedtls
#[[
# 尝试查找 MbedTLS
find_package(MbedTLS QUIET)

# 如果没有找到，尝试安装
if(NOT MbedTLS_FOUND)
    message(STATUS "MbedTLS not found. Attempting to install...")
    
    # 执行安装命令
    execute_process(
        COMMAND sudo apt install -y libmbedtls-dev
        RESULT_VARIABLE INSTALL_RESULT
        OUTPUT_VARIABLE INSTALL_OUTPUT
        ERROR_VARIABLE INSTALL_ERROR
    )
    
    # 检查安装结果
    if(NOT INSTALL_RESULT EQUAL 0)
        message(WARNING "Failed to install libmbedtls-dev: ${INSTALL_ERROR}")
    else()
        message(STATUS "Successfully installed libmbedtls-dev")
        # 重新尝试查找包
        find_package(MbedTLS REQUIRED)
    endif()
else()
    message(STATUS "Found MbedTLS: ${MbedTLS_VERSION}")
endif()
]]
if(NOT TARGET nng::nng)
    message(STATUS "NNG not found, will download and build it")
    CPMAddPackage(
        NAME nng
        GITHUB_REPOSITORY nanomsg/nng
        VERSION 2.0.0-alpha.6
        #VERSION main
        GIT_SHALLOW TRUE
        GIT_PROGRESS TRUE
        OPTIONS
            "NNG_TESTS OFF"
            "NNG_TOOLS OFF"
            "NNG_ENABLE_NNGCAT OFF"
            "BUILD_SHARED_LIBS OFF"
            "NNG_ENABLE_TLS OFF"
            "NNG_ENABLE_HTTP ON"
    )
    
    message(STATUS "NNG found: ${nng_SOURCE_DIR}")        
    # Create an alias target that matches the expected name
    if(NOT TARGET nng::nng)
        add_library(nng::nng ALIAS nng)
    endif()
else()
    if(DEFINED NNG_LIBRARIES AND NNG_LIBRARIES)
        message(STATUS "Found NNG (libs): ${NNG_LIBRARIES}")
    elseif(TARGET nng::nng)
        message(STATUS "Found NNG as imported target: nng::nng")
        # 向后兼容：把变量设为 target 名称（下游最好改用 target）
        set(NNG_LIBRARIES nng::nng CACHE STRING "NNG libraries (target)")
    else()
        message(STATUS "Found NNG but no NNG_LIBRARIES and no nng::nng target")
    endif()
    message(STATUS "Found NNG: ${NNG_LIBRARIES}")
endif()

# Determine a usable version string (support several variable namings)
if(DEFINED NNG_VERSION)
    set(_nng_version "${NNG_VERSION}")
elseif(DEFINED nng_VERSION)
    set(_nng_version "${nng_VERSION}")
elseif(DEFINED nng_VERSION)
    set(_nng_version "${nng_VERSION}")
else()
    set(_nng_version "<unknown>")
endif()

# If the package exposes an imported target, prefer that and provide
# a backward-compatible `NNG_LIBRARIES` variable for old CMakeLists.
if(TARGET nng::nng)
    if(NOT (DEFINED NNG_LIBRARIES AND NNG_LIBRARIES))
        set(NNG_LIBRARIES nng::nng CACHE STRING "NNG libraries (target)")
    endif()
    message(STATUS "Found NNG as imported target: nng::nng")
endif()

message(STATUS "Using nng version: ${_nng_version}")
message(STATUS "NNG_LIBRARIES variable: ${NNG_LIBRARIES}")
#[[
# demo
add_executable(nng_example src/main.c)
target_link_libraries(nng_example PRIVATE nng::nng)

# 如果需要，设置包含目录
target_include_directories(nng_example PRIVATE ${nng_SOURCE_DIR}/include)

# 设置 C 标准
set_target_properties(nng_example PROPERTIES
    C_STANDARD 99
    C_STANDARD_REQUIRED ON
)
]]
