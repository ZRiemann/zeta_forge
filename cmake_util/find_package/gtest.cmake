
find_package(GTest QUIET CONFIG)

if(NOT GTest_FOUND)
    if(ZPP_USE_CONAN)
        message(FATAL_ERROR "GTest was not found in Conan mode")
    endif()

    message(STATUS "GTest not found, will download and build it")
    CPMAddPackage(
        NAME GTest
        GITHUB_REPOSITORY google/googletest
        VERSION 1.14.0
        GIT_SHALLOW TRUE
        GIT_PROGRESS TRUE
        OPTIONS
            "INSTALL_GTEST OFF"
            "gtest_force_shared_crt ON"
    )

    message(STATUS "GTest added via CPM")
else()
    message(STATUS "Found GTest: ${GTEST_LIBRARIES}")
endif()

if(DEFINED GTest_VERSION)
    message(STATUS "Using GTest ${GTest_VERSION}")
endif()