from fastapi.testclient import TestClient

from app.config import Settings
from app.main import create_app


def test_schema_declares_the_api_key_scheme(client):
    schemes = client.get("/openapi.json").json()["components"]["securitySchemes"]
    assert schemes["APIKeyHeader"]["in"] == "header"
    assert schemes["APIKeyHeader"]["name"] == "X-API-Key"


def test_upload_route_declares_its_error_responses(client):
    responses = client.get("/openapi.json").json()["paths"]["/v1/documents"]["post"]["responses"]
    for code in ("202", "400", "401", "503"):
        assert code in responses


def test_upload_route_requires_the_scheme(client):
    operation = client.get("/openapi.json").json()["paths"]["/v1/documents"]["post"]
    assert {"APIKeyHeader": []} in operation["security"]


def test_health_route_is_not_secured(client):
    operation = client.get("/openapi.json").json()["paths"]["/health"]["get"]
    assert "security" not in operation


def test_artifact_routes_declare_409(client):
    paths = client.get("/openapi.json").json()["paths"]
    assert "409" in paths["/v1/documents/{job_id}/markdown"]["get"]["responses"]
    assert "409" in paths["/v1/documents/{job_id}/tables/{index}"]["get"]["responses"]


def test_options_field_carries_an_example(client):
    schema = client.get("/openapi.json").json()["components"]["schemas"]
    body = schema["Body_create_document_v1_documents_post"]["properties"]["options"]
    assert "example" in body or "examples" in body


def test_app_description_explains_the_flow(client):
    info = client.get("/openapi.json").json()["info"]
    assert "202" in info["description"]
    assert "poll" in info["description"].lower()


def test_docs_can_be_disabled(tmp_path, recording_service):
    settings = Settings(api_keys="k", data_dir=tmp_path, docs_enabled=False)
    app = create_app(settings=settings, service=recording_service)
    with TestClient(app) as client:
        assert client.get("/openapi.json").status_code == 404
        assert client.get("/docs").status_code == 404


def test_docs_page_renders(client):
    assert client.get("/docs").status_code == 200


def test_no_operation_advertises_the_dead_auto_422(client):
    paths = client.get("/openapi.json").json()["paths"]
    for path, operations in paths.items():
        for method, operation in operations.items():
            assert "422" not in operation["responses"], (
                f"{method.upper()} {path} still advertises an unreachable 422; "
                "validation failures are always rendered as 400 by the "
                "RequestValidationError handler."
            )


def test_stripping_422_leaves_the_rest_of_a_route_intact(client):
    responses = client.get("/openapi.json").json()["paths"]["/v1/documents/{job_id}/markdown"][
        "get"
    ]["responses"]
    assert "422" not in responses
    for code in ("200", "401", "404", "409"):
        assert code in responses


def test_artifact_routes_declare_their_response_media_type(client):
    """Without a `content` entry on the 200, the schema does not say what
    any of these routes return -- and the spec's bar is that Swagger UI is
    enough to integrate against without reading the source."""
    paths = client.get("/openapi.json").json()["paths"]
    expected = {
        "/v1/documents/{job_id}/markdown": ["text/markdown"],
        "/v1/documents/{job_id}/document.json": ["application/json"],
        "/v1/documents/{job_id}/tables/{index}": ["text/csv", "text/html"],
        "/v1/documents/{job_id}/images/{name}": ["image/png"],
    }
    for route, media_types in expected.items():
        ok = paths[route]["get"]["responses"]["200"]
        assert "content" in ok, f"{route} declares no response media type"
        for media_type in media_types:
            assert media_type in ok["content"], f"{route} does not declare {media_type}"


def test_upload_route_declares_413(client):
    responses = client.get("/openapi.json").json()["paths"]["/v1/documents"]["post"]["responses"]
    assert "413" in responses


def test_listing_routes_declare_their_error_responses(client):
    paths = client.get("/openapi.json").json()["paths"]
    for route in ("/v1/documents/{job_id}/images", "/v1/documents/{job_id}/tables"):
        responses = paths[route]["get"]["responses"]
        for code in ("200", "401", "404", "409"):
            assert code in responses, f"{route} is missing {code}"
