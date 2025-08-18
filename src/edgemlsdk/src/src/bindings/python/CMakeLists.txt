set(CMAKE_CXX_FLAGS "${CMAKE_CXX_FLAGS} -Wno-delete-non-virtual-dtor -Wno-deprecated-declarations")
add_compile_definitions(EXPORT)

include_directories(${Python3_INCLUDE_DIRS})
include_directories(${SWIG_AUTOGEN_DIR}/python)

add_compile_options(-DSWIG_TYPE_TABLE=PanoramaSDKTypeTable)

# Don't want to link to debug libraries for python
string(REGEX MATCH ";debug.*" debugLibs "${Python3_LIBRARIES}")
if(NOT "${debugLibs}" STREQUAL "")
    string(REGEX REPLACE "${debugLibs}" "" Python3_LIBRARIES "${Python3_LIBRARIES}")
endif()

if(LINUX)
    string(REGEX REPLACE "optimized" "" Python3_LIBRARIES "${Python3_LIBRARIES}")
endif()

# Library used for embedding Python modules into C code
add_library(panorama.python SHARED embed.cpp)
target_link_directories(panorama.python PRIVATE ${_VCPKG_INSTALLED_DIR}/${VCPKG_TARGET_TRIPLET}/lib)
target_link_libraries(panorama.python panorama.core panorama.app panorama.gst panorama.gstapp_runtime panorama.triton ${Python3_LIBRARIES})
if(LINUX)
    target_link_libraries(panorama.python util)
endif()

set(PATCHES
    ${CMAKE_CURRENT_SOURCE_DIR}/autogen_patches/ITraceListener_QueryInterface.txt
    ${CMAKE_CURRENT_SOURCE_DIR}/autogen_patches/IPropertyEventHandler_QueryInterface.txt
    ${CMAKE_CURRENT_SOURCE_DIR}/autogen_patches/IPropertyDelegateEventHandler_QueryInterface.txt
    ${CMAKE_CURRENT_SOURCE_DIR}/autogen_patches/IProtocolClientEventHandler_QueryInterface.txt
    ${CMAKE_CURRENT_SOURCE_DIR}/autogen_patches/IMessageBrokerEventHandler_QueryInterface.txt
    ${CMAKE_CURRENT_SOURCE_DIR}/autogen_patches/IPipelineEventHandler_QueryInterface.txt
    ${CMAKE_CURRENT_SOURCE_DIR}/autogen_patches/IPipelineManagerEventHandler_QueryInterface.txt
    ${CMAKE_CURRENT_SOURCE_DIR}/autogen_patches/IAppPlugin_QueryInterface.txt
    ${CMAKE_CURRENT_SOURCE_DIR}/autogen_patches/IPayload_QueryInterface.txt
    ${CMAKE_CURRENT_SOURCE_DIR}/autogen_patches/IProtocolClient_QueryInterface.txt
    ${CMAKE_CURRENT_SOURCE_DIR}/autogen_patches/IProtocolMessage_QueryInterface.txt
    ${CMAKE_CURRENT_SOURCE_DIR}/autogen_patches/IProtocolSubscription_QueryInterface.txt
    ${CMAKE_CURRENT_SOURCE_DIR}/autogen_patches/IProtocolFactory_QueryInterface.txt
    ${CMAKE_CURRENT_SOURCE_DIR}/autogen_patches/IProtocolFactory_CreateProtocol.txt
    ${CMAKE_CURRENT_SOURCE_DIR}/autogen_patches/IProtocolFactory_CreateMessage.txt
    ${CMAKE_CURRENT_SOURCE_DIR}/autogen_patches/IProtocolMessage_Payload.txt
)

SWIG(
    FILE ${CMAKE_CURRENT_SOURCE_DIR}/../panorama_projections.i
    LANGUAGE python
    DEPENDS panorama.python
    AUTOGEN_PATCH ${PATCHES})

set(publicHeaders "${PROJECT_SOURCE_DIR}/include/Panorama/python.h")
set_target_properties(panorama.python PROPERTIES PUBLIC_HEADER "${publicHeaders}")
install(TARGETS panorama.python PUBLIC_HEADER DESTINATION include/Panorama)

# Copy python package to output directory
FILECOPY(DIRECTORY ${CMAKE_CURRENT_SOURCE_DIR}/python_package DESTINATION ${CMAKE_ARCHIVE_OUTPUT_DIRECTORY})
FILECOPY(FILE ${CMAKE_ARCHIVE_OUTPUT_DIRECTORY}/panorama_projections.py DESTINATION ${CMAKE_ARCHIVE_OUTPUT_DIRECTORY}/python_package/src/panorama)
add_dependencies(COPY_panorama_projections COPY_python_package panorama_projections_python)

FILECOPY(FILE ${CMAKE_ARCHIVE_OUTPUT_DIRECTORY}/_panorama_projections.so DESTINATION ${CMAKE_ARCHIVE_OUTPUT_DIRECTORY}/python_package/src/panorama)
add_dependencies(COPY__panorama_projections panorama_projections_python)
install(DIRECTORY ${CMAKE_ARCHIVE_OUTPUT_DIRECTORY}/python_package DESTINATION ./lib)