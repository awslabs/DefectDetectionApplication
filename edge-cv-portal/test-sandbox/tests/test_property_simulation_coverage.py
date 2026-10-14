"""Property test for simulation result coverage (task 8.4).

**Feature: custom-node-designer, Property 16: Simulation results cover every input frame**

For all simulated runs over random input frame sets (pipeline layer
mocked), the results report contains exactly one entry per input frame,
each carrying the input frame reference, the output frame reference, and
the emitted metadata.

**Validates: Requirements 7.3**

The subject is simulate-mode's pure result-shaping logic: the store is
driven exactly like ``simulate.execute`` drives it — initial flush,
frame count, per-frame ``add_frame`` of ``frame_record`` entries as the
capture tap produces output frames (an element may legitimately drop
frames, so the produced indexes are a random subset added in random
order with random incremental flush points), then
``missing_frame_records`` backfill and ``set_completed``. The finalized
document must cover every input frame index exactly once, ordered by
``frameIndex``, produced frames carrying their output references and
dropped frames retaining the input reference with a null output
reference; and every flushed snapshot along the way must be a
monotonically growing, ordered subset of the final results (partial
results retained mid-run).
"""

from hypothesis import given, settings
from hypothesis import strategies as st

from harness.simulate import (
    STATUS_COMPLETED,
    SimulationResultsStore,
    frame_metadata,
    frame_record,
    input_frame_key,
    missing_frame_records,
    output_frame_key,
)

RESULTS_KEY = "simulations/run-prop16/results.json"


class FlushRecorder:
    """Captures every flushed document snapshot (the S3 put stand-in)."""

    def __init__(self):
        self.snapshots = []

    def __call__(self, document):
        self.snapshots.append(document)


@st.composite
def simulation_run(draw):
    """One simulated run: an input frame set, the subset of frames the
    element produced output for, the capture-order permutation those
    records get added in, and per-add incremental flush choices."""
    frame_count = draw(st.integers(min_value=0, max_value=25))
    produced = sorted(draw(st.sets(
        st.integers(min_value=0, max_value=frame_count - 1))
        if frame_count else st.just(set())))
    order = draw(st.permutations(produced))
    flush_choices = draw(st.lists(st.booleans(), min_size=len(order),
                                  max_size=len(order)))
    metadata_sizes = draw(st.lists(
        st.integers(min_value=0, max_value=1 << 20),
        min_size=len(order), max_size=len(order)))
    return frame_count, order, flush_choices, metadata_sizes


def _drive_run(frame_count, order, flush_choices, metadata_sizes, flush):
    """Drive the store through the harness's completed-run lifecycle
    (simulate.execute steps 2-5, pipeline layer mocked)."""
    input_refs = [input_frame_key(RESULTS_KEY, index)
                  for index in range(frame_count)]
    metadata_by_index = {}

    store = SimulationResultsStore("myfilter", {"threshold": 3}, flush)
    store.flush()  # results document exists before anything can fail
    store.set_frame_count(frame_count, flush=False)
    store.flush()  # input frames staged

    # Capture tap: produced frames added incrementally, flush points vary.
    for position, index in enumerate(order):
        metadata = frame_metadata(
            pts=index * 1000, duration=40, size=metadata_sizes[position],
            caps="image/jpeg", tags={"frame": index})
        metadata_by_index[index] = metadata
        store.add_frame(
            frame_record(index, input_refs[index],
                         output_frame_key(RESULTS_KEY, index), metadata),
            flush=flush_choices[position])

    # Completed: backfill dropped frames, mark completed (final flush).
    for record in missing_frame_records(frame_count, set(order), input_refs):
        store.add_frame(record, flush=False)
    store.set_completed()
    return input_refs, metadata_by_index


@given(run=simulation_run())
@settings(max_examples=25, deadline=None)
def test_simulation_results_cover_every_input_frame(run):
    """**Feature: custom-node-designer, Property 16: Simulation results cover every input frame**

    **Validates: Requirements 7.3**
    """
    frame_count, order, flush_choices, metadata_sizes = run
    produced = set(order)
    flush = FlushRecorder()
    input_refs, metadata_by_index = _drive_run(
        frame_count, order, flush_choices, metadata_sizes, flush)

    # -- finalized document: exactly one entry per input frame, ordered --
    final = flush.snapshots[-1]
    assert final["status"] == STATUS_COMPLETED
    assert final["frameCount"] == frame_count
    indexes = [record["frameIndex"] for record in final["frames"]]
    assert indexes == list(range(frame_count)), (
        "results must cover every input frame index exactly once, "
        "ordered: {0}".format(indexes))

    for record in final["frames"]:
        index = record["frameIndex"]
        assert set(record) == {"frameIndex", "inputRef", "outputRef",
                               "metadata"}
        assert record["inputRef"] == input_refs[index], (
            "frame {0} must retain its input frame reference".format(index))
        if index in produced:
            assert record["outputRef"] == output_frame_key(RESULTS_KEY, index)
            assert record["metadata"] == metadata_by_index[index], (
                "frame {0} must carry the emitted metadata".format(index))
        else:
            assert record["outputRef"] is None, (
                "dropped frame {0} must be backfilled with a null output "
                "reference".format(index))
            assert record["metadata"].get("note"), (
                "dropped frame {0} must explain the missing output".format(
                    index))

    # -- every flushed snapshot: ordered, monotonically growing subset --
    final_by_index = {record["frameIndex"]: record
                      for record in final["frames"]}
    previous_indexes = set()
    for snapshot in flush.snapshots:
        snapshot_indexes = [record["frameIndex"]
                            for record in snapshot["frames"]]
        assert snapshot_indexes == sorted(set(snapshot_indexes)), (
            "flushed snapshot frames must be ordered and unique: "
            "{0}".format(snapshot_indexes))
        assert previous_indexes.issubset(set(snapshot_indexes)), (
            "partial results must be retained across flushes")
        previous_indexes = set(snapshot_indexes)
        for record in snapshot["frames"]:
            if record["frameIndex"] in produced:
                assert record == final_by_index[record["frameIndex"]], (
                    "mid-run snapshot must carry the same produced-frame "
                    "record as the final results")
    assert previous_indexes == set(range(frame_count))
