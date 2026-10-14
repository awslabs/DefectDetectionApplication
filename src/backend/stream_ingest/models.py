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
"""SQLAlchemy model for device-level settings (additive only).

``device_settings`` lives in the configuration database and is created by
the alembic migration ``b8d4f0a26c57_create_device_settings`` in
``alembic/configuration_database/versions``. It holds one row per setting;
an absent row means the default (``stream_ingest/settings.py``). No existing
table is modified.
"""
from sqlalchemy import JSON, Column, Integer, String

from dao.sqlite_db.sqlite_db_operations import Base


class DeviceSetting(Base):
    """One device-level setting, such as ``streamIngest.maxSessions``."""

    __tablename__ = "device_settings"

    key = Column(String, primary_key=True)
    value = Column(JSON, nullable=True)
    updated_at = Column(Integer, nullable=False)
