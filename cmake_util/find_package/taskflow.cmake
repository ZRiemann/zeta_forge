# 2) 如果没找到，尝试使用用户指定的 TASKFLOW_ROOT（纯头文件路径）
if (DEFINED TASKFLOW_ROOT)
  message(STATUS "Using Taskflow from TASKFLOW_ROOT: ${TASKFLOW_ROOT}")
  if (NOT TARGET Taskflow::Taskflow)
    add_library(Taskflow::Taskflow INTERFACE IMPORTED)
    set_target_properties(Taskflow::Taskflow PROPERTIES
      INTERFACE_INCLUDE_DIRECTORIES "${TASKFLOW_ROOT}"
    )
  endif()
  return()
endif()

find_package(Taskflow QUIET CONFIG)

if(NOT TARGET Taskflow::Taskflow)
  if(ZPP_USE_CONAN)
    message(FATAL_ERROR "Taskflow was not found in Conan mode")
  endif()

    message(STATUS "Taskflow not found, will download it")
    # 添加 Taskflow
    CPMAddPackage(
        NAME taskflow
        GITHUB_REPOSITORY taskflow/taskflow
        GIT_TAG v4.0.0 # 目前官方稳定版为 v3.7.0，如需 v4.0.0 可改为 master 或较新 tag
        GIT_SHALLOW TRUE
        DOWNLOAD_ONLY YES
    )

    if(taskflow_ADDED)
        if(NOT TARGET Taskflow::Taskflow)
            add_library(Taskflow::Taskflow INTERFACE IMPORTED)
            set_target_properties(Taskflow::Taskflow PROPERTIES
                INTERFACE_INCLUDE_DIRECTORIES "${taskflow_SOURCE_DIR}"
            )
        endif()
    endif()
endif()
