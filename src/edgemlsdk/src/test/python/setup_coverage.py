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

from coverage import Coverage
import os

PYTHON_DATA_FILE_NAME = 'python_coverage'
PYTHON_COVERAGE_OMIT_FILE_LIST = ['test_utils.py', 'panorama_projections.py', 'setup_coverage.py']

class PythonCoverage:
    def __init__(self, build_dir):
        self.lcov_file_path = os.path.join(build_dir, f'{PYTHON_DATA_FILE_NAME}.info')
        self.cov = Coverage(
            source=[build_dir],
            cover_pylib=False,
            branch=True,
            auto_data=True,
            omit=PYTHON_COVERAGE_OMIT_FILE_LIST
        )
        self.cov.start()

    def stop_coverage(self):
        self.cov.stop()
        self.cov.lcov_report(outfile=self.lcov_file_path)
        