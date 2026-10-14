"""Contract content example tests for the produce_frame Code_Assistant
contract (custom-python-source, task 4.3).

The `produce_frame` entry in `code_assist.CONTRACTS` declares the
entry-point rule and signature the validator enforces, and its
environment text names the Trigger_Context keys for both trigger
transports (and {} for manual runs), the three accepted return shapes,
the None-fails-the-run rule, and the `dda_frames.load_image` /
`load_bytes` Frame_Helpers with their timeout and prefix restriction.

_Requirements: 9.4_
"""
import sys

import pytest


@pytest.fixture(scope="module")
def code_assist_module(aws_stack):
    """The real code_assist module, imported inside the moto stack so its
    module-level `from shared_utils import ...` binds the real layer."""
    sys.modules.pop("code_assist", None)
    import code_assist
    return code_assist


@pytest.fixture(scope="module")
def contract(code_assist_module):
    return code_assist_module.CONTRACTS['produce_frame']


class TestProduceFrameContractEntry:
    def test_entry_point_rule_and_signature(self, contract):
        """The contract requires exactly the produce_frame entry point
        with the produce_frame(context) signature."""
        assert contract['entry_points'] == frozenset({'produce_frame'})
        assert contract['require_exactly_one'] is False
        assert contract['signature'] == 'produce_frame(context)'

    def test_environment_is_the_produce_frame_environment(
            self, code_assist_module, contract):
        assert contract['environment'] is (
            code_assist_module.PRODUCE_FRAME_ENVIRONMENT)


class TestProduceFrameEnvironmentText:
    def test_exactly_once_invocation(self, contract):
        """The environment states the exactly-once-per-run invocation."""
        env = contract['environment']
        assert 'EXACTLY ONCE per workflow run' in env

    def test_mqtt_trigger_context_keys(self, contract):
        """Every MQTT Trigger_Context key is named, including the derived
        payload_json."""
        env = contract['environment']
        for key in ('topic', 'payload', 'payload_json', 'qos', 'timestamp'):
            assert key in env, f"MQTT Trigger_Context key {key!r} missing"

    def test_opcua_trigger_context_keys(self, contract):
        """Every OPC UA Trigger_Context key is named."""
        env = contract['environment']
        for key in ('endpoint', 'node_id', 'value', 'source_timestamp'):
            assert key in env, f"OPC UA Trigger_Context key {key!r} missing"

    def test_manual_runs_get_empty_context(self, contract):
        assert '{} for manual runs' in contract['environment']

    def test_accepted_return_shapes(self, contract):
        """All three accepted return shapes are described: BGR/BGRA/GRAY8
        NumPy arrays, the {"array", "format"} mapping, and the
        {"data", "width", "height", "format"} mapping."""
        env = contract['environment']
        # NumPy array shapes in OpenCV channel order.
        assert 'NumPy uint8 array' in env
        assert 'H x W grayscale' in env
        assert 'H x W x 3' in env and 'BGR' in env
        assert 'H x W x 4' in env and 'BGRA' in env
        assert 'OpenCV channel order' in env
        # The conversion-free array mapping with its supported formats.
        assert '{"array": arr, "format": "RGB"|"RGBA"|"GRAY8"}' in env
        # The raw-bytes mapping.
        assert '{"data": bytes, "width": W, "height": H, "format": ...}' in env

    def test_none_fails_the_run(self, contract):
        assert 'Returning None fails the run' in contract['environment']

    def test_frame_helpers(self, contract):
        """The dda_frames helpers are named with their reach (local, S3,
        HTTP(S)), the bounded timeout, and the prefix restriction."""
        env = contract['environment']
        assert 'dda_frames' in env
        assert 'load_image(source)' in env
        assert 'load_bytes(source)' in env
        assert 's3://bucket/key' in env
        assert 'http(s)://' in env
        assert 'bounded network timeout' in env
        assert 'allowed URI prefixes' in env

    def test_prebound_modules(self, contract):
        """cv2/np/numpy are pre-bound, matching the runner."""
        env = contract['environment']
        assert 'cv2, np, and numpy are pre-bound' in env


class TestProduceFrameContractWiring:
    def test_contract_is_a_valid_request_contract(self, code_assist_module):
        """validate_request accepts produce_frame as a contract (the 400
        matrix is driven by CONTRACTS membership)."""
        body = {'usecase_id': 'uc-1', 'surface': 'workflow-builder',
                'contract': 'produce_frame', 'prompt': 'Fetch the image'}
        assert code_assist_module.validate_request(body) is None

    def test_system_prompt_carries_signature_and_environment(
            self, code_assist_module):
        """build_system_prompt embeds the contract signature and the
        producer environment text."""
        prompt = code_assist_module.build_system_prompt('produce_frame')
        assert 'TARGET ENTRY POINT: produce_frame(context)' in prompt
        assert code_assist_module.PRODUCE_FRAME_ENVIRONMENT in prompt

    def test_missing_entry_point_is_the_existing_defect(
            self, code_assist_module):
        """Generated code lacking a top-level produce_frame gets the
        existing missing-entry-point defect (Requirement 9.5)."""
        defect = code_assist_module.validate_entry_point(
            'def process_frame(frame, metadata):\n    return None\n',
            'produce_frame')
        assert defect is not None
        assert defect.startswith('missing entry point')
        assert 'produce_frame(context)' in defect
