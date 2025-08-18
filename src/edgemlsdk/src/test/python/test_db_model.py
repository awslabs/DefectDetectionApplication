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

from sqlalchemy import create_engine, Column, Integer, String, MetaData
from sqlalchemy.orm import sessionmaker, declarative_base
from marshmallow import Schema, fields
from panorama import trace

DB_FILE="/tmp/test.db"
Base = declarative_base()
class User(Base):
    __tablename__ = 'users'
    id = Column(Integer, primary_key=True)
    username = Column(String, unique=True, nullable=False)

class UserSchema(Schema):
    id = fields.Integer(dump_only=True)
    username = fields.String(required=True)

# TODO: use :memory:, need to make Session singleton
engine = create_engine('sqlite:///{}'.format(DB_FILE)) 
Base.metadata.create_all(engine)
Session = sessionmaker(bind=engine)
