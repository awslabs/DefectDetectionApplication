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

from .models import OutputConfiguration
from data_models.common import OutputConfigurationsModel


def get_output_cfg(db: Session, output_configuration_id: str):
    return db.get(OutputConfiguration, output_configuration_id)


def list_output_cfgs(db: Session):
    return db.scalars(select(OutputConfiguration)).all()


def create_output_source_cfg(db: Session, output_configuration: OutputConfigurationsModel):
    db_output_configuration = OutputConfiguration(**output_configuration)
    db.add(db_output_configuration)
    db.commit()
    db.refresh(db_output_configuration)
    return db_output_configuration
