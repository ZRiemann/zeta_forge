include("${CMAKE_CURRENT_LIST_DIR}/../../../build/${CMAKE_BUILD_TYPE}/conan/build/${CMAKE_BUILD_TYPE}/generators/asio-config.cmake")

set(Asio_FOUND TRUE)

if(TARGET asio::asio AND NOT TARGET Asio::asio)
  add_library(Asio::asio ALIAS asio::asio)
endif()

if(TARGET Asio::asio AND NOT TARGET asio::asio)
  add_library(asio::asio ALIAS Asio::asio)
endif()

# HPX's Asio setup checks Asio_INCLUDE_DIR in addition to Asio_FOUND/targets.
if(TARGET asio::asio)
  get_target_property(_zeta_asio_include_dirs asio::asio INTERFACE_INCLUDE_DIRECTORIES)
  if(_zeta_asio_include_dirs)
    set(Asio_INCLUDE_DIRS "${_zeta_asio_include_dirs}")
    list(GET _zeta_asio_include_dirs 0 Asio_INCLUDE_DIR)
    # Keep a compatibility alias for projects expecting the upper-case variable.
    set(ASIO_INCLUDE_DIR "${Asio_INCLUDE_DIR}")
  endif()
endif()