"""
dda_labeling_worker._canonical_annotation normalization
(llm-auto-labeling, task 8.2).

Feature: llm-auto-labeling

Covers the widened image_width/image_height -> image_size bridge
(Req 8.1): a Segmentation pre-label carrying the dimension fields
gains the canonical image_size (previously ObjectDetection only); an
annotation already carrying image_size is returned unchanged even
when the dimension fields disagree; one carrying neither is returned
unchanged; Classification is never given an image_size; and DynamoDB
Decimal values still normalize to plain ints.

_canonical_annotation is pure, but dda_labeling_worker builds boto3
clients at import time, so the module is imported inside the moto
mock exactly as the other worker suites do.
"""
import sys
from decimal import Decimal

import pytest


@pytest.fixture(scope="module")
def worker(aws_stack):
    """The real dda_labeling_worker imported inside the moto mock."""
    sys.modules.pop("dda_labeling", None)
    sys.modules.pop("dda_labeling_worker", None)
    import dda_labeling_worker
    return dda_labeling_worker


class TestCanonicalAnnotationSegmentationBridge:
    def test_segmentation_dimensions_gain_image_size(self, worker):
        """Req 8.1: a Segmentation pre-label in the auto-label worker's
        shape (image_width/image_height, no image_size) gains the
        canonical image_size mask rendering requires."""
        annotation = {
            "modality": "Segmentation",
            "image_width": 640,
            "image_height": 480,
            "regions": [{"class": "scratch", "rle": "12 5 40 3"}],
        }
        result = worker._canonical_annotation(annotation, "Segmentation")
        assert result["image_size"] == {"width": 640, "height": 480}
        # The original fields and regions are untouched.
        assert result["image_width"] == 640
        assert result["image_height"] == 480
        assert result["regions"] == annotation["regions"]

    def test_existing_image_size_wins_over_disagreeing_dimensions(
            self, worker):
        """The bridge fires only when image_size is absent: an
        annotation already carrying image_size keeps it verbatim even
        when the dimension fields disagree."""
        annotation = {
            "modality": "Segmentation",
            "image_size": {"width": 100, "height": 80},
            "image_width": 999,
            "image_height": 777,
            "regions": [],
        }
        result = worker._canonical_annotation(annotation, "Segmentation")
        assert result == annotation

    def test_neither_shape_returned_unchanged(self, worker):
        """No image_size and no dimension fields: nothing is invented."""
        annotation = {
            "modality": "Segmentation",
            "regions": [{"class": "dent", "rle": "0 4"}],
        }
        result = worker._canonical_annotation(annotation, "Segmentation")
        assert result == annotation
        assert "image_size" not in result

    def test_partial_dimensions_do_not_fire_the_bridge(self, worker):
        """The guard needs both fields; a lone width invents nothing."""
        annotation = {
            "modality": "Segmentation",
            "image_width": 640,
            "regions": [],
        }
        result = worker._canonical_annotation(annotation, "Segmentation")
        assert "image_size" not in result


class TestCanonicalAnnotationClassification:
    def test_classification_never_gains_image_size(self, worker):
        """The bridge is scoped to the geometry modalities: a
        Classification annotation carrying dimension fields is left
        without an image_size."""
        annotation = {
            "modality": "Classification",
            "label": "anomaly",
            "image_width": 640,
            "image_height": 480,
        }
        result = worker._canonical_annotation(annotation, "Classification")
        assert "image_size" not in result
        assert result["label"] == "anomaly"


class TestCanonicalAnnotationDecimals:
    def test_decimal_dimensions_normalize_to_plain_ints(self, worker):
        """DynamoDB Decimal dimension fields still come out as plain
        ints, both in the copied fields and the derived image_size."""
        annotation = {
            "modality": "Segmentation",
            "image_width": Decimal("640"),
            "image_height": Decimal("480"),
            "regions": [{"class": "scratch", "rle": "12 5 40 3"}],
        }
        result = worker._canonical_annotation(annotation, "Segmentation")
        assert result["image_size"] == {"width": 640, "height": 480}
        assert isinstance(result["image_size"]["width"], int)
        assert isinstance(result["image_size"]["height"], int)
        assert result["image_width"] == 640
        assert isinstance(result["image_width"], int)
        assert not isinstance(result["image_width"], Decimal)
        assert isinstance(result["image_height"], int)

    def test_decimal_image_size_normalizes_to_plain_ints(self, worker):
        """An already-canonical annotation read back from DynamoDB
        (nested Decimals) normalizes without the bridge firing."""
        annotation = {
            "modality": "ObjectDetection",
            "image_size": {"width": Decimal("100"), "height": Decimal("80")},
            "boxes": [{"class": "scratch", "left": Decimal("10"),
                       "top": Decimal("20"), "width": Decimal("30"),
                       "height": Decimal("15")}],
        }
        result = worker._canonical_annotation(annotation, "ObjectDetection")
        assert result["image_size"] == {"width": 100, "height": 80}
        assert isinstance(result["image_size"]["width"], int)
        box = result["boxes"][0]
        assert box == {"class": "scratch", "left": 10, "top": 20,
                       "width": 30, "height": 15}
        assert all(isinstance(box[k], int)
                   for k in ("left", "top", "width", "height"))
