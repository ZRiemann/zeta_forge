# 2) 如果没找到，尝试使用用户指定的 RAPIDJSON_ROOT（纯头文件路径）
if (DEFINED RAPIDJSON_ROOT)
  message(STATUS "Using RapidJSON from RAPIDJSON_ROOT: ${RAPIDJSON_ROOT}")
  if (NOT TARGET rapidjson::rapidjson)
    add_library(rapidjson::rapidjson INTERFACE IMPORTED)
    set_target_properties(rapidjson::rapidjson PROPERTIES
      INTERFACE_INCLUDE_DIRECTORIES "${RAPIDJSON_ROOT}"
    )
  endif()
  return()
endif()

find_package(RapidJSON QUIET CONFIG)

if(TARGET rapidjson AND NOT TARGET rapidjson::rapidjson)
  add_library(rapidjson::rapidjson INTERFACE IMPORTED)
  set_target_properties(rapidjson::rapidjson PROPERTIES
    INTERFACE_LINK_LIBRARIES rapidjson
  )
elseif(NOT TARGET rapidjson::rapidjson AND DEFINED RAPIDJSON_INCLUDE_DIRS AND NOT "${RAPIDJSON_INCLUDE_DIRS}" STREQUAL "")
  add_library(rapidjson::rapidjson INTERFACE IMPORTED)
  set_target_properties(rapidjson::rapidjson PROPERTIES
    INTERFACE_INCLUDE_DIRECTORIES "${RAPIDJSON_INCLUDE_DIRS}"
  )
endif()

if(NOT TARGET rapidjson::rapidjson)
    include(FetchContent)

    set(_rapidjson_git_tag "24b5e7a8b27f42fa16b96fc70aade9106cf7102f")
    message(STATUS "RapidJSON not found, fetching source revision ${_rapidjson_git_tag}")

    FetchContent_Declare(
        rapidjson_src
        GIT_REPOSITORY https://github.com/Tencent/rapidjson.git
        GIT_TAG ${_rapidjson_git_tag}
        GIT_SHALLOW TRUE
        GIT_PROGRESS TRUE
    )

    FetchContent_GetProperties(rapidjson_src)
    if(NOT rapidjson_src_POPULATED)
        FetchContent_Populate(rapidjson_src)
    endif()

    add_library(rapidjson::rapidjson INTERFACE IMPORTED)
    set_target_properties(rapidjson::rapidjson PROPERTIES
        INTERFACE_INCLUDE_DIRECTORIES "${rapidjson_src_SOURCE_DIR}/include"
    )
endif()

# 创建可执行文件
#add_executable(myapp main.cpp)

# 包含 RapidJSON 头文件
#if(rapidjson_ADDED)
#  target_include_directories(myapp PRIVATE ${rapidjson_SOURCE_DIR}/include)
#endif()

#############################################################
# 查找 RapidJSON
#find_package(RapidJSON REQUIRED)

# 创建可执行文件
#add_executable(myapp main.cpp)

# 链接 RapidJSON (只需包含头文件)
#target_include_directories(myapp PRIVATE ${RAPIDJSON_INCLUDE_DIRS})

# 如果需要，可以添加 RapidJSON 的编译定义
#target_compile_definitions(myapp PRIVATE ${RAPIDJSON_DEFINITIONS})