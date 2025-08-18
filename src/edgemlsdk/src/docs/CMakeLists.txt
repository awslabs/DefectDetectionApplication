if(NOT DEFINED SKIP_DOCS)
    message(STATUS "Generating documentation")
    find_package(Doxygen)
    find_package(Sphinx)
    if (NOT (DOXYGEN_FOUND AND SPHINX_FOUND))
        message(FATAL_ERROR "Documentation: Doxygen and/or Sphinx have not been installed")
    endif()

    file(GLOB_RECURSE PUBLIC_HEADERS ${PROJECT_SOURCE_DIR}/include/Panorama/*.h)

    # set input and output files
    set(DOXYFILE_IN ${PROJECT_SOURCE_DIR}/docs/Doxyfile.in)
    set(DOXYFILE_OUT ${CMAKE_BINARY_DIR}/docs/Doxyfile)

    # Preprocess the header files because doxygen struggles with it
    set(PREPROCESSED_HEADER ${CMAKE_BINARY_DIR}/docs/preprocessed/preprocessed.h)
    add_custom_command(
        OUTPUT ${PREPROCESSED_HEADER}
        COMMAND mkdir -p ${CMAKE_BINARY_DIR}/docs/preprocessed
        COMMAND mkdir -p ${CMAKE_BINARY_DIR}/docs/sphinx/_static
        COMMAND cp ${CMAKE_CURRENT_SOURCE_DIR}/custom.css ${CMAKE_BINARY_DIR}/docs/sphinx/_static
        COMMAND g++ -std=c++11 -I${PROJECT_SOURCE_DIR}/include -E -C ${CMAKE_CURRENT_SOURCE_DIR}/include_all.h -o ${PREPROCESSED_HEADER}
        COMMENT "Preprocessing header files..."
        WORKING_DIRECTORY ${PROJECT_SOURCE_DIR}
        VERBATIM
    )

    # request to configure the file
    configure_file(${DOXYFILE_IN} ${DOXYFILE_OUT} @ONLY)

    set(DOXYGEN_OUTPUT_DIR ${CMAKE_CURRENT_BINARY_DIR}/doxygen)
    set(DOXYGEN_INDEX_FILE ${DOXYGEN_OUTPUT_DIR}/xml/index.xml)
    set(DOXYGEN_LOGS ${CMAKE_CURRENT_BINARY_DIR}/doxygen/logs.txt)
    add_custom_command(
        OUTPUT ${DOXYGEN_INDEX_FILE}
        DEPENDS ${PUBLIC_HEADERS} ${PREPROCESSED_HEADER}
        COMMAND mkdir -p ${DOXYGEN_OUTPUT_DIR}
        COMMAND ${DOXYGEN_EXECUTABLE} ${DOXYFILE_OUT} > ${DOXYGEN_LOGS} 2>&1
        MAIN_DEPENDENCY ${DOXYFILE_OUT} ${DOXYFILE_IN}
        COMMENT "Documentation: Generating C++ xml with Doxygen"
        WORKING_DIRECTORY ${PROJECT_SOURCE_DIR}
        VERBATIM)

    set(SPHINX_SOURCE_DIR ${CMAKE_CURRENT_SOURCE_DIR}/source)
    file(GLOB_RECURSE SPHINX_SOURCE_FILES ${SPHINX_SOURCE_DIR}/*)
    file(GLOB_RECURSE PYTHON_SOURCE_FILES ${PROJECT_SOURCE_DIR}/src/bindings/python/python_package/src/panorama/*.py)
    set(SPHINX_INDEX_FILE ${CMAKE_CURRENT_BINARY_DIR}/sphinx/index.html)
    add_custom_command(
        OUTPUT  ${SPHINX_INDEX_FILE}
        COMMAND  ${SPHINX_EXECUTABLE} -b html -Dbreathe_projects.C++API=${DOXYGEN_OUTPUT_DIR}/xml ${SPHINX_SOURCE_DIR} ${CMAKE_BINARY_DIR}/docs/sphinx
        WORKING_DIRECTORY ${CMAKE_CURRENT_BINARY_DIR}
        COMMENT "Documentation: Generating with Sphinx"
        DEPENDS ${DOXYGEN_INDEX_FILE} ${SPHINX_SOURCE_FILES} ${PYTHON_SOURCE_FILES})

    add_custom_target(Sphinx ALL DEPENDS ${SPHINX_INDEX_FILE})
    add_dependencies(Sphinx panorama_projections_python)

    install(DIRECTORY ${CMAKE_BINARY_DIR}/docs/sphinx DESTINATION ./)
endif()

FILECOPY(DIRECTORY ${CMAKE_CURRENT_SOURCE_DIR}/samples/Python DESTINATION ${CMAKE_BINARY_DIR}/bin/samples)
add_subdirectory(samples/C++/MessageBroker)
add_subdirectory(samples/C++/Plugins)
#add_subdirectory(samples/C++/Gate)