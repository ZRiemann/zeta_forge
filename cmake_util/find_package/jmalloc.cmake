# 查找 jemalloc 库
find_library(JEMALLOC_LIBRARY
    NAMES jemalloc
    PATHS /usr/lib /usr/local/lib /usr/lib/x86_64-linux-gnu
)

# 查找 jemalloc 头文件
find_path(JEMALLOC_INCLUDE_DIR
    NAMES jemalloc/jemalloc.h
    PATHS /usr/include /usr/local/include
)

# 检查是否找到
if(JEMALLOC_LIBRARY AND JEMALLOC_INCLUDE_DIR)
    message(STATUS "Found jemalloc:")
    message(STATUS "  - Library: ${JEMALLOC_LIBRARY}")
    message(STATUS "  - Include: ${JEMALLOC_INCLUDE_DIR}")
    
    # 设置包含目录和库
    set(JEMALLOC_INCLUDE_DIRS ${JEMALLOC_INCLUDE_DIR})
    set(JEMALLOC_LIBS ${JEMALLOC_LIBRARY})
else()
    message(FATAL_ERROR "jemalloc not found. Please install libjemalloc-dev package")
endif()

# 添加可执行文件
#add_executable(myapp main.c)
#target_include_directories(myapp PRIVATE ${JEMALLOC_INCLUDE_DIRS})
#target_link_libraries(myapp PRIVATE ${JEMALLOC_LIBRARIES})

# handle the QUIET and REQUIRED arguments and set DL_FOUND to TRUE
# if all listed variables are TRUE
# Note: capitalisation of the package name must be the same as in the file name
find_package_handle_standard_args(Dl DEFAULT_MSG DL_LIBRARIES )

if(JEMALLOC_LIBRARY AND JEMALLOC_INCLUDE_DIR)
    message(STATUS "Found jemalloc: ${Jemalloc_LIBRARIES}")
else()
    message(STATUS "jemalloc not found. Attempting to install...")
        
    execute_process(
        COMMAND sudo apt install -y libjemalloc-dev
        RESULT_VARIABLE INSTALL_RESULT
        OUTPUT_VARIABLE INSTALL_OUTPUT
        ERROR_VARIABLE INSTALL_ERROR
    )

    if(NOT INSTALL_RESULT EQUAL 0)
        message(WARNING "Failed to install libjemalloc-dev: ${INSTALL_ERROR}")
    else()
        message(STATUS "Successfully installed libjemalloc-dev")
        find_package(Jemalloc REQUIRED)
    endif()
endif()

#target_link_libraries(myapp PRIVATE ${Jemalloc_LIBRARIES})
