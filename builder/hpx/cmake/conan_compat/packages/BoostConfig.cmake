get_filename_component(_hpx_build_root "${CMAKE_CURRENT_LIST_DIR}/../../.." ABSOLUTE)
if(CMAKE_BUILD_TYPE)
  set(_hpx_build_type "${CMAKE_BUILD_TYPE}")
else()
  set(_hpx_build_type "Release")
endif()
set(_hpx_conan_generators_dir "${_hpx_build_root}/build/${_hpx_build_type}/conan/build/${_hpx_build_type}/generators")

include("${_hpx_conan_generators_dir}/BoostConfig.cmake")

# Conan CMakeDeps creates Boost component targets but does not set the
# Boost_<COMPONENT>_FOUND variables expected by HPX's Boost setup modules.
foreach(_hpx_boost_component IN LISTS boost_COMPONENT_NAMES)
  string(REPLACE "Boost::" "" _hpx_boost_component_name "${_hpx_boost_component}")
  string(TOUPPER "${_hpx_boost_component_name}" _hpx_boost_component_upper)
  if(TARGET Boost::${_hpx_boost_component_name})
    set(Boost_${_hpx_boost_component_upper}_FOUND TRUE)
  endif()
endforeach()

unset(_hpx_boost_component)
unset(_hpx_boost_component_name)
unset(_hpx_boost_component_upper)
unset(_hpx_build_root)
unset(_hpx_build_type)
unset(_hpx_conan_generators_dir)
