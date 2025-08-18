add_executable(EmlCaptureSample emlcapture_sample.cpp)
set_target_properties(EmlCaptureSample PROPERTIES RUNTIME_OUTPUT_DIRECTORY "${CMAKE_BINARY_DIR}/bin/samples/")
target_link_libraries(EmlCaptureSample panorama.gst panorama.app)