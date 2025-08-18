set(Triton_FOUND FALSE CACHE INTERNAL "FFmpeg_FOUND")

function(ValidateDirectory lib include)
    if(NOT EXISTS "${lib}/libtritonserver.so")
        return()
    endif()

    file(GLOB INC_DIRECTORY_EXIST "${include}")
    if(NOT INC_DIRECTORY_EXIST)
        return()
    endif()

    set(Triton_LIBRARIES "${lib}/libtritonserver.so" CACHE INTERNAL "Triton_LIBRARIES")
    set(Triton_INCLUDE_DIRS ${include} CACHE INTERNAL "Triton_INCLUDE_DIRS")
    set(Triton_FOUND TRUE CACHE INTERNAL "Triton_FOUND")
    message(STATUS "Found Triton: ${lib}")
endfunction()


if(TRITON_INSTALL_DIR)
    ValidateDirectory("${TRITON_INSTALL_DIR}/lib" "${TRITON_INSTALL_DIR}/include")
else()
    ValidateDirectory("/dependencies/server/build/tritonserver/install/lib" "/dependencies/server/build/tritonserver/install/include")
    set(TRITON_INSTALL_DIR "/dependencies/server/build/tritonserver/install")
endif()

if(NOT ${Triton_FOUND})
    message(FATAL_ERROR "Could not find Triton")
endif()