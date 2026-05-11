## Helper to create run_<name> custom target for an executable
# Usage:
#   add_run_target(<exe_target> [ARGS <arg1> <arg2> ...])
# This will create a custom target named run_<exe_target> that executes
# the built executable with the provided arguments and sets WORKING_DIRECTORY
# to the target binary directory.

function(add_run_target exe_target)
  cmake_parse_arguments(RUN "" "" "ARGS" ${ARGN})
  set(run_name "run_${exe_target}")

  # Resolve path to target file at build time using generator expression
  set(cmd "$<TARGET_FILE:${exe_target}>")

  if(RUN_UNPARSED_ARGUMENTS)
    # support older CMake where cmake_parse_arguments may behave differently
    set(args_list ${RUN_UNPARSED_ARGUMENTS})
  else()
    set(args_list ${RUN_ARGS})
  endif()

  # Simplified: do not create a wrapper file. Instead invoke the built
  # executable directly using `cmake -E chdir` so the command runs in the
  # target's binary directory and the generator-expression $<TARGET_FILE:...>
  # is expanded to the absolute executable path at build time.
  add_custom_target(${run_name}
    COMMAND ${CMAKE_COMMAND} -E chdir ${CMAKE_CURRENT_BINARY_DIR} $<TARGET_FILE:${exe_target}> ${args_list}
    DEPENDS ${exe_target}
    COMMENT "Running ${exe_target}..."
    VERBATIM
  )
endfunction()
