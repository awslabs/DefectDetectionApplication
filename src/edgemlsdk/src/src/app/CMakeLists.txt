set(SRC
    app.cpp
    credential_provider.cpp
    mds_credential_provider.cpp
    mds_client.cpp
    mds_property_delegate.cpp)

add_library(panorama.app SHARED ${SRC})
target_link_libraries(panorama.app panorama.core panorama.aws)
install(TARGETS panorama.app)