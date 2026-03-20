include("${CMAKE_CURRENT_LIST_DIR}/../../../build/${CMAKE_BUILD_TYPE}/conan/build/${CMAKE_BUILD_TYPE}/generators/asio-config.cmake")

set(Asio_FOUND TRUE)

if(TARGET asio::asio AND NOT TARGET Asio::asio)
  add_library(Asio::asio ALIAS asio::asio)
endif()