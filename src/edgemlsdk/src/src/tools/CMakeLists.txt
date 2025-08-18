# add_subdirectory(mockdevice)

add_executable(VideoCaptureTool capture_tool.cpp)
target_link_libraries(VideoCaptureTool panorama.videocapture panorama.gst panorama.app)
install(TARGETS VideoCaptureTool)