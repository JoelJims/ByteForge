cmake_minimum_required(VERSION 3.28)
project(byteforge_btrblocks_probe LANGUAGES C CXX)

set(BTRBLOCKS_SOURCE "" CACHE PATH "Unmodified pinned BtrBlocks checkout")
if(NOT EXISTS "${BTRBLOCKS_SOURCE}/btrblocks/local.cmake")
  message(FATAL_ERROR "Set BTRBLOCKS_SOURCE to the pinned BtrBlocks checkout")
endif()
execute_process(COMMAND git -C "${BTRBLOCKS_SOURCE}" rev-parse HEAD
  OUTPUT_VARIABLE BTR_REV OUTPUT_STRIP_TRAILING_WHITESPACE COMMAND_ERROR_IS_FATAL ANY)
if(NOT BTR_REV STREQUAL "c950c3c01c1fbf91007936b05781d431a29a60d3")
  message(FATAL_ERROR "Unexpected BtrBlocks revision: ${BTR_REV}")
endif()
execute_process(COMMAND git -C "${BTRBLOCKS_SOURCE}" status --porcelain
  OUTPUT_VARIABLE BTR_DIRTY COMMAND_ERROR_IS_FATAL ANY)
if(NOT BTR_DIRTY STREQUAL "")
  message(FATAL_ERROR "BtrBlocks checkout must be clean")
endif()

# The upstream top-level project is not embeddable (absolute CMAKE_SOURCE_DIR
# includes). Build its unchanged core sources with its pinned codec dependencies,
# excluding unrelated S3 tooling, tests and benchmark executables.
set(CMAKE_CXX_STANDARD 17)
set(CMAKE_CXX_STANDARD_REQUIRED ON)
set(NO_SIMD OFF)
option(BYTEFORGE_SANITIZE "Instrument the core/probe and FSST with AddressSanitizer" OFF)
option(BYTEFORGE_FSST_SAFETY_FIX "Apply the named FSST safety-v1 corrections" OFF)
find_package(Threads REQUIRED)
foreach(dependency fastpfor fsst croaring dynamic_bitset)
  include("${BTRBLOCKS_SOURCE}/cmake/${dependency}.cmake")
endforeach()
# The pinned FSST omits <cstdint> and depended on transitive standard-library
# includes removed in GCC 16. Supply that header without editing vendor source.
ExternalProject_Get_Property(fsst_src source_dir binary_dir)
find_package(Python3 REQUIRED COMPONENTS Interpreter)
ExternalProject_Add_Step(fsst_src sample_capacity_variant
  COMMAND "${Python3_EXECUTABLE}" "${CMAKE_CURRENT_SOURCE_DIR}/fsst_sample_patch.py"
    "${source_dir}/libfsst.cpp" "${BYTEFORGE_FSST_SAFETY_FIX}"
  DEPENDS "${CMAKE_CURRENT_SOURCE_DIR}/fsst_sample_patch.py"
  DEPENDEES patch DEPENDERS configure)
set(FSST_COMPAT_FLAGS "-include cstdint")
if(BYTEFORGE_SANITIZE)
  string(APPEND FSST_COMPAT_FLAGS " -fsanitize=address -fno-omit-frame-pointer")
endif()
ExternalProject_Add_Step(fsst_src fixed_width_header
  COMMAND "${CMAKE_COMMAND}" -S "${source_dir}" -B "${binary_dir}"
    "-DCMAKE_CXX_FLAGS=${FSST_COMPAT_FLAGS}"
  DEPENDEES configure DEPENDERS build)
file(GLOB_RECURSE BTR_SOURCES CONFIGURE_DEPENDS
  "${BTRBLOCKS_SOURCE}/btrblocks/*.cpp" "${BTRBLOCKS_SOURCE}/btrblocks/*.c")
if(BYTEFORGE_FSST_SAFETY_FIX)
  # The upstream FSST adapter never destroys the encoder. Generate a private
  # corrected translation unit, keeping the verified BtrBlocks checkout clean.
  set(FSST_ADAPTER "${BTRBLOCKS_SOURCE}/btrblocks/scheme/string/Fsst.cpp")
  file(READ "${FSST_ADAPTER}" FSST_ADAPTER_TEXT)
  set(FSST_GUARD_ANCHOR "  die_if(fsst_export(encoder, write_ptr) > 0);")
  string(FIND "${FSST_ADAPTER_TEXT}" "${FSST_GUARD_ANCHOR}" FSST_GUARD_POSITION)
  if(FSST_GUARD_POSITION EQUAL -1)
    message(FATAL_ERROR "Unexpected FSST adapter source; cannot apply safety-v1")
  endif()
  string(REPLACE "${FSST_GUARD_ANCHOR}"
    "  std::unique_ptr<fsst_encoder_t, decltype(&fsst_destroy)> guard(encoder, &fsst_destroy);\n${FSST_GUARD_ANCHOR}"
    FSST_ADAPTER_TEXT "${FSST_ADAPTER_TEXT}")
  file(WRITE "${CMAKE_CURRENT_BINARY_DIR}/Fsst-safety-v1.cpp" "${FSST_ADAPTER_TEXT}")
  list(REMOVE_ITEM BTR_SOURCES "${FSST_ADAPTER}")
  list(APPEND BTR_SOURCES "${CMAKE_CURRENT_BINARY_DIR}/Fsst-safety-v1.cpp")
endif()
add_library(btrblocks STATIC ${BTR_SOURCES})
target_include_directories(btrblocks PUBLIC "${BTRBLOCKS_SOURCE}/btrblocks")
target_include_directories(btrblocks PRIVATE "${BTRBLOCKS_SOURCE}/btrblocks/scheme/string")
target_compile_options(btrblocks PRIVATE -march=native -Wall -Wextra -Wno-unused-parameter -g)
target_link_libraries(btrblocks PUBLIC Threads::Threads fsst fastpfor croaring dynamic_bitset)
add_executable(byteforge_btrblocks_probe probe.cpp)
target_link_libraries(byteforge_btrblocks_probe PRIVATE btrblocks)
target_compile_features(byteforge_btrblocks_probe PRIVATE cxx_std_17)
if(BYTEFORGE_SANITIZE)
  foreach(target btrblocks byteforge_btrblocks_probe)
    target_compile_options(${target} PRIVATE -fsanitize=address -fno-omit-frame-pointer)
    target_link_options(${target} PUBLIC -fsanitize=address)
  endforeach()
endif()
enable_testing()
foreach(probe integers empty all_empty unicode long)
  add_test(NAME native_${probe} COMMAND byteforge_btrblocks_probe ${probe})
  set_tests_properties(native_${probe} PROPERTIES TIMEOUT 30)
endforeach()
