# 查找CUDA工具包
find_package(CUDAToolkit QUIET)
if(NOT CUDAToolkit_FOUND)
    message(WARNING "CUDA Toolkit not found")
else()
enable_language(CUDA)

# 设置CUDA架构
set(CMAKE_CUDA_ARCHITECTURES "native")

# 设置CUDA标准
set(CMAKE_CUDA_STANDARD 17)
set(CMAKE_CUDA_STANDARD_REQUIRED ON)

add_library(cuda_options INTERFACE)
# 创建接口库以保存CUDA编译选项
option(ENABLE_FAST_MATH  "Enable CUDA fast math"   ON)

# 编译阶段（.cu -> .o）
if(ENABLE_FAST_MATH)
  target_compile_options(cuda_options INTERFACE
    $<$<COMPILE_LANGUAGE:CUDA>:--use_fast_math>
  )
endif()

# IPO/LTO
option(ENABLE_CUDA_DEVICE_LTO "Enable CUDA device LTO (-dlto) in Release config" ON)
if(ENABLE_CUDA_DEVICE_LTO)
    # 设备 LTO：编译 + 链接都加 -dlto
  target_compile_options(cuda_options INTERFACE
    $<$<AND:$<CONFIG:Release>,$<COMPILE_LANGUAGE:CUDA>>:-dlto>
  )
  # 设备链接阶段（nvlink）
  target_link_options(cuda_options INTERFACE
    $<$<AND:$<CONFIG:Release>,$<LINK_LANGUAGE:CUDA>>:-dlto>
  )
endif()
endif()

#add_subdirectory(6_2_Runtime)
#add_subdirectory(zpp)

#set(CUDA_KERNELS_DIR "${PROJECT_SOURCE_DIR}/AI/CUDA/kernels")
#[[添加CUDA编译选项
适合使用 --use_fast_math 的场景:
1. 图形和图像处理应用
2. 机器学习训练
3. 物理模拟和游戏

不应使用 --use_fast_math 的场景
1. 科学计算和数值分析
   气候模型和天气预报, 计算流体力学(CFD), 分子动力学模拟, 精确的物理模拟
2. 金融和经济模型: 风险评估和定价模型, 期权定价, 高频交易算法
3. 密码学和安全应用
4. 测试和验证

sin, cos, tan	最大误差约为1-2个ULP	2-10倍
exp, log	最大误差约为1-3个ULP	2-5倍
pow	最大误差可达几个ULP	2-7倍
sqrt	最大误差约为1个ULP	1.5-3倍
除法操作	精度略有降低	1.5-2倍
ULP (Unit in the Last Place) 是衡量浮点精度的单位，表示在给定数值的最低有效位上的一个单位变化。
###################################################################################3
set(CUDA_KERNELS_DIR "${PROJECT_SOURCE_DIR}/AI/CUDA/kernels")
set(CUDA_KERNELS_INC_DIR "${PROJECT_SOURCE_DIR}/AI/CUDA")

add_executable(cuda_demo
    ${ZCU_BASE_SRC}
    ../kernels/kernel_test.cu
    ../kernels/kernel_vector_add.cu
    test_stream_parallel.cu test_graph.cu test_p2p.cu 
    test_mem_2d.cu test_mem_3d.cu
    benchmark_memcpy.cu benchmark_workflow_overlap.cu
)

# 设置CUDA特定选项
set_target_properties(cuda_demo PROPERTIES
    CUDA_SEPARABLE_COMPILATION ON
)

target_link_libraries(cuda_demo PRIVATE
    CUDA::cudart
    Threads::Threads
    cuda_options
    cxx_options
)
                             
target_include_directories(cuda_demo PRIVATE .
                            "${CUDA_KERNELS_INC_DIR}"
                            "${PROJECT_BINARY_DIR}"
                            "${PROJECT_SOURCE_DIR}/include"
                            "${PROJECT_SOURCE_DIR}/spdlog/include"
                            "${RapidJSON_INCLUDE_DIRS}"
                            )

add_custom_target(run_cuda_demo
    COMMAND $<TARGET_FILE:cuda_demo> ${PROJECT_SOURCE_DIR}/doc/config/server.json
    DEPENDS cuda_demo
    WORKING_DIRECTORY ${CMAKE_CURRENT_BINARY_DIR}
    COMMENT "Running cuda_demo..."
)
]]