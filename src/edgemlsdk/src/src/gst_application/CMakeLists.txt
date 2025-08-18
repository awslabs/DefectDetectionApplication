# Edge runtime
add_library(panorama.gstapp_runtime SHARED
    edge_app.cpp
    config/config_v1.cpp)
target_link_libraries(panorama.gstapp_runtime panorama.gst panorama.app)

# Library to load plugins
add_library(panorama.gstapp_plugin_loader SHARED plugin_loader.cpp)
target_link_libraries(panorama.gstapp_plugin_loader panorama.python)

# Gst Application
add_executable(GstApplication main.cpp)
target_link_libraries(GstApplication panorama.gstapp_runtime panorama.gstapp_plugin_loader)

install(TARGETS GstApplication panorama.gstapp_runtime panorama.gstapp_plugin_loader)