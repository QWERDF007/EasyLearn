import pytest


@pytest.mark.asyncio
async def test_create_upload_advertises_enforced_limits(client):
    response = await client.post(
        "/api/v1/uploads", json={"filename": "paper.pdf", "size": 1, "sha256": "0" * 64}
    )
    assert response.status_code == 201
    assert response.json()["limits"] == {
        "max_file_bytes": 536870912,
        "max_chunk_bytes": 4194304,
    }
    too_large = await client.post(
        "/api/v1/uploads",
        json={"filename": "paper.pdf", "size": 536870913, "sha256": "0" * 64},
    )
    assert too_large.status_code == 413
    assert too_large.json()["code"] == "UPLOAD_TOO_LARGE"


@pytest.mark.asyncio
async def test_validation_and_domain_errors_share_safe_traceable_contract(client):
    invalid = await client.post(
        "/api/v1/uploads",
        json={"filename": "private/secret.pdf", "size": 1, "sha256": "0" * 64},
    )
    assert invalid.status_code == 422
    error = invalid.json()
    assert error["code"] == "REQUEST_INVALID"
    assert error["request_id"] == invalid.headers["X-Request-ID"]
    assert error["retryable"] is False
    assert error["run_ref"] is None
    assert error["details"]["issues"][0]["location"] == ["body", "filename"]
    assert "secret.pdf" not in invalid.text
    missing = await client.get("/api/v1/uploads/00000000-0000-0000-0000-000000000000")
    assert missing.status_code == 404
    assert set(missing.json()) == set(error)
    assert missing.json()["request_id"] == missing.headers["X-Request-ID"]
    assert (await client.get("/api/v1/not-a-route")).json()["code"] == "HTTP_404"
