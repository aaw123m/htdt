cmake_minimum_required(VERSION 3.21)
project(htdt_r100b_mfem_probe LANGUAGES CXX)

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

add_executable(r100b_mfem_rigid_modes rigid_modes.cpp)
target_compile_features(r100b_mfem_rigid_modes PRIVATE cxx_std_17)
target_link_libraries(r100b_mfem_rigid_modes PRIVATE mfem)

add_executable(r100b_mfem_concave_transfer concave_transfer.cpp)
target_compile_features(r100b_mfem_concave_transfer PRIVATE cxx_std_17)
target_link_libraries(r100b_mfem_concave_transfer PRIVATE mfem)

add_executable(r100b_mfem_concave_finite_record concave_finite_record.cpp)
target_compile_features(r100b_mfem_concave_finite_record PRIVATE cxx_std_17)
target_link_libraries(r100b_mfem_concave_finite_record PRIVATE mfem)

add_executable(r100b_mfem_rectangular_semidiscrete rectangular_semidiscrete.cpp)
target_compile_features(r100b_mfem_rectangular_semidiscrete PRIVATE cxx_std_17)
target_link_libraries(r100b_mfem_rectangular_semidiscrete PRIVATE mfem)

add_executable(r100b_mfem_portal_transfer portal_transfer.cpp)
target_compile_features(r100b_mfem_portal_transfer PRIVATE cxx_std_17)
target_link_libraries(r100b_mfem_portal_transfer PRIVATE mfem)

add_executable(r100b_mfem_radiation_transfer radiation_transfer.cpp)
target_compile_features(r100b_mfem_radiation_transfer PRIVATE cxx_std_17)
target_link_libraries(r100b_mfem_radiation_transfer PRIVATE mfem)
