cmake_minimum_required (VERSION 3.14)

if(NOT DEFINED PKG_VERSION)
  set(PKG_VERSION "0.0.0")
endif()

project(PanoramaSDK VERSION ${PKG_VERSION})

set(CMAKE_CXX_STANDARD 17)
set(CMAKE_CXX_STANDARD_REQUIRED ON)
set(CMAKE_VERBOSE_MAKEFILE OFF)
set(CMAKE_ARCHIVE_OUTPUT_DIRECTORY ${CMAKE_BINARY_DIR}/lib)
set(CMAKE_LIBRARY_OUTPUT_DIRECTORY ${CMAKE_BINARY_DIR}/lib)
set(CMAKE_RUNTIME_OUTPUT_DIRECTORY ${CMAKE_BINARY_DIR}/bin)
set(CMAKE_MODULE_PATH ${PROJECT_SOURCE_DIR}/cmake)
set(CMAKE_POSITION_INDEPENDENT_CODE ON)
set(CMAKE_INSTALL_PREFIX ${CMAKE_BINARY_DIR}/install)
set(SWIG_AUTOGEN_DIR ${CMAKE_BINARY_DIR}/src/bindings)

# Add coverage flags for Debug build
if(${CMAKE_BUILD_TYPE} STREQUAL "Debug" AND ${RUN_COVERAGE} )
  set(CMAKE_CXX_FLAGS "-Werror -O0 -coverage")
else()
  set(CMAKE_CXX_FLAGS "-Werror")
endif()

if (NOT "${MEMORY_CHECK_FLAGS}" STREQUAL "")
  set(CMAKE_CXX_FLAGS "${CMAKE_CXX_FLAGS} ${MEMORY_CHECK_FLAGS}")
  set(CMAKE_EXE_LINKER_FLAGS "${CMAKE_EXE_LINKER_FLAGS} ${MEMORY_CHECK_FLAGS}")
endif()

include(filecopy)

# Steps needed to get files in order for publishing stage
# Not packaging from CMake because of how our pipeline is set up
# Packing is done in a later stage post Build and Test.  So we need these files
# In the install directory
add_custom_target(write_version ALL
    COMMAND ${CMAKE_COMMAND} -E echo "${PKG_VERSION}" > ${CMAKE_BINARY_DIR}/version
    WORKING_DIRECTORY ${CMAKE_BINARY_DIR}
    COMMENT "Writing version file"
)

install(FILES ${CMAKE_CURRENT_SOURCE_DIR}/utilities/create_debian.sh DESTINATION ./)
install(FILES ${CMAKE_CURRENT_SOURCE_DIR}/utilities/create_whl.ps1 DESTINATION ./)
install(FILES ${CMAKE_CURRENT_SOURCE_DIR}/utilities/publish.sh DESTINATION ./)
install(FILES ${CMAKE_BINARY_DIR}/version DESTINATION ./)

#set(SWIG_AUTOGEN_DIR ${CMAKE_BINARY_DIR}/src/bindings)
# Preprocessor Definitions
add_compile_definitions(BUILD_DIR="${CMAKE_BINARY_DIR}")
add_compile_definitions(__SDK_VERSION__="${PKG_VERSION}")
add_compile_definitions(__SDK_MAJOR_MINOR_VERSION__="${MAJOR_MINOR}")
add_compile_definitions(GST_USE_UNSTABLE_API="1")

if(${CMAKE_CXX_COMPILER_VERSION} VERSION_GREATER_EQUAL 8.0)
  add_compile_definitions(GXX_8)
endif()

# Required packages
find_package(GTest CONFIG REQUIRED)
find_package(nlohmann_json CONFIG REQUIRED)
find_package(civetweb CONFIG REQUIRED)
find_package(CURL)
find_package(PkgConfig)
find_package(FFmpeg)
find_package(Triton)

# libexif manual find
find_library(LIBEXIF_LIBRARY
    NAMES exif
)
set(LIBEXIF_INCLUDE_DIRS /usr/include/libexif)

if(NOT LIBEXIF_LIBRARY)
  message(FATAL_ERROR "Libexif not found")
else()
  message(STATUS "Libexif = ${LIBEXIF_LIBRARY}")
endif()

message(STATUS "Python Version = ${PYTHON_VERSION}")
find_package(Python3 ${PYTHON_VERSION} EXACT COMPONENTS Interpreter Development REQUIRED)

pkg_search_module(GLIB REQUIRED glib-2.0) 
pkg_check_modules(GSTREAMER REQUIRED gstreamer-1.0)
pkg_check_modules(ARAVIS REQUIRED aravis-0.8)

# Include Directories
include_directories(${PROJECT_SOURCE_DIR}/include)
include_directories(${nlohmann_json_INCLUDE_DIR})
include_directories(${civetweb_INCLUDE_DIR})
include_directories(${GSTREAMER_INCLUDE_DIRS})
include_directories(${ARAVIS_INCLUDE_DIRS})
include_directories(${LIBEXIF_INCLUDE_DIRS})
# Code Directories
add_subdirectory(src)
add_subdirectory(docs)

enable_testing()
add_subdirectory(test)