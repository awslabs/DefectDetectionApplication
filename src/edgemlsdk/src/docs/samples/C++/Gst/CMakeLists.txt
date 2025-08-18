add_executable(Gst_SimplePipeline simple_pipeline.cpp)
set_target_properties(Gst_SimplePipeline PROPERTIES RUNTIME_OUTPUT_DIRECTORY "${CMAKE_BINARY_DIR}/bin/samples")
target_link_libraries(Gst_SimplePipeline panorama.gst panorama.app)

add_executable(Gst_PipelineVariables variables_pipeline.cpp)
set_target_properties(Gst_PipelineVariables PROPERTIES RUNTIME_OUTPUT_DIRECTORY "${CMAKE_BINARY_DIR}/bin/samples")
target_link_libraries(Gst_PipelineVariables panorama.gst panorama.app)

add_executable(Gst_PipelineManager pipeline_manager.cpp)
set_target_properties(Gst_PipelineManager PROPERTIES RUNTIME_OUTPUT_DIRECTORY "${CMAKE_BINARY_DIR}/bin/samples")
target_link_libraries(Gst_PipelineManager panorama.gst panorama.app)