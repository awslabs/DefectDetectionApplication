# Building EdgeML-SDK
## Docker


2. Run the build image
    ```bash
    ./docker/build_image/run.sh -u [18.04,20.04]
    ```

3. Build and test code
    ```bash
    cd /code
    ./run_build.sh
    ```

    Optional args for ./run_build.sh:
    - `-c`: run tests and unit test coverage
    - `-a`: run static code analysis via Coverity and Pylint
    - `-m`: run C++ memory checkers via google analyzers

    Alternatively, you can run conan build manually. See **Building Code, Running Tests and Generating Code Coverage Reports**

## To Generate Release Artifacts
0. Create a debian file
    ```
    cd /code/build/Release/install/
    chmod +x ./create_debian.sh
    ./create_debian.sh
    ```
1. create python wheel file 
    ```
    pwsh ./create_whl.ps1 -InstallDirectory ./lib -Version 1.0
    ```
    generated wheel file will be at `/code/build/Release/install/lib/python_package/dist`

## Push Updated Docker image to ECR Part of Build Process
This step will build and push docker images(ubuntu 20.04, 18.04) correspodning to docker container platform (x86 or aarch64)

0. Make sure AWS Tokens for account 691462484548 are valid before running below steps
1. Runing below command will build docker image part of build process and publishes to corresponding ECR repositiory
```
conan build . --build=missing -o publish_docker_image=True
```

3. Build the code
    ```bash
    conan build . --build=missing
    ```

## Building Code, Running Tests and Generating Code Coverage Reports
1. To Build code run:
    ```
    cd /code
    conan build . --build=missing
    cd build/Release
    make test
    ```
1. To Run tests after build, set run_tests option to True:
    ```
    conan build . --build=missing -o run_tests=True
    ```
2. To Generate Code Coverage Reports, do a Debug build and set run_coverage and run_tests options to True:
    ```
    conan build . --build=missing -s build_type=Debug -o run_tests=True -o run_coverage=True
    ```
    1. Code coverage text report is written to ./build/Debug/coverage_summary.txt
### Viewing HTML code coverage report:
The HTML files for the coverage report are generated in <>/EdgeML-SDK/build/Debug/coverage.
Open <>/EdgeML-SDK/build/Debug/coverage/index.html in a browser.


