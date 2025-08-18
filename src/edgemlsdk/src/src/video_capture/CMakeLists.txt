set(SRC
    ffmpeg_video_clip.cpp
    h264_encoding_handler.cpp
    h264_packet.cpp
    video_capture.cpp
    video_capture_event_handler.cpp
    video_clip_collection.cpp
    video_payload.cpp)

if("$ENV{UBUNTU_VERSION}" MATCHES "18.04")
    add_compile_definitions(OLDER_FFMPEG="1")
    message(STATUS "Using an older version (3.4.11) of FFmpeg")
endif()

add_library(panorama.videocapture SHARED ${SRC})
include_directories(${FFmpeg_INCLUDE_DIRS})
target_link_libraries(panorama.videocapture ${FFmpeg_LIBRARIES})
install(TARGETS panorama.videocapture)
