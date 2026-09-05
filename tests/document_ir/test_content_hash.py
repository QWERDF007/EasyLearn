import copy

from easylearn.document_ir.schema import DocumentIR


def test_source_hash_is_structural_and_independent_of_json_key_or_reading_order(document_payload):
    document_payload["blocks"] = [
        {
            "block_id": "paragraph",
            "block_type": "paragraph",
            "order_index": 0,
            "source_nodes": [{"node_id": "text", "type": "text", "text": "Evidence"}],
        }
    ]
    first = DocumentIR.model_validate(document_payload)
    reordered = copy.deepcopy(document_payload)
    reordered["blocks"][0] = dict(reversed(list(reordered["blocks"][0].items())))
    reordered["blocks"][0]["order_index"] = 5
    second = DocumentIR.model_validate(reordered)
    assert first.blocks[0].source_content_hash == second.blocks[0].source_content_hash
    reordered["blocks"][0]["source_nodes"][0]["text"] = "Different evidence"
    third = DocumentIR.model_validate(reordered)
    assert first.blocks[0].source_content_hash != third.blocks[0].source_content_hash
    assert len(first.blocks[0].source_content_hash) == 64
    assert first.page_count == 1
