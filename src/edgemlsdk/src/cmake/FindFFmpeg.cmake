set(FFmpeg_FOUND FALSE CACHE INTERNAL "FFmpeg_FOUND")

function(ValidateDirectory lib include)
    if( NOT(EXISTS "${lib}/libavcodec.so"
        AND EXISTS "${lib}/libavformat.so"
        AND EXISTS "${lib}/libavutil.so"
        AND EXISTS "${lib}/libswscale.so"))
        return()
    endif()

    if( NOT(EXISTS "${include}/libavcodec"
        AND EXISTS "${include}/libavformat"
        AND EXISTS "${include}/libavutil"
        AND EXISTS "${include}/libswscale"))
        return()
    endif()

    set(FFmpeg_LIBRARIES "${lib}/libavcodec.so" "${lib}/libavformat.so" "${lib}/libavutil.so" "${lib}/libswscale.so" CACHE INTERNAL "FFmpeg_LIBRARIES")
    set(FFmpeg_INCLUDE_DIRS ${include} CACHE INTERNAL "FFmpeg_INCLUDE_DIRS")
    set(FFmpeg_FOUND TRUE CACHE INTERNAL "FFmpeg_FOUND")
    message(STATUS "Found FFmpeg: ${lib}")
endfunction()


if(FFMPEG_INSTALL_DIR)
    ValidateDirectory("${FFMPEG_INSTALL_DIR}/lib" "${FFMPEG_INSTALL_DIR}/include")
else()
    ValidateDirectory("/dependencies/FFmpeg/install/lib" "/dependencies/FFmpeg/install/include")
    if(NOT FFmpeg_FOUND)
        message(STATUS "Could not find compiled version of FFmpeg, using system install")
        ValidateDirectory("/usr/lib/${CMAKE_SYSTEM_PROCESSOR}-linux-gnu" "/usr/include/${CMAKE_SYSTEM_PROCESSOR}-linux-gnu")
    endif()
endif()

if(NOT FFmpeg_FOUND)
    message(FATAL_ERROR "Could not find FFmpeg")
endif()