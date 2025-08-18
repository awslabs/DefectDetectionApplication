add_compile_definitions(EXPORT)

include_directories(${PROJECT_SOURCE_DIR}/src)
include_directories(${PROJECT_SOURCE_DIR}/src/include)

add_subdirectory(core)
add_subdirectory(aws)
add_subdirectory(app)
add_subdirectory(tools)
add_subdirectory(gst)
add_subdirectory(gst_application)
add_subdirectory(bindings)
add_subdirectory(video_capture)
add_subdirectory(mlops)