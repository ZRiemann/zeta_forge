find_package(OpenCV QUIET)

# 检查是否找到
if(OpenCV_FOUND)
    message(STATUS "OpenCV ${OpenCV_VERSION}")
    message(STATUS "OpenCV ${OpenCV_INCLUDE_DIRS}")
    message(STATUS "OpenCV ${OpenCV_LIBS}")
else()
    message(WARNING "OpenCV NOT FOUND")
end()

# demo
# include_directories(${OpenCV_INCLUDE_DIRS})
# add_executable(DisplayImage DisplayImage.cpp)
# target_link_libraries(DisplayImage ${OpenCV_LIBS})