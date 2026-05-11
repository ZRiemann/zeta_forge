find_package(PkgConfig REQUIRED)
pkg_check_modules(MODBUS libmodbus)

if(MODBUS_FOUND)
    message(STATUS "Found libmodbus: V${MODBUS_VERSION} dir[${LIBMODBUS_LIBRARY_DIRS}] lib[${MODBUS_LIBRARIES}] inc[${MODBUS_INCLUDE_DIRS}]")
else()
    message(WARNING
        "libmodbus not found! Please install it using:\n"
        "  sudo apt update && sudo apt install libmodbus-dev"
    )
endif()

# add_executable(test_modbus main.cpp)
# target_include_directories(test_modbus PRIVATE ${MODBUS_INCLUDE_DIRS})
# target_link_libraries(test_modbus PRIVATE ${MODBUS_LIBRARIES})

# Ubuntu Install libmodbus-dev
# sudo apt update
# sudo apt install libmodbus-dev

#if(NOT MODBUS_FOUND)
#    message(WARNING "libmodbus not found, skipping ${CMAKE_CURRENT_LIST_FILE}")
#    return() #会立即终止当前脚本的执行，相当于“跳过后续所有内容”。
#endif()
# 后续内容只有找到 libmodbus 时才会执行
# message(STATUS "libmodbus found! Continue processing...")