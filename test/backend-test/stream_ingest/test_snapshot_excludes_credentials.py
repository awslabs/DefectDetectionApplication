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
"""The diagnostic snapshot never carries the Credential_Store
(rtsp-rtmp-stream-cameras task 15.5 — Requirements 6.1, 6.2).

``snapshot/snapshot.sh`` tars the LocalServer work path, which holds the
Credential_Store (``stream_credentials/credentials.json``). The script's
own LocalServer ``tar`` command is run, with a real ``tar``, over a temporary
tree laid out like a device's work path: the store must be absent from the
archive and everything else present.
"""
import os
import shlex
import shutil
import subprocess
import tarfile

import pytest

from stream_ingest.credentials import CredentialStore

_REPO_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..", ".."))
_SNAPSHOT_SCRIPT = os.path.join(_REPO_ROOT, "src", "backend", "snapshot", "snapshot.sh")
_LOCAL_SERVER_WORK_PATH = "/aws_dda/greengrass/v2/work/aws.edgeml.dda.LocalServer/"
SECRET = "pw-SNAPSHOT-60b3"


def _local_server_tar_command():
    """The tokens of the script's tar command over the LocalServer work path."""
    with open(_SNAPSHOT_SCRIPT, encoding="utf-8") as handle:
        commands = [shlex.split(line) for line in handle
                    if line.strip().startswith("tar ") and _LOCAL_SERVER_WORK_PATH in line]
    assert len(commands) == 1, "expected one tar command over the LocalServer work path"
    return commands[0]


def test_the_script_excludes_the_credential_store():
    assert "--exclude=stream_credentials" in _local_server_tar_command()


@pytest.mark.skipif(shutil.which("tar") is None, reason="no tar on this host")
def test_the_archive_holds_the_work_path_without_the_store(tmp_path):
    work_path = tmp_path / "aws.edgeml.dda.LocalServer"
    (work_path / "logs").mkdir(parents=True)
    (work_path / "logs" / "application.log").write_text("log line\n")
    (work_path / "dda_backend_app.db").write_bytes(b"SQLite format 3\x00")
    (work_path / "workflow_runs" / "exec-1").mkdir(parents=True)
    (work_path / "workflow_runs" / "exec-1" / "run.log").write_text("run\n")
    store = CredentialStore(directory=str(work_path / "stream_credentials"))
    store.put("src-1", {"password": SECRET})
    archive = tmp_path / "snapshot-test.tar"
    archive.touch()

    tokens = _local_server_tar_command()
    command = [str(archive) if token == "$snapshotfile" else
               (str(work_path) + "/" if token == _LOCAL_SERVER_WORK_PATH else token)
               for token in tokens]
    assert command[0] == "tar" and str(archive) in command and str(work_path) + "/" in command
    result = subprocess.run(command, cwd=str(tmp_path), stdout=subprocess.PIPE,
                            stderr=subprocess.PIPE, universal_newlines=True)
    assert result.returncode == 0, result.stderr

    with tarfile.open(str(archive)) as snapshot:
        names = snapshot.getnames()
        contents = b"".join(snapshot.extractfile(member).read()
                            for member in snapshot.getmembers() if member.isfile())
    relative = {name.split("aws.edgeml.dda.LocalServer/", 1)[-1] for name in names}
    assert {"logs/application.log", "dda_backend_app.db", "workflow_runs/exec-1/run.log"} <= relative
    assert not [name for name in names if "stream_credentials" in name or "credentials.json" in name]
    assert SECRET.encode() not in contents
