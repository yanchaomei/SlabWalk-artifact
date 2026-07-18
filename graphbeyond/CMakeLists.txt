cmake_minimum_required(VERSION 3.1)
project(rdma-hnsw CXX)

include(CTest)

set(CMAKE_CXX_STANDARD 20)
set(CMAKE_CXX_STANDARD_REQUIRED ON)
set(CMAKE_EXPORT_COMPILE_COMMANDS ON)
set(CMAKE_CXX_FLAGS_DEBUG "-g -fno-omit-frame-pointer -fno-inline -fsanitize=address")

include(CheckCXXSourceCompiles)
set(CMAKE_REQUIRED_FLAGS "${CMAKE_REQUIRED_FLAGS} -std=c++20")
check_cxx_source_compiles(
    "#include <atomic>
     #include <memory>
     int main() {
       std::atomic<std::shared_ptr<int>> value;
       return value.load() ? 1 : 0;
     }"
    GB_HAS_ATOMIC_SHARED_PTR)
unset(CMAKE_REQUIRED_FLAGS)
if (NOT GB_HAS_ATOMIC_SHARED_PTR)
    message(FATAL_ERROR
        "GraphBeyond requires a C++20 standard library with "
        "std::atomic<std::shared_ptr<T>> support (validated with GCC 12+).")
endif ()

# set default build type to Release
if (NOT CMAKE_BUILD_TYPE)
    set(CMAKE_BUILD_TYPE Release)
endif ()

# compiler flags
add_compile_options(-Wall -Wextra -Wpedantic -Wfatal-errors)
add_compile_options(-march=native -ffast-math -mavx2)
add_compile_options(-DNOHUGEPAGES)  # GraphBeyond: skip MAP_HUGETLB on SKV (no hugepages configured)
#add_compile_options(-DDEBUG_HNSW)
#add_compile_options(-g -fno-omit-frame-pointer -fno-inline)  # for profiling

set(CMAKE_MODULE_PATH ${CMAKE_SOURCE_DIR})
include_directories(src)
include_directories(thirdparty)
add_subdirectory(rdma-library)
add_subdirectory(thirdparty)

file(GLOB_RECURSE SOURCES src/*.cc)

# executables
add_executable(shine ${SOURCES})
target_link_libraries(shine rdma_library)
target_compile_options(shine PRIVATE $<$<COMPILE_LANGUAGE:CXX>:-fcoroutines>)

# baseline-opt: identical sources, -DSHINE_OPT enables the
# open-addressing visited set (the "faster hashset" TODO). Used to
# show an optimized baseline still leaves the op-count wall.
add_executable(shine-opt ${SOURCES})
target_link_libraries(shine-opt rdma_library)
target_compile_options(shine-opt PRIVATE $<$<COMPILE_LANGUAGE:CXX>:-fcoroutines>)
target_compile_definitions(shine-opt PRIVATE SHINE_OPT)

if (BUILD_TESTING)
    function(add_graphbeyond_header_test test_name source_file)
        add_executable(${test_name} ${source_file})
        target_include_directories(${test_name} PRIVATE ${CMAKE_SOURCE_DIR}/src)
        target_compile_options(${test_name} PRIVATE -UNDEBUG)
        add_test(NAME ${test_name} COMMAND ${test_name})
    endfunction()

    add_graphbeyond_header_test(
        evidence_hash_test tests/evidence_hash_test.cc)
    add_graphbeyond_header_test(
        materialization_policy_test tests/materialization_policy_test.cc)
    add_graphbeyond_header_test(
        native_descriptor_test tests/native_descriptor_test.cc)
    add_graphbeyond_header_test(
        staged_io_test tests/staged_io_test.cc)
    add_graphbeyond_header_test(
        query_result_fingerprint_test tests/query_result_fingerprint_test.cc)
    add_graphbeyond_header_test(
        rabitq_rotation_reuse_test tests/rabitq_rotation_reuse_test.cc)
    target_link_libraries(rabitq_rotation_reuse_test PRIVATE rdma_library)
    add_graphbeyond_header_test(
        build_snapshot_test tests/build_snapshot_test.cc)

    find_package(PythonInterp 3 REQUIRED)
    set(GB_REPO_ROOT ${CMAKE_SOURCE_DIR}/..)
    add_test(
        NAME vldb_binary_ab_contract
        COMMAND ${PYTHON_EXECUTABLE}
                ${GB_REPO_ROOT}/experiments/sigmetrics/test_vldb_binary_ab.py)
    add_test(
        NAME vldb_query_profile_contract
        COMMAND ${PYTHON_EXECUTABLE}
                ${GB_REPO_ROOT}/experiments/sigmetrics/test_run_vldb_query_profile.py)
    add_test(
        NAME graphbeyond_build_contract
        COMMAND ${PYTHON_EXECUTABLE}
                ${GB_REPO_ROOT}/experiments/sigmetrics/test_graphbeyond_build_contract.py)
    set_tests_properties(
        vldb_binary_ab_contract
        vldb_query_profile_contract
        graphbeyond_build_contract
        PROPERTIES
        WORKING_DIRECTORY ${GB_REPO_ROOT}
        ENVIRONMENT "PYTHONPATH=${GB_REPO_ROOT}")
endif ()

# status prints
get_target_property(compile_options shine COMPILE_OPTIONS)
message(STATUS "Compile options: ${compile_options}")
