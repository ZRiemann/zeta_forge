if(CMAKE_BUILD_TYPE)
  set(_folly_build_type "${CMAKE_BUILD_TYPE}")
else()
  set(_folly_build_type "Release")
endif()

include("${CMAKE_CURRENT_LIST_DIR}/../../../build/${_folly_build_type}/conan/build/${_folly_build_type}/generators/LibeventConfig.cmake")

if(TARGET libevent::core)
  string(TOUPPER "${_folly_build_type}" _folly_build_type_upper)
  set(_libevent_include_dir "${libevent_libevent_core_INCLUDE_DIRS_${_folly_build_type_upper}}")
  set(_libevent_targets "${libevent_libevent_core_LIBRARIES_TARGETS}")
  if(_libevent_targets)
    list(GET _libevent_targets 0 _libevent_target)
    get_target_property(_libevent_location "${_libevent_target}" "IMPORTED_LOCATION_${_folly_build_type_upper}")
    if(NOT _libevent_location)
      get_target_property(_libevent_location "${_libevent_target}" IMPORTED_LOCATION_NOCONFIG)
    endif()
  endif()
endif()

if(NOT EXISTS "${_libevent_location}" OR NOT EXISTS "${_libevent_include_dir}/event.h")
  message(FATAL_ERROR "Folly requires the Conan Libevent library and headers for ${_folly_build_type}")
endif()

# FindLibEvent reuses these values in find_path/find_library. Creating an event
# target would make it explicitly select CMP0026 OLD, removed in CMake 4.
set(LIBEVENT_INCLUDE_DIRS "${_libevent_include_dir}")
set(LIBEVENT_INCLUDE_DIR "${_libevent_include_dir}")
set(LIBEVENT_LIB "${_libevent_location}")
set(Libevent_FOUND TRUE)
