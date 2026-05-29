find_program(Swig_EXE NAMES swig swig4.0 PATHS /usr/local/bin /usr/bin NO_DEFAULT_PATH)
find_program(Swig_EXE NAMES swig swig4.0)

if(NOT Swig_EXE)
    message(FATAL_ERROR "Could not find swig")
else()
    message(STATUS "Found Swig: ${Swig_EXE}")
    set(Swig_FOUND true)
endif()