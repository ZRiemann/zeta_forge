get_filename_component(_hpx_build_root "${CMAKE_CURRENT_LIST_DIR}/../../.." ABSOLUTE)
if(CMAKE_BUILD_TYPE)
  set(_hpx_build_type "${CMAKE_BUILD_TYPE}")
else()
  set(_hpx_build_type "Release")
endif()

include("${_hpx_build_root}/build/${_hpx_build_type}/conan/build/${_hpx_build_type}/generators/BoostConfigVersion.cmake")

unset(_hpx_build_root)
unset(_hpx_build_type)
