include("${CMAKE_CURRENT_LIST_DIR}/../../../build/${CMAKE_BUILD_TYPE}/conan/build/${CMAKE_BUILD_TYPE}/generators/hwloc-config.cmake")

set(Hwloc_FOUND TRUE)

if(TARGET hwloc::hwloc AND NOT TARGET Hwloc::hwloc)
  add_library(Hwloc::hwloc ALIAS hwloc::hwloc)
endif()