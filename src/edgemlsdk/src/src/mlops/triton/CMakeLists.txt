# To build triton server: python3 build.py --enable-logging --no-container-build --extra-core-cmake-arg=TRITON_ENABLE_ENSEMBLE=ON --build-dir=`pwd`/build
add_library(panorama.triton SHARED 
    tensor.cpp
    triton_server.cpp
    triton_cpu_request.cpp)

target_include_directories(panorama.triton PUBLIC ${Triton_INCLUDE_DIRS})

target_link_libraries(panorama.triton PRIVATE panorama.core ${Triton_LIBRARIES})
install(TARGETS panorama.triton)