#!/usr/bin/env bash
set -euo pipefail

release_action=build
if (( $# == 1 )) && [[ $1 == --rebuild-release ]]; then
    release_action=rebuild
elif (( $# != 0 )); then
    printf 'Usage: bash %s [--rebuild-release]\n' "$0" >&2
    exit 2
fi

cd -- "$(dirname -- "${BASH_SOURCE[0]}")/../../.."
mkdir -p build
evidence=$(mktemp -d "$PWD/build/phase2-acceptance.XXXXXX")
step=setup
report_failure() {
    local status=$1
    if (( status != 0 )); then
        printf '\nFailed: %s (exit %s)\nAcceptance evidence: %s\n' "$step" "$status" "$evidence" >&2
    fi
}
trap 'report_failure "$?"' EXIT
printf 'Acceptance evidence: %s\n' "$evidence"

run_step() {
    step=$1
    shift
    {
        printf '\n[%s]' "$step"
        printf ' %q' "$@"
        printf '\n'
        "$@"
    } 2>&1 | tee "$evidence/$step.log"
}

record_tools() {
    uname -smr
    conan --version
    cmake --version
    ninja --version
    c++ --version
    rustc --version
    python3 --version
}

targets=(deps grpc hpx folly nng)
fixture=tests/manual/native_delivery
export ZETA_INSTALL_PREFIX="$evidence/release-prefix"

git rev-parse HEAD > "$evidence/head"
git diff HEAD --binary | sha256sum > "$evidence/worktree.sha256"
git status --short > "$evidence/worktree-status"
git submodule status > "$evidence/submodules"
run_step tools record_tools

run_step release-build ./zbuild.py "$release_action" "${targets[@]}" --profile release -j 4
run_step conan-reuse ./zbuild.py build deps --profile release -j 2
step=conan-reuse-check
grep -F 'conan deps: reuse generators and lock ' "$evidence/conan-reuse.log"
run_step conan-rebuild ./zbuild.py rebuild deps --profile release -j 4
step=conan-retained-lock-check
grep -F 'conan deps: regenerate using retained lock ' "$evidence/conan-rebuild.log"
run_step release-install ./zbuild.py install "${targets[@]}" --profile release

step=release-receipts
mkdir -p "$evidence/receipts/release" "$evidence/receipts/debug"
for target in "${targets[@]}"; do
    cp "build/staging/release/$target/.zeta-build-complete.json" "$evidence/receipts/release/$target.json"
done
cp "$ZETA_INSTALL_PREFIX/share/zeta_forge/native/owners.json" "$evidence/receipts/release/owners.json"

run_step consumer-configure cmake -S "$fixture" -B "$evidence/consumers" -G Ninja \
    -DCMAKE_BUILD_TYPE=Release -DFORGE_FULL_NATIVE=ON \
    "-DCMAKE_PREFIX_PATH=$ZETA_INSTALL_PREFIX;$ZETA_INSTALL_PREFIX/lib/cmake/zeta_deps/Release"
run_step consumer-build cmake --build "$evidence/consumers" --parallel 4 --verbose
run_step consumer-tests env LC_ALL=C ctest --test-dir "$evidence/consumers" --output-on-failure --no-tests=error
step=consumer-count
grep -Eq '100% tests passed, 0 tests failed out of 5([[:space:]]|$)' "$evidence/consumer-tests.log"
run_step rust-openssl python3 "$fixture/rust_smoke.py" "$ZETA_INSTALL_PREFIX" "$evidence/rust-openssl-probe"

step=release-nng-baseline
sha256sum "$ZETA_INSTALL_PREFIX/lib/libnng.a" \
    build/staging/release/nng/lib/libnng.a \
    build/staging/release/nng/.zeta-build-complete.json > "$evidence/release-nng.sha256"
run_step debug-nng-build ./zbuild.py build nng --profile debug -j 4
run_step debug-nng-install env "ZETA_INSTALL_PREFIX=$evidence/debug-prefix" ./zbuild.py install nng --profile debug
step=debug-receipts
cp build/staging/debug/nng/.zeta-build-complete.json "$evidence/receipts/debug/nng.json"
cp "$evidence/debug-prefix/share/zeta_forge/native/owners.json" "$evidence/receipts/debug/owners.json"
run_step debug-nng-clean ./zbuild.py clean nng --profile debug
step=debug-clean-check
test ! -e builder/nng/build/Debug
test ! -e build/staging/debug/nng
test -f "$evidence/debug-prefix/lib/libnng.a"
run_step release-nng-verify sha256sum -c "$evidence/release-nng.sha256"

printf '\nPhase 2 overall acceptance commands passed.\nAcceptance evidence: %s\n' "$evidence"
