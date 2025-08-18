add_executable(MqttProtocolSample mqtt.cpp)
set_target_properties(MqttProtocolSample PROPERTIES RUNTIME_OUTPUT_DIRECTORY "${CMAKE_BINARY_DIR}/bin/samples/")
target_link_libraries(MqttProtocolSample panorama.aws)

add_executable(S3ProtocolSample s3.cpp)
set_target_properties(S3ProtocolSample PROPERTIES RUNTIME_OUTPUT_DIRECTORY "${CMAKE_BINARY_DIR}/bin/samples/")
target_link_libraries(S3ProtocolSample panorama.aws)

add_executable(MessageBrokerSample message_broker_sample.cpp)
set_target_properties(MessageBrokerSample PROPERTIES RUNTIME_OUTPUT_DIRECTORY "${CMAKE_BINARY_DIR}/bin/samples/")
target_link_libraries(MessageBrokerSample panorama.aws)

add_executable(FileProtocolClientSample file.cpp)
set_target_properties(FileProtocolClientSample PROPERTIES RUNTIME_OUTPUT_DIRECTORY "${CMAKE_BINARY_DIR}/bin/samples/")
target_link_libraries(FileProtocolClientSample panorama.core)

