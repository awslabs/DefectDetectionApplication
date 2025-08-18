macro(AddTest)
    set(_OPTIONS_ARGS )
    set(_ONE_VALUE_ARGS NAME DIR MAIN)
    set(_MULTI_VALUE_ARGS DEPENDS ADDFILES)
    cmake_parse_arguments( _ADDTEST "${_OPTIONS_ARGS}" "${_ONE_VALUE_ARGS}" "${_MULTI_VALUE_ARGS}" ${ARGN} )

    if(NOT DEFINED _ADDTEST_DIR)
        set(_ADDTEST_DIR ${CMAKE_CURRENT_SOURCE_DIR})
    endif()

    if(NOT DEFINED _ADDTEST_ADDFILES)
        set(_ADDTEST_ADDFILES)
    endif()

    add_executable(${_ADDTEST_NAME} ${_ADDTEST_DIR}/${_ADDTEST_NAME}.cpp ${_ADDTEST_ADDFILES})
    target_link_libraries(${_ADDTEST_NAME} ${_ADDTEST_DEPENDS} panorama.core GTest::gtest)
    if(DEFINED _ADDTEST_MAIN)
        target_link_libraries(${_ADDTEST_NAME} ${_ADDTEST_MAIN})
    else()
        target_link_libraries(${_ADDTEST_NAME} testmain)
    endif()

    add_test(NAME ${_ADDTEST_NAME} COMMAND ${_ADDTEST_NAME})
    install(TARGETS  ${_ADDTEST_NAME} DESTINATION tests)
endmacro()

macro(AddPythonTest)
    set(_OPTIONS_ARGS )
    set(_ONE_VALUE_ARGS DESTINATION FILE INSTALL_DIR)
    set(_MULTI_VALUE_ARGS )
    cmake_parse_arguments( _ADDPYTHONTEST "${_OPTIONS_ARGS}" "${_ONE_VALUE_ARGS}" "${_MULTI_VALUE_ARGS}" ${ARGN} )

    if(NOT DEFINED _ADDPYTHONTEST_DESTINATION)
        set(_ADDPYTHONTEST_DESTINATION "bin")
    endif()

    if(NOT DEFINED _ADDPYTHONTEST_INSTALL_DIR)
        set(_ADDPYTHONTEST_INSTALL_DIR "tests")
    endif()

    FILECOPY(FILE ${CMAKE_CURRENT_SOURCE_DIR}/python/${_ADDPYTHONTEST_FILE}.py DESTINATION ${CMAKE_BINARY_DIR}/${_ADDPYTHONTEST_DESTINATION})
    install(FILES ${CMAKE_BINARY_DIR}/${_ADDPYTHONTEST_DESTINATION}/${_ADDPYTHONTEST_FILE}.py DESTINATION ${_ADDPYTHONTEST_INSTALL_DIR})
endmacro()

# Don't want to link to debug libraries for python
string(REGEX MATCH ";debug.*" debugLibs "${Python3_LIBRARIES}")
if(NOT "${debugLibs}" STREQUAL "")
    string(REGEX REPLACE "${debugLibs}" "" Python3_LIBRARIES "${Python3_LIBRARIES}")
endif()
string(REGEX REPLACE "optimized" "" Python3_LIBRARIES "${Python3_LIBRARIES}")


# include_directories(${Python3_INCLUDE_DIRS})
include_directories(${CMAKE_CURRENT_SOURCE_DIR})
include_directories(${CMAKE_CURRENT_SOURCE_DIR}/test_with_mock)
include_directories(${PROJECT_SOURCE_DIR}/src)
include_directories(${PROJECT_SOURCE_DIR}/src/include)
include_directories(${Python3_INCLUDE_DIRS})

# include_directories(${CMAKE_BINARY_DIR}/src/core)

# ====== Test Entry =========
add_library(testmain main.cpp)
target_compile_definitions(testmain PUBLIC EXPORT=1)
add_compile_definitions(TRITON_INSTALL_DIR="${TRITON_INSTALL_DIR}")

target_link_libraries(testmain GTest::gtest panorama.core)
install(TARGETS testmain DESTINATION tests)

# add_library(testmain_mock 
#     test_with_mock/main.cpp
#     test_with_mock/test_app_request_handler.cpp)
# target_compile_definitions(testmain_mock PUBLIC EXPORT=1)
# target_link_libraries(testmain_mock GTest::gtest panorama.core panorama.mock_device)
# install(TARGETS testmain_mock DESTINATION tests)

# === localrtspsever===========
# find_library(GST_RTSP_SERVER
#   NAMES
#   gstrtspserver-1.0
#   libgstrtspserver-1.0
#   libgstrtspserver-1.0.so
# )

# add_executable(localrtspserver LocalRtspServer.cpp)
# target_link_libraries(localrtspserver c panorama.gst ${GST_RTSP_SERVER})

# ======= Test Suite ========
# Core Tests
AddTest(NAME com_tests DIR core)
AddTest(NAME trace_tests DIR core)
AddTest(NAME buffer_tests DIR core)
AddTest(NAME properties_tests DIR core)
AddTest(NAME scheduling_tests DIR core)
AddTest(NAME misc_tests DIR core)
AddTest(NAME message_broker_tests DIR core/message_broker DEPENDS panorama.videocapture)
AddTest(NAME file_protocol_tests DIR core/message_broker)
AddTest(NAME gpio_protocol_tests DIR core/message_broker)
# AddTest(NAME http_trace_listener_tests DIR core MAIN testmain_mock)

# Library for the test protocol client
add_library(test_protocol_client SHARED core/message_broker/test_protocol_client.cpp)
target_link_libraries(test_protocol_client panorama.core)
target_link_libraries(message_broker_tests test_protocol_client)

# Device Tests
# AddTest(NAME mds_tests DIR panorama MAIN testmain_mock DEPENDS panorama.mock_device panorama.app)
# AddTest(NAME panorama_app_tests DIR panorama MAIN testmain_mock DEPENDS panorama.app)
AddTest(NAME panorama_stand_alone_tests DIR panorama DEPENDS panorama.app)

# Aws Tests
AddTest(NAME aws_property_delegate_tests DIR aws DEPENDS panorama.aws)
AddTest(NAME aws_variable_expansion_tests DIR aws DEPENDS panorama.aws)
AddTest(NAME mqtt_protocol_client_tests DIR aws DEPENDS panorama.aws)
AddTest(NAME s3_protocol_client_tests DIR aws DEPENDS panorama.aws)

# Gst Tests
AddTest(NAME pipeline_structure_tests DIR gst DEPENDS panorama.gst panorama.app)
AddTest(NAME pipeline_tests DIR gst DEPENDS panorama.gst panorama.app panorama.aws)
AddTest(NAME retry_tests DIR gst DEPENDS panorama.gst panorama.app panorama.aws)
AddTest(NAME pipeline_manager_tests DIR gst DEPENDS panorama.gst panorama.app panorama.aws)
AddTest(NAME gst_elementlatency_tracer_tests DIR gst DEPENDS panorama.gst panorama.app)
AddTest(NAME gst_fps_tracer_tests DIR gst DEPENDS panorama.gst panorama.app)
AddTest(NAME gst_plugin_tests DIR gst DEPENDS panorama.gst panorama.app panorama.triton)
AddTest(NAME payload_metadata_tests DIR gst DEPENDS panorama.gst panorama.app)

# Python Tests
AddTest(NAME python_tests DIR python DEPENDS panorama.python)

# Video Capture Tests
# Disabling for now as there is a periodic seg-fault issue in the implementation
#AddTest(NAME vc_clip_creation_tests DIR videocapture DEPENDS panorama.videocapture panorama.app panorama.gst)
#AddTest(NAME vc_clip_manager_tests DIR videocapture DEPENDS panorama.videocapture)

# MLOps Tests
AddTest(NAME triton_tests DIR mlops DEPENDS panorama.triton)
FILECOPY(DIRECTORY ${CMAKE_CURRENT_SOURCE_DIR}/mlops/test_model DESTINATION ${CMAKE_BINARY_DIR}/bin/model_repo)
FILECOPY(DIRECTORY ${CMAKE_CURRENT_SOURCE_DIR}/mlops/test_model2 DESTINATION ${CMAKE_BINARY_DIR}/bin/model_repo)
FILECOPY(DIRECTORY ${CMAKE_CURRENT_SOURCE_DIR}/mlops/dynamic_model DESTINATION ${CMAKE_BINARY_DIR}/bin/model_repo)
FILECOPY(DIRECTORY ${CMAKE_CURRENT_SOURCE_DIR}/mlops/test_model_dup DESTINATION ${CMAKE_BINARY_DIR}/bin/model_repo)
FILECOPY(DIRECTORY ${CMAKE_CURRENT_SOURCE_DIR}/mlops/ensemble_model DESTINATION ${CMAKE_BINARY_DIR}/bin/model_repo)
FILECOPY(DIRECTORY ${CMAKE_CURRENT_SOURCE_DIR}/mlops/dynamic_model_2 DESTINATION ${CMAKE_BINARY_DIR}/bin/model_repo)

# Copying to model_repo in parallel can cause rsync to fail.  Add dependencies to copy sequentially
add_dependencies(COPY_test_model2 COPY_test_model)
add_dependencies(COPY_dynamic_model COPY_test_model)
add_dependencies(COPY_ensemble_model COPY_dynamic_model)
add_dependencies(COPY_test_model_dup COPY_ensemble_model)
add_dependencies(COPY_dynamic_model_2 COPY_test_model_dup)

# GstApplication Tests
add_library(test_app_plugin SHARED gst_application/test_plugin.cpp)
target_link_libraries(test_app_plugin panorama.gstapp_runtime)
install(TARGETS test_app_plugin test_protocol_client DESTINATION tests)

AddTest(NAME edge_app_tests DIR gst_application DEPENDS panorama.gstapp_runtime)

FILECOPY(FILE ${CMAKE_CURRENT_SOURCE_DIR}/gst_application/test_plugin.py DESTINATION ${CMAKE_BINARY_DIR}/lib)
FILECOPY(FILE ${CMAKE_CURRENT_SOURCE_DIR}/gst_application/test_bad_plugin.py DESTINATION ${CMAKE_BINARY_DIR}/lib)
install(FILES ${CMAKE_CURRENT_SOURCE_DIR}/gst_application/test_plugin.py ${CMAKE_CURRENT_SOURCE_DIR}/gst_application/test_bad_plugin.py DESTINATION tests)
AddTest(NAME app_plugin_tests DIR gst_application DEPENDS panorama.gstapp_runtime panorama.gstapp_plugin_loader)

# ====== Copy Python test scripts to bin directory =======
AddPythonTest(FILE test_utils DESTINATION lib INSTALL_DIR lib)
AddPythonTest(FILE trace_tests)
AddPythonTest(FILE property_tests)
AddPythonTest(FILE device_application_tests)
AddPythonTest(FILE mqtt_protocol_client_tests)
AddPythonTest(FILE s3_protocol_client_tests)
AddPythonTest(FILE file_protocol_client_tests)
AddPythonTest(FILE message_broker_tests)
AddPythonTest(FILE gst_tests)
AddPythonTest(FILE python_protocol_client_tests)
AddPythonTest(FILE sqlite_protocol_client_tests)
AddPythonTest(FILE setup_coverage)
AddPythonTest(FILE query_interface_tests)
AddPythonTest(FILE triton_tests)
AddPythonTest(FILE run_all_tests)

# ====== Add Code Coverage Tool ================]
find_program(LCOV lcov REQUIRED)
find_program(GENHTML genhtml REQUIRED)

FILECOPY(FILE  ${CMAKE_CURRENT_SOURCE_DIR}/gcov_for_clang.sh DESTINATION ${CMAKE_BINARY_DIR})
add_custom_target(coverage
    # gather data for C++ code
    COMMAND ${LCOV} --capture -d ${CMAKE_SOURCE_DIR}/src -d ${CMAKE_SOURCE_DIR}/include -d ${CMAKE_BINARY_DIR} --output-file coverage.info --no-external --gcov-tool ${CMAKE_BINARY_DIR}/gcov_for_clang.sh
    COMMAND ${LCOV} --remove coverage.info '*panorama_projections*' -o coverage.info

    # add python coverage
    COMMAND ${LCOV}  --add-tracefile python_coverage.info --add-tracefile coverage.info --output-file total_coverage.info

    # generate HTML report
    COMMAND ${GENHTML} --demangle-cpp -o coverage total_coverage.info

    # generate text report
    COMMAND ${LCOV} --list total_coverage.info > total_coverage_summary.txt
    WORKING_DIRECTORY ${CMAKE_BINARY_DIR})