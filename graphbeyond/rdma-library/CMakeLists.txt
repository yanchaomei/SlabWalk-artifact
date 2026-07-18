cmake_minimum_required(VERSION 3.0)

# Use our FindIBVerbs.cmake module
list(APPEND CMAKE_MODULE_PATH "${CMAKE_CURRENT_SOURCE_DIR}")
set(THREADS_PREFER_PTHREAD_FLAG TRUE)

# required packages
find_package(IBVerbs REQUIRED)
find_package(Boost COMPONENTS program_options REQUIRED)
find_package(Threads REQUIRED)

# Some deployments contain a stale TBBConfig.cmake that pairs oneTBB headers
# with the ABI-incompatible legacy libtbb.so.2. Resolve the header and library
# independently, then prove that the pair links before compiling GraphBeyond.
find_path(GB_TBB_INCLUDE_DIR NAMES oneapi/tbb/concurrent_queue.h)
find_library(GB_TBB_LIBRARY NAMES tbb)
if (NOT GB_TBB_INCLUDE_DIR OR NOT GB_TBB_LIBRARY)
    message(FATAL_ERROR
        "GraphBeyond requires oneTBB headers and a linkable libtbb. Install "
        "the oneTBB development package or set GB_TBB_INCLUDE_DIR and "
        "GB_TBB_LIBRARY explicitly.")
endif ()

include(CheckIncludeFileCXX)
set(CMAKE_REQUIRED_INCLUDES ${GB_TBB_INCLUDE_DIR})
check_include_file_cxx("oneapi/tbb/concurrent_queue.h" GB_HAS_ONETBB_HEADERS)
if (NOT GB_HAS_ONETBB_HEADERS)
    message(FATAL_ERROR
        "GraphBeyond requires oneTBB headers under oneapi/tbb; the legacy "
        "tbb/concurrent_queue.h-only package is not compatible.")
endif ()

include(CheckCXXSourceCompiles)
set(CMAKE_REQUIRED_LIBRARIES ${GB_TBB_LIBRARY})
check_cxx_source_compiles(
    "#include <oneapi/tbb/concurrent_queue.h>
     #include <oneapi/tbb/concurrent_vector.h>
     int main() {
       oneapi::tbb::concurrent_queue<int> queue;
       queue.push(1);
       oneapi::tbb::concurrent_vector<int> values;
       values.push_back(1);
       return values.front();
     }"
    GB_HAS_ONETBB_ABI)
unset(CMAKE_REQUIRED_INCLUDES)
unset(CMAKE_REQUIRED_LIBRARIES)
if (NOT GB_HAS_ONETBB_ABI)
    message(FATAL_ERROR
        "The selected oneTBB headers and library are ABI-incompatible. Set "
        "GB_TBB_INCLUDE_DIR and GB_TBB_LIBRARY to a matching oneTBB install.")
endif ()

add_library(graphbeyond_tbb UNKNOWN IMPORTED)
set_target_properties(graphbeyond_tbb PROPERTIES
    IMPORTED_LOCATION "${GB_TBB_LIBRARY}"
    INTERFACE_INCLUDE_DIRECTORIES "${GB_TBB_INCLUDE_DIR}")

# our library
file(GLOB_RECURSE LIBRARY_CC library/**.cc)
add_library(rdma_library STATIC ${LIBRARY_CC})
target_include_directories(rdma_library PUBLIC .)
target_link_libraries(rdma_library PUBLIC
    ${IBVERBS_LIBRARY} ${Boost_LIBRARIES} graphbeyond_tbb Threads::Threads)
