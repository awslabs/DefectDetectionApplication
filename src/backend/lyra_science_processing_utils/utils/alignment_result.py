#  #
#   Copyright  Amazon Web Services, Inc.
#  #
#   Licensed under the Apache License, Version 2.0 (the "License");
#   you may not use this file except in compliance with the License.
#    You may obtain a copy of the License at
#  #
#        http://www.apache.org/licenses/LICENSE-2.0
#  #
#    Unless required by applicable law or agreed to in writing, software
#    distributed under the License is distributed on an "AS IS" BASIS,
#    WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
#   See the License for the specific language governing permissions and
#   limitations under the License.
#  #
#  #
#  Licensed under the Apache License, Version 2.0 (the "License");
#  you may not use this file except in compliance with the License.
#  You may obtain a copy of the License at
#  #
#      http://www.apache.org/licenses/LICENSE-2.0
#  #
#  Unless required by applicable law or agreed to in writing, software
#  distributed under the License is distributed on an "AS IS" BASIS,
#  WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
#  See the License for the specific language governing permissions and
#  limitations under the License.
import numpy as np
from typing import Dict

from lyra_science_processing_utils.utils.serializable import Serializable


class AlignmentResult(Serializable):
    """
    This class represents result of alignment for one object.
    """

    def __init__(self, transform: np.ndarray):
        """
        :param transform: An np.ndarray representing the transformation matrix.
        """
        self.transform = transform

    def serialize(self) -> Dict:
        """
        Returns a version of this object that is JSON serializable
        """
        return {"transform": self.transform.tolist()}

    @classmethod
    def deserialize(cls, data: Dict):
        """
        Returns an instance of this object from the data given
        :param data: the data to populate the class
        """
        return cls(np.array(data['transform']))
