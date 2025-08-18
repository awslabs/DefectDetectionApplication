set(SWIG_BIN_DIR "/usr/local/bin")

if(EXISTS "${SWIG_BIN_DIR}/swig")
    set(Swig_EXE "${SWIG_BIN_DIR}/swig")
endif()

if(NOT DEFINED Swig_EXE)
    message(FATAL_ERROR "Could not find ${SWIG_BIN_DIR}/swig")
else()
    message(STATUS "Found Swig: ${Swig_EXE}")
    set(Swig_FOUND true)
endif()