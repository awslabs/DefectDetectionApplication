#
#  Copyright 2025 Amazon Web Services, Inc.
#
#  Licensed under the Apache License, Version 2.0 (the "License");
#  you may not use this file except in compliance with the License.
#   You may obtain a copy of the License at
#
#       http://www.apache.org/licenses/LICENSE-2.0
#
#   Unless required by applicable law or agreed to in writing, software
#   distributed under the License is distributed on an "AS IS" BASIS,
#   WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
#  See the License for the specific language governing permissions and
#  limitations under the License.
#

import os
import sys
import subprocess
import json
import platform
import re
import shutil
from datetime import datetime
from six import StringIO

from conan import ConanFile, tools
from conan.tools.cmake import CMake, cmake_layout, CMakeToolchain
from conan.tools.files import copy
from conan.tools.env.environment import Environment

MEMORY_SANITIZER_MAP = {
    "address": "-fsanitize=address -O1 -fno-omit-frame-pointer -g",
    "thread": "-fsanitize=thread -O2 -g",
}

def get_current_date_as_string():
    current_date = datetime.now()
    return current_date.strftime('%Y%m%d')

def get_ubuntu_version():
    try:
        result = subprocess.check_output(['lsb_release', '-a'], universal_newlines=True)
        for line in result.split("\n"):
            if "Description:" in line:
                match = re.search(r'(\d+\.\d+)', line)
                if match:
                    return match.group(1)
    except:
        return "Not an Ubuntu system or lsb_release not found"

class PanoramaSDK(ConanFile):
    name = "edgeml_sdk"

    # If you change this you MUST also change the CDK pipeline
    version = "1.0"
    min_coverage_percent = 80.0

    # Binary configuration
    settings = "os", "compiler", "build_type", "arch"
    options = {
        "shared": [True, False],
        "run_tests": [True, False],
        "build_docs": [True, False],
        "run_coverage": [True, False],
        "sanitizer": ["", "address", "thread"],
        "publish_docker_image":[True, False],
        "triton_install": ["ANY"]
    }

    default_options = {
        "shared": True,
        "run_tests": False,
        "build_docs": False,
        "run_coverage": False,
        "sanitizer": "",
        "publish_docker_image":False,
        "triton_install": ""
    }

    exports_sources = "*"
    generators = "CMakeDeps"

    def generate(self):
        tc = CMakeToolchain(self)
        tc.variables["CMAKE_C_COMPILER"] = "clang"
        tc.variables["CMAKE_CXX_COMPILER"] = "clang++"
        tc.generate()

    def requirements(self):
        self.requires("gtest/cci.20210126")
        self.requires("nlohmann_json/3.11.3")
        self.requires("zlib/1.2.13")
        self.requires("yaml-cpp/0.7.0")

        version = get_ubuntu_version()
        if version == "20.04" or version == "18.04":
            self.requires("civetweb/1.15")
        elif version == "22.04":
            self.requires("civetweb/1.16")
        else:
            raise Exception("Not a supported OS")

    def layout(self):
        cmake_layout(self)

    def build(self):
        major, minor, _ = sys.version_info[:3]
        definitions = {
            "PYTHON_VERSION": f"{major}.{minor}",
            "MAJOR_MINOR": self.version, 
            "PKG_VERSION": f'{self.version}.{get_current_date_as_string()}',
            "BUILD_DIR": os.getcwd(),
            "MEMORY_CHECK_FLAGS": MEMORY_SANITIZER_MAP.get(str(self.options.sanitizer), ""),
            "RUN_COVERAGE": self.options.run_coverage
        }

        if len(str(self.options.triton_install)) > 0:
            definitions["TRITON_INSTALL_DIR"] = self.options.triton_install

        # Build the documents if the option is specified
        # Otherwise only build on 20.04, x86_64, Python 3.9 build
        if self.options.build_docs:
            pass
        else:
            ubuntu = os.environ.get('UBUNTU_VERSION')
            if not (ubuntu == "20.04" and platform.machine() == "x86_64" and sys.version_info.minor == 8):
                definitions["SKIP_DOCS"] = "1"

        cmake = CMake(self)
        cmake.configure(definitions)
        cmake.build()

        if self.options.run_tests:
            try:
                cmake.test()
            except Exception:
                if self.options.sanitizer != '':
                    self.parse_memory_check_results()
                else:
                    raise
            if self.settings.build_type == 'Debug' and self.options.run_coverage:
                self.run_coverage()

        if self.options.publish_docker_image:
            self.publish_docker_image()
            
    def run_coverage(self):
        self.run(f'make coverage')

        percent_buf = StringIO()
        self.run("tail -1 total_coverage_summary.txt | awk -F'|' '/Total:/ {print $2}' | awk '{print $1}' | tr -d '%'", stdout=percent_buf)
        percent_cov = percent_buf.getvalue()
        self.output.info(f"Current Overall Unit Test Percent Coverage is: {percent_cov}")
        if not float(percent_cov) >= self.min_coverage_percent:
            raise Exception(f"Current overall unit test coverage percent, {percent_cov}%, is lower than minimum percent, {self.min_coverage_percent}%")

    def parse_memory_check_results(self):
        shutil.copyfile(f"Testing/Temporary/LastTest.log", f'{self.options.sanitizer}_sanitizer_LastTest.log')
        shutil.copyfile(f"Testing/Temporary/LastTestsFailed.log", f'{self.options.sanitizer}_sanitizer_LastTestsFailed.log')
        self.output.error(f"Memory checkers found errors. See {os.getcwd()}/{self.options.sanitizer}_sanitizer_LastTest.log for logs.")

    def publish_docker_image(self):
        # self.run(f'Publish docker image')
        self.run(f"../../docker/build_image/deploy.sh")
        
    def package(self):
        cmake = CMake(self)
        cmake.install()

    def deploy(self):
        copy(self, "*", dst=".", src=self.package_folder)