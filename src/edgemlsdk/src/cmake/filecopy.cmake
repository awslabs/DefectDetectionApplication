function(FileCopy)
    cmake_parse_arguments("" "" "FILE;DIRECTORY;DESTINATION;TARGET_NAME" "" ${ARGN})

    if(NOT DEFINED _FILE AND NOT DEFINED _DIRECTORY)
        message(FATAL_ERROR "File or Directory must be specified")
    endif()

    if(DEFINED _TARGET_NAME)
        set(TARGET_NAME  ${_TARGET_NAME})
    endif()

    if(DEFINED _FILE)
        set(CP "cp")
        set(INPUT_ARTIFACT ${_FILE})
        
        if(NOT DEFINED _TARGET_NAME)
            get_filename_component(TARGET_NAME ${_FILE} NAME_WE)
        endif()

        get_filename_component(FILE_NAME ${_FILE} NAME)
    elseif(DEFINED _DIRECTORY)
        set(CP "rsync")
        set(INPUT_ARTIFACT ${_DIRECTORY})

        if(NOT DEFINED _TARGET_NAME)
            get_filename_component(TARGET_NAME ${_DIRECTORY} NAME_WE)
        endif()

        get_filename_component(FILE_NAME ${_DIRECTORY} NAME)
    endif()

    if(NOT DEFINED _DESTINATION)
        message(FATAL_ERROR "Destination directory must be specified")
    endif()

    if(NOT DEFINED _DIRECTORY)
        add_custom_command(
            OUTPUT ${_DESTINATION}/${FILE_NAME}
            COMMAND ${CP} ${INPUT_ARTIFACT} ${_DESTINATION}
            DEPENDS ${INPUT_ARTIFACT})
            
        add_custom_target(COPY_${TARGET_NAME} ALL
            DEPENDS ${_DESTINATION}/${FILE_NAME})
    else()
        # Run every time but use rsync, 
        # CMake struggles with keeping these up to date if internal files have changed
        add_custom_target(COPY_${TARGET_NAME} ALL
                COMMAND ${CP} "-av" ${INPUT_ARTIFACT} ${_DESTINATION})
    endif()
endfunction()