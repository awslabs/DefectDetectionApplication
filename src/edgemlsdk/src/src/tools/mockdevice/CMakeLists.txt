set(SRC
    mock_panorama_device.cpp
    mock_panorama_device_credential_provider.cpp)

add_library(panorama.mock_device SHARED ${SRC})
target_link_libraries(panorama.mock_device panorama.core panorama.aws civetweb::civetweb)
install(TARGETS panorama.mock_device)

add_executable(MockPanoramaDevice 
    app/main.cpp
    app/app_request_handler.cpp)
target_link_libraries(MockPanoramaDevice panorama.mock_device)
install(TARGETS MockPanoramaDevice)