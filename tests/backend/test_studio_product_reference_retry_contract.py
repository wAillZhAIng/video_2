from pydantic import ValidationError
import pytest

from app.api.studio import ProductReferenceRetry


def test_product_reference_retry_accepts_target_node_id():
    payload = ProductReferenceRetry.model_validate({
        "expected_run_id": "studio_0123456789abcdef",
        "node_id": "video:shot_0123456789abcdef",
        "products": [{
            "product_id": "product_0123456789abcdef",
            "expected_revision": 1,
            "video_image_ids": ["pimage_0123456789abcdef"],
            "keyframe_image_ids": ["pimage_0123456789abcdef"],
        }],
    })

    assert payload.node_id == "video:shot_0123456789abcdef"


def test_product_reference_retry_requires_target_node_id():
    with pytest.raises(ValidationError):
        ProductReferenceRetry.model_validate({
            "expected_run_id": "studio_0123456789abcdef",
            "products": [{
                "product_id": "product_0123456789abcdef",
                "expected_revision": 1,
                "video_image_ids": ["pimage_0123456789abcdef"],
                "keyframe_image_ids": ["pimage_0123456789abcdef"],
            }],
        })
