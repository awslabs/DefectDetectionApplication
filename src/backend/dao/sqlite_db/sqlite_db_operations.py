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

import os

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker, declarative_base

SQLALCHEMY_DATABASE_URL = "sqlite:///{}/dda_backend_app.db".format(os.environ['COMPONENT_WORK_PATH'])
SQLALCHEMY_METADATA_DATABASE_URL = "sqlite:///{}/dda_backend_metadata.db".format(os.environ['COMPONENT_WORK_PATH'])

# "check_same_thread" is to prevent accidentally sharing the same connection for different requests.
engine = create_engine(
    SQLALCHEMY_DATABASE_URL, connect_args={"check_same_thread": False}
)
metadata_engine = create_engine(SQLALCHEMY_METADATA_DATABASE_URL)

Base = declarative_base()
BaseMetadata = declarative_base()

## Sessionmaker is used to initialize session.
## Session is a mutable, stateful object that represents a single database transaction.
##
## Concept 
## session.commit: auto commits (persists) those changes to the database.
## session.flush: communicates operations to the database, but aren't persisted permanently to disk yet
## If autocommit set to true, will auto flush automatically
##
# All configuration operations will be on engine for dda_backend_app
# All metadata(inference results) operations will be on metadata_engine            
SessionLocal = sessionmaker(autocommit=False, autoflush=False, binds={Base: engine, BaseMetadata: metadata_engine})
