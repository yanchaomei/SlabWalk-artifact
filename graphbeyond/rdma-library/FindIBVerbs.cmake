# FindIBVerbs.cmake
# Locate the InfiniBand verbs library.
#
# Sets the following variables:
#   IBVERBS_FOUND          - true if libibverbs was found
#   IBVERBS_INCLUDE_DIR    - directory containing infiniband/verbs.h
#   IBVERBS_LIBRARY        - path to libibverbs.so
#   IBVerbs_FOUND          - alias for IBVERBS_FOUND (REQUIRED keyword)

find_path(IBVERBS_INCLUDE_DIR
  NAMES infiniband/verbs.h
  HINTS /usr/include /usr/local/include
)

find_library(IBVERBS_LIBRARY
  NAMES ibverbs
  HINTS /usr/lib /usr/lib/x86_64-linux-gnu /usr/local/lib
)

include(FindPackageHandleStandardArgs)
find_package_handle_standard_args(IBVerbs
  REQUIRED_VARS IBVERBS_LIBRARY IBVERBS_INCLUDE_DIR
)

if(IBVerbs_FOUND)
  set(IBVERBS_FOUND TRUE)
  if(NOT TARGET IBVerbs::IBVerbs)
    add_library(IBVerbs::IBVerbs UNKNOWN IMPORTED)
    set_target_properties(IBVerbs::IBVerbs PROPERTIES
      IMPORTED_LOCATION "${IBVERBS_LIBRARY}"
      INTERFACE_INCLUDE_DIRECTORIES "${IBVERBS_INCLUDE_DIR}"
    )
  endif()
endif()

mark_as_advanced(IBVERBS_INCLUDE_DIR IBVERBS_LIBRARY)
