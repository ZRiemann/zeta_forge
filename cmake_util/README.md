# cmake_util
cmake utils, such as find packages and pre definitions.

This copy is maintained by `zeta_forge`. The original standalone
`cmake_util` project is treated as legacy and is no longer the update point
for ZetaX projects.

Legacy `cbuild` launcher scripts are intentionally not maintained here.
Projects should use their `zbuild.py` entrypoints and consume these files as
CMake helper modules.

## add_run_target helper

`add_run_target(exe_target [ARGS <args>...])` creates a CMake custom target
named `run_<exe_target>` that executes the built executable in the current
binary directory.

Usage examples:

```cmake
add_run_target(uni_svr ARGS ${PROJECT_SOURCE_DIR}/doc/server.json uni_svr)
```

Run it from CMake (no extra args):

```bash
cmake --build build --target run_uni_svr
```
