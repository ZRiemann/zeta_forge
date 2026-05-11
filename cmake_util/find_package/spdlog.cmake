# 2) 如果没找到，尝试使用用户指定的 spdlog_ROOT（纯头文件路径）
if (DEFINED SPDLOG_ROOT)
  message(STATUS "Using spdlog from SPDLOG_ROOT: ${SPDLOG_ROOT}")
  if (NOT TARGET spdlog::spdlog)
    add_library(spdlog::spdlog INTERFACE IMPORTED)
    set_target_properties(spdlog::spdlog PROPERTIES
      INTERFACE_INCLUDE_DIRECTORIES "${SPDLOG_ROOT}"
    )
  endif()
  return()
endif()

  find_package(spdlog QUIET CONFIG)

  if(TARGET spdlog::spdlog_header_only AND NOT TARGET spdlog::spdlog)
    add_library(spdlog::spdlog INTERFACE IMPORTED)
    set_target_properties(spdlog::spdlog PROPERTIES
      INTERFACE_LINK_LIBRARIES spdlog::spdlog_header_only
    )
  endif()

# 检查是否找到
if(spdlog_FOUND)
    message(STATUS "spdlog found: ${spdlog_DIR}")
else()
    if(ZPP_USE_CONAN)
      message(FATAL_ERROR "spdlog was not found in Conan mode")
    endif()

    message(STATUS "spdlog not found, will download it")

    CPMAddPackage(
        NAME spdlog
        URL https://github.com/gabime/spdlog/archive/refs/tags/v1.15.3.zip
        URL_HASH SHA256=b74274c32c8be5dba70b7006c1d41b7d3e5ff0dff8390c8b6390c1189424e094
        DOWNLOAD_ONLY YES
    )
    #[[
    CPMAddPackage(
        NAME spdlog
        GITHUB_REPOSITORY gabime/spdlog
        GIT_TAG v1.15.3
        GIT_SHALLOW TRUE
        GIT_PROGRESS TRUE
        DOWNLOAD_ONLY YES  # 仅下载，不构建（因为它是仅头文件库）
    )
    ]]
    set(SPDLOG_HEADER_ONLY ON CACHE BOOL "" FORCE)

    add_library(spdlog::spdlog INTERFACE IMPORTED)
    set_target_properties(spdlog::spdlog PROPERTIES
        INTERFACE_INCLUDE_DIRECTORIES "${spdlog_SOURCE_DIR}/include"
    )
    #target_link_libraries(your_target spdlog::spdlog)
endif()

# 用法示例：
# if(spdlog_ADDED) # CPM 会定义此变量
#   target_link_libraries(myapp PRIVATE spdlog::spdlog)
#   可选：若你想用 header-only 模式且包支持该选项（通常 link 目标不变
#   target_compile_definitions(myapp PRIVATE SPDLOG_HEADER_ONLY)
# endif()