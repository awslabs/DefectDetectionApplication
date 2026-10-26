# Copyright 2025 Amazon Web Services, Inc.
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
"""Edge catalog classification honesty (``mcp_edge/child/catalog.py``).

Spec: .kiro/specs/mcp-servers/ (design.md Testing Strategy row "Edge
classification honesty", Decision point 4, Property 2 "edge
classification"; review-5 H1, review-9 N1, review-10 N7, review-11 N3;
tasks.md 11.13). Requirements 8.2, 8.3, 10.1.

For every ``RO`` edge spec the transitive walk of ``_edge_ast`` over
``src/backend`` from the route function must find exactly the spec's
``accepted_effects`` (an unlisted effect fails, and so does a listed entry
the walk no longer finds). Accepted readings outside the walk's reach stay
fixed cases below: the ingest-session lease/restart (the worker spawn,
``stream_ingest/launch.py``, sits behind a manager passed in as an untyped
parameter), the lazy ``TritonEdgeClient.get_instance`` and
``model_status_shadow.report`` (no effect-list call on their paths), and the
retention orphan sweep behind ``notable=true`` (an index injected through a
constructor argument).
"""
import os
import textwrap

import pytest

import _edge_ast as ea
from mcp_edge.child import catalog

RO_SPECS = [s for s in catalog.CATALOG if s.read_only]
READ_ONLY_POSTS = ('/image-sources/{imageSourceId}/preview',
                   '/image-sources/{imageSourceId}/test-connection',
                   '/text-generation/{model_name}/generate')


@pytest.fixture(scope='module')
def uni():
    return ea.universe()


def walk(uni, module, function):
    return uni.route_effects(module, function)[1]


@pytest.mark.parametrize('spec', RO_SPECS, ids=lambda s: s.name)
def test_ro_spec_finds_exactly_its_accepted_effects(uni, spec):
    found = walk(uni, spec.handler_module, spec.handler_function)
    assert sorted(found) == sorted(spec.accepted_effects), spec.name
    if spec.accepted_effects:
        assert spec.accepted_effects_note == catalog.ACCEPTED_EFFECT_NOTE
    else:
        assert spec.accepted_effects_note is None


@pytest.mark.parametrize('spec', RO_SPECS, ids=lambda s: s.name)
def test_ro_spec_method_is_get_or_a_listed_read_only_post(spec):
    assert spec.method == 'GET' or (spec.method == 'POST' and spec.resource in READ_ONLY_POSTS), \
        (spec.name, spec.method, spec.resource)


def test_eight_accepted_effects_on_eight_tools():
    with_effects = [s for s in RO_SPECS if s.accepted_effects]
    assert sorted(s.name for s in with_effects) == sorted([
        'edge_workflows_list', 'edge_workflow_get', 'edge_image_sources_list',
        'edge_image_source_get', 'edge_image_source_stream_health_get',
        'edge_image_source_test_connection', 'edge_image_source_preview',
        'edge_streams_capabilities_get'])
    assert len({e for s in with_effects for e in s.accepted_effects}) == 8
    assert len(RO_SPECS) - len(with_effects) == 39      # 37 walked + whoami + status
    for spec in catalog.CATALOG:
        if not spec.read_only:
            assert spec.accepted_effects == ()


# ----------------------------------------------------- fixed regression cases

def test_get_snapshot_is_not_read_only(uni):
    assert 'snapshot.Snapshotter.take_snapshot:subprocess.check_output' in \
        walk(uni, 'endpoints.system', 'get_snapshot')


def test_capture_id_download_write_is_not_read_only(uni):
    found = walk(uni, 'endpoints.inference_result',
                 'save_capture_ids_for_inference_results_download')
    assert ('endpoints.inference_result.save_capture_ids_for_inference_results_download:open'
            in found)
    assert not any(s.handler_function == 'save_capture_ids_for_inference_results_download'
                   for s in catalog.CATALOG)


@pytest.mark.parametrize('module,function', [
    ('endpoints.camera', 'connect_camera_endpoint'),
    ('endpoints.feature_config', 'start_feature_config'),
    ('endpoints.workflow', 'retry_dio_workflow'),
])
def test_get_but_mutating_rows_are_not_read_only(module, function):
    specs = [s for s in catalog.CATALOG
             if (s.handler_module, s.handler_function) == (module, function)]
    assert specs, function
    for spec in specs:
        assert spec.method == 'GET' and not spec.read_only, spec.name


@pytest.mark.parametrize('module,function', [
    ('endpoints.image_source', 'preview_image'),
    ('endpoints.system', 'get_system_health'),
    ('endpoints.workflow', 'list_images'),
    ('endpoints.image_source', 'test_stream_connection'),
    ('endpoints.feature_config', 'list_feature_configs'),
    ('endpoints.feature_config', 'get_gpu_status'),
    ('endpoints.streams', 'get_stream_capabilities'),
    ('workflow_engine.api', 'list_registration_executions'),
])
def test_accepted_readings_are_read_only(uni, module, function):
    [spec] = [s for s in catalog.CATALOG
              if (s.handler_module, s.handler_function) == (module, function)]
    assert spec.read_only
    assert set(walk(uni, module, function)) <= set(spec.accepted_effects)


def test_disk_usage_and_gettempdir_are_lookups(uni):
    health = walk(uni, 'endpoints.system', 'get_system_health')
    assert not [e for e in health if 'shutil' in e]
    capabilities = walk(uni, 'endpoints.streams', 'get_stream_capabilities')
    assert not [e for e in capabilities if 'tempfile' in e]


def test_jp6_staging_write_is_found_and_named_by_the_effect_list(uni):
    found = walk(uni, 'endpoints.image_source', 'preview_image')
    assert 'gstreamer.pipeline_builder.GstPipelineBuilder._stage_decoded_png:Image.save' in found
    assert not [e for e in found if e.endswith(':im.save')]


# ----------------------------------------------- the effect list on fixtures

def _fixture_effects(tmp_path, source: str, function: str = 'f'):
    path = os.path.join(tmp_path, 'fixture_mod.py')
    with open(path, 'w', encoding='utf-8') as handle:
        handle.write(textwrap.dedent(source))
    uni = ea.Universe.__new__(ea.Universe)
    uni.modules = {'fixture_mod': ea.Module('fixture_mod', path, False)}
    return [e.split(':', 1)[1] for e in uni.effects(ea.FuncRef('fixture_mod', function))]


@pytest.mark.parametrize('body,expected', [
    ('from PIL import Image, ImageOps\ndef f(p, q):\n    ImageOps.exif_transpose(Image.open(p)).save(q)\n',
     ['Image.save']),
    ('from PIL import Image\ndef f(p, q):\n    with Image.open(p) as im:\n        im.convert("RGB").save(q)\n',
     ['Image.save']),
    ('from PIL import Image\ndef f(a, q):\n    im = Image.fromarray(a)\n    im.save(q)\n', ['Image.save']),
    ('def f(doc, q):\n    doc.save(q)\n', []),
    ('import datetime\ndef f():\n    return datetime.datetime.now().replace(microsecond=0)\n', []),
    ('from pathlib import Path\ndef f(p):\n    Path(p).unlink()\n', ['Path.unlink']),
    ('from pathlib import Path\ndef f(p):\n    d = Path(p) / "x"\n    d.mkdir()\n', ['Path.mkdir']),
    ('from pathlib import Path\ndef f(p):\n    Path(p).write_text("x")\n', ['Path.write_text']),
    ('from pathlib import Path\ndef f(p):\n    with Path(p).open("a") as h:\n        h.write("x")\n',
     ['Path.open']),
    ('from pathlib import Path\ndef f(p):\n    return Path(p).open().read()\n', []),
    ('import os\ndef f(p):\n    os.makedirs(p)\n    os.chmod(p, 0o755)\n', ['os.chmod', 'os.makedirs']),
    ('import os\ndef f(p):\n    os.replace(p, p + ".bak")\n', ['os.replace']),
    ('import shutil\ndef f(p):\n    shutil.disk_usage(p)\n    shutil.which("x")\n', []),
    ('import shutil\ndef f(p, q):\n    shutil.copy(p, q)\n', ['shutil.copy']),
    ('import tempfile\ndef f():\n    return tempfile.gettempdir()\n', []),
    ('import tempfile\ndef f():\n    return tempfile.mkstemp()\n', ['tempfile.mkstemp']),
    ('def f(p):\n    with open(p, "w") as h:\n        h.write("x")\n', ['open']),
    ('def f(p):\n    with open(p, mode="rb") as h:\n        return h.read()\n', []),
    ('import subprocess\ndef f():\n    subprocess.check_output(["ls"])\n', ['subprocess.check_output']),
])
def test_effect_list(tmp_path, body, expected):
    assert _fixture_effects(str(tmp_path), body) == sorted(expected)
