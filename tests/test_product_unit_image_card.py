"""Validate the retail-kit scope and bounded review of unresolved product photos."""
from copy import deepcopy
import importlib.util
from pathlib import Path
from types import SimpleNamespace

import pytest


SOURCE = (Path(__file__).resolve().parents[1] /
          "scripts/local_inference/bundle/comfy_nodes/VideoFactoryReferenceCachePolicy/cache_policy.py")
SPEC = importlib.util.spec_from_file_location("factory_product_unit_policy", SOURCE)
policy = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(policy)

NATIVE_INSTRUCTIONS = "Ground the card in its image.\n\n" + policy.NATIVE_SUBJECT_PARAGRAPH
IMAGE = {"type": "image_url", "image_url": {"url": "data:image/jpeg;base64,fixture"}}
SCHEMA = {
    "title": "ImageCard",
    "properties": {"subjects": {
        "type": "array", "items": {"properties": {"kind": {"enum": ["object", "person", "animal"]}}},
    }},
}
KIT = {"kind": "object", "descriptor": "the advent calendar kit with its tray of toys",
       "attributes": ["boxed calendar", "toys in the fitted insert"], "pose": ["box is open"]}


class RecordingBackend:
    def __init__(self, *replies):
        self.replies = iter(replies)
        self.calls = []

    def json_call(self, messages, schema, **options):
        self.calls.append((deepcopy(messages), deepcopy(schema), options))
        return next(self.replies)


def request(backend, scope=policy.PRODUCT_UNIT):
    proxy = policy._ImageBackendProxy(backend, scope, SCHEMA, NATIVE_INSTRUCTIONS, "b"*64)
    return proxy.json_call([
        {"role": "system", "content": NATIVE_INSTRUCTIONS},
        {"role": "user", "content": [{"type": "text", "text": "Catalogue this image."}, IMAGE]},
    ], SCHEMA, seed=17)


def test_open_packaging_and_visibly_matching_contents_are_one_retail_unit():
    backend = RecordingBackend({"subjects": [KIT]})
    assert request(backend)["subjects"] == [KIT]
    [(messages, effective, options)] = backend.calls
    assert "box, fitted tray, and included pieces" in messages[0]["content"]
    assert policy.NATIVE_SUBJECT_PARAGRAPH not in messages[0]["content"]
    assert "Do not assume unrelated objects" in messages[0]["content"]
    assert messages[1]["content"][1] == IMAGE
    subjects = effective["properties"]["subjects"]
    assert subjects["maxItems"] == 1 and subjects["items"]["properties"]["kind"]["enum"] == ["object"]
    assert "maxItems" not in SCHEMA["properties"]["subjects"]
    assert options == {"seed": 17}


def test_empty_card_gets_one_grounded_recheck_on_the_same_image():
    backend = RecordingBackend({"subjects": [], "summary": "untrusted prior summary"}, {"subjects": [KIT]})
    assert request(backend)["subjects"] == [KIT]
    first, second = backend.calls
    assert first[1:] == second[1:]
    assert second[0][1]["content"][:-1] == first[0][1]["content"]
    assert second[0][1]["content"][-1] == {"type": "text", "text": policy.PRODUCT_RECHECK}
    assert "untrusted prior summary" not in str(second)


def test_an_unresolved_product_never_gets_a_synthetic_subject():
    backend = RecordingBackend({"subjects": []}, {"subjects": []})
    with pytest.raises(policy.CachePolicyError, match="object subject for product-unit: image SHA256 bbbbbbbbbbbb"):
        request(backend)
    assert len(backend.calls) == 2


def test_character_scope_keeps_its_original_prompt_and_single_call():
    backend = RecordingBackend({"subjects": []})
    with pytest.raises(policy.CachePolicyError, match="person subject for character-unit:"):
        request(backend, "character-unit:")
    [(messages, _, _)] = backend.calls
    assert NATIVE_INSTRUCTIONS in messages[0]["content"]
    assert "videofactory-reference-cache-policy-v2r1" in messages[0]["content"]
    assert policy.PRODUCT_RECHECK not in str(messages)


def test_new_product_prompt_does_not_reuse_old_product_cards_or_invalidate_character_cards():
    analyser = SimpleNamespace(_cache_key=lambda ref, model: "upstream-digest",
                               analyse_image=None, analyse_all=None, Backend=object)
    key = policy._make_wrappers(analyser, SCHEMA, NATIVE_INSTRUCTIONS)["_cache_key"]
    token = policy._LOCAL_BATCH.set(True)
    try:
        for prefix in (policy.PRODUCT_UNIT, "character-unit:"):
            ref = SimpleNamespace(kind=SimpleNamespace(value="image"), note=prefix,
                                  provenance={}, composition=None)
            old_policy = "videofactory-reference-cache-policy-v2r1"
            old = policy._digest({
                "namespace": policy.UNIT_NAMESPACE, "policy_version": old_policy,
                "native_key": "upstream-digest", "analysis_effective_inputs": policy._effective_inputs(ref),
                "unit": {
                    "prefix": prefix, "kind": policy.UNIT_RULES[prefix][0],
                    "instruction_sha256": policy._digest(policy._priority_instruction(prefix).replace(
                        policy.POLICY_VERSION, old_policy)),
                    "native_image_schema_sha256": policy._digest(SCHEMA),
                },
            })[:24]
            assert (key(ref, policy.MODEL) == old) == (prefix == "character-unit:")
    finally:
        policy._LOCAL_BATCH.reset(token)
