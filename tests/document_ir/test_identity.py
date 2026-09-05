from uuid import UUID

from easylearn.document_ir.schema import BlockRef


def test_same_block_name_in_different_parse_versions_is_a_different_reference():
    first = BlockRef(document_id=UUID(int=1), parse_run_id=UUID(int=2), block_id="p0-b1")
    second = BlockRef(document_id=UUID(int=1), parse_run_id=UUID(int=3), block_id="p0-b1")

    assert len({first, second}) == 2
    assert BlockRef.model_validate_json(first.model_dump_json()) == first
