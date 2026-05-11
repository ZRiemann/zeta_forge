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

find_package(RapidJSON QUIET)

if(NOT TARGET rapidjson::rapidjson)
    message(STATUS "RapidJSON not found, will download it")
    # 添加 RapidJSON
    CPMAddPackage(
        NAME rapidjson
        URL https://github.com/rigtorp/MPMCQueue/archive/refs/tags/v1.0.zip
        #URL_HASH SHA256=8e00c38829d6785a2dfb951bb87c6974fa07dfe488aa5b25deec4b8bc0f6a3ab
        DOWNLOAD_ONLY YES
    )
    #[[
    CPMAddPackage(
        NAME rapidjson
        GITHUB_REPOSITORY Tencent/rapidjson
        GIT_TAG v1.1.0
        GIT_SHALLOW TRUE
        GIT_PROGRESS TRUE
        DOWNLOAD_ONLY YES  # 仅下载，不构建（因为它是仅头文件库）
    )
    ]]

    add_library(rapidjson::rapidjson INTERFACE IMPORTED)
    set_target_properties(rapidjson::rapidjson PROPERTIES
        INTERFACE_INCLUDE_DIRECTORIES "${rapidjson_SOURCE_DIR}/include"
    )
    #target_link_libraries(myexe_or_lib PUBLIC rapidjson::rapidjson)
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