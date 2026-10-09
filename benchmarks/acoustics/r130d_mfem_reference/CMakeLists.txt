cmake_minimum_required(VERSION 3.21)
project(htdt_r130d_mfem_reference LANGUAGES CXX)

if(NOT DEFINED HTDT_MFEM_SOURCE_DIR)
  message(FATAL_ERROR "HTDT_MFEM_SOURCE_DIR is required")
endif()

set(BUILD_SHARED_LIBS OFF CACHE BOOL "" FORCE)
set(MFEM_USE_MPI OFF CACHE BOOL "" FORCE)
set(MFEM_USE_METIS OFF CACHE BOOL "" FORCE)
set(MFEM_USE_LAPACK OFF CACHE BOOL "" FORCE)
set(MFEM_ENABLE_TESTING OFF CACHE BOOL "" FORCE)
set(MFEM_ENABLE_EXAMPLES OFF CACHE BOOL "" FORCE)
set(MFEM_ENABLE_MINIAPPS OFF CACHE BOOL "" FORCE)
set(MFEM_ENABLE_BENCHMARKS OFF CACHE BOOL "" FORCE)
set(MFEM_USE_OPENMP OFF CACHE BOOL "" FORCE)
set(MFEM_USE_ZLIB OFF CACHE BOOL "" FORCE)

add_subdirectory("${HTDT_MFEM_SOURCE_DIR}" mfem-src EXCLUDE_FROM_ALL)

add_executable(r130d_mfem_sloped_tet_system sloped_tet_system.cpp)
target_compile_features(r130d_mfem_sloped_tet_system PRIVATE cxx_std_17)
target_link_libraries(r130d_mfem_sloped_tet_system PRIVATE mfem)

# Isolated P2 r4 sparse reference: only the refinement bound differs from
# frozen r1–r3 exporter. The MFEM library commit remains pinned independently.
add_executable(r130d_mfem_sloped_tet_r4_system sloped_tet_system_r4.cpp)
target_compile_features(r130d_mfem_sloped_tet_r4_system PRIVATE cxx_std_17)
target_link_libraries(r130d_mfem_sloped_tet_r4_system PRIVATE mfem)

# Independent convex tetrahedral wave reference, not a production backend.
add_executable(r130d_mfem_convex_tet_system convex_tet_system.cpp)
target_compile_features(r130d_mfem_convex_tet_system PRIVATE cxx_std_17)
target_link_libraries(r130d_mfem_convex_tet_system PRIVATE mfem)
