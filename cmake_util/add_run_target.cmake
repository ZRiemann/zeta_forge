## Helper to create run_<name> custom target for an executable
# Usage:
#   add_run_target(<exe_target> [ARGS <arg1> <arg2> ...])
# This will create a custom target named run_<exe_target> that executes
# the built executable with the provided arguments and sets WORKING_DIRECTORY
# to the target binary directory.

# A new configuration invalidates metadata from targets that are now disabled.
# This runs even when no add_run_target() invocation remains enabled.
get_property(_zeta_run_token GLOBAL PROPERTY ZETA_RUN_CONFIGURATION_TOKEN)
if(NOT _zeta_run_token)
  string(RANDOM LENGTH 32 ALPHABET "0123456789abcdef" _zeta_run_token)
  set_property(GLOBAL PROPERTY ZETA_RUN_CONFIGURATION_TOKEN "${_zeta_run_token}")
  file(MAKE_DIRECTORY "${CMAKE_BINARY_DIR}/.zeta-run")
  file(WRITE "${CMAKE_BINARY_DIR}/.zeta-run/configuration-token" "${_zeta_run_token}")
endif()

function(add_run_target exe_target)
  cmake_parse_arguments(RUN "" "" "ARGS" ${ARGN})
  set(run_name "run_${exe_target}")

  if(RUN_UNPARSED_ARGUMENTS)
    # support older CMake where cmake_parse_arguments may behave differently
    set(args_list ${RUN_UNPARSED_ARGUMENTS})
  else()
    set(args_list ${RUN_ARGS})
  endif()

  # Let CMake set cwd and quote argv directly. A `cmake -E chdir` wrapper
  # reparses quotes/backslashes in arguments on some CMake versions.
  add_custom_target(${run_name}
    COMMAND $<TARGET_FILE:${exe_target}> ${args_list}
    WORKING_DIRECTORY "${CMAKE_CURRENT_BINARY_DIR}"
    DEPENDS ${exe_target}
    COMMENT "Running ${exe_target}..."
    VERBATIM
  )

  # Native values are generated after target properties/generator expressions
  # resolve. Separate raw UTF-8 fields preserve spaces, quotes and newlines
  # without asking Python to interpret CMake or escape generated JSON strings.
  if(NOT exe_target MATCHES "^[A-Za-z0-9_.+-]+$" OR exe_target MATCHES "^\\.\\.?$")
    message(FATAL_ERROR "Unsupported registered run target name: ${exe_target}")
  endif()
  set(metadata_dir "${CMAKE_BINARY_DIR}/.zeta-run/$<CONFIG>/${exe_target}")
  get_property(configuration_token GLOBAL PROPERTY ZETA_RUN_CONFIGURATION_TOKEN)
  file(GENERATE OUTPUT "${metadata_dir}/configuration-token" CONTENT "${configuration_token}")
  file(GENERATE OUTPUT "${metadata_dir}/schema" CONTENT "1")
  file(GENERATE OUTPUT "${metadata_dir}/target" CONTENT "${exe_target}")
  file(GENERATE OUTPUT "${metadata_dir}/configuration" CONTENT "$<CONFIG>")
  file(GENERATE OUTPUT "${metadata_dir}/source" CONTENT "${CMAKE_SOURCE_DIR}")
  file(GENERATE OUTPUT "${metadata_dir}/build" CONTENT "${CMAKE_BINARY_DIR}")
  file(GENERATE OUTPUT "${metadata_dir}/executable" CONTENT "$<TARGET_FILE:${exe_target}>")
  file(GENERATE OUTPUT "${metadata_dir}/cwd" CONTENT "${CMAKE_CURRENT_BINARY_DIR}")
  list(LENGTH args_list argument_count)
  file(GENERATE OUTPUT "${metadata_dir}/argc" CONTENT "${argument_count}")
  set(argument_index 0)
  foreach(argument IN LISTS args_list)
    file(GENERATE OUTPUT "${metadata_dir}/arg-${argument_index}" CONTENT "${argument}")
    math(EXPR argument_index "${argument_index} + 1")
  endforeach()
endfunction()
