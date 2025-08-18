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
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

from sqlalchemy.orm import Session
from sqlalchemy import select

from .models import InputConfiguration
from data_models.common import InputConfigurationsModel


def get_input_cfg(db: Session, input_configuration_id: str):
    return db.get(InputConfiguration, input_configuration_id)


def list_input_cfgs(db: Session):
    return db.scalars(select(InputConfiguration)).all()


def create_input_source_cfg(db: Session, input_configuration: InputConfigurationsModel):
    db_input_configuration = InputConfiguration(**input_configuration)
    db.add(db_input_configuration)
    db.commit()
    db.refresh(db_input_configuration)
    return db_input_configuration
