macro(AddGstElement)
    set(_OPTIONS_ARGS )
    set(_ONE_VALUE_ARGS NAME DIR)
    set(_MULTI_VALUE_ARGS DEPENDS)
    cmake_parse_arguments( _ARGS "${_OPTIONS_ARGS}" "${_ONE_VALUE_ARGS}" "${_MULTI_VALUE_ARGS}" ${ARGN} )

    if(NOT DEFINED _ARGS_NAME)
        message( "GstElement name needs to be provided")
    endif()

    if(NOT DEFINED _ARGS_DIR)
        message( "Directory needs to be provided")
    endif()

    add_library(gst${_ARGS_NAME} SHARED ${_ARGS_DIR}/${_ARGS_NAME}.cpp)
    target_link_libraries(gst${_ARGS_NAME} panorama.gst panorama.app)

    if(DEFINED _ARGS_DEPENDS)
        target_link_libraries(gst${_ARGS_NAME} ${_ARGS_DEPENDS})
    endif()

    install(TARGETS gst${_ARGS_NAME} DESTINATION lib/panoramagst)
endmacro()

add_compile_definitions(EXPORT)
add_library(panorama.gst SHARED
    gst.cpp
    meta.cpp
    gstrunner/pipeline_event_handler.cpp
    gstrunner/pipeline.cpp
    gstrunner/pipeline_definition.cpp
    gstrunner/pipeline_error.cpp
    gstrunner/pipeline_manager_event_handler.cpp
    gstrunner/pipeline_manager.cpp
    gstrunner/pipeline_structure.cpp
    gstrunner/retry_mechanism.cpp
    gstrunner/variable_expansion_factory.cpp
    )
target_link_libraries(panorama.gst panorama.core gstreamer-1.0 glib-2.0 gobject-2.0 gstvideo-1.0 gstbase-1.0)
install(TARGETS panorama.gst)

AddGstElement(NAME elementlatency DIR tracers)
AddGstElement(NAME fps DIR tracers)

AddGstElement(NAME emexifextract DIR plugins DEPENDS ${LIBEXIF_LIBRARY})
AddGstElement(NAME emlcapture DIR plugins)
AddGstElement(NAME emlfilesrc DIR plugins)
AddGstElement(NAME gate DIR plugins)
AddGstElement(NAME genicamsrc DIR plugins DEPENDS aravis-0.8)
AddGstElement(NAME emlvideocapture DIR plugins DEPENDS panorama.videocapture)
AddGstElement(NAME emltriton DIR plugins DEPENDS panorama.triton)
