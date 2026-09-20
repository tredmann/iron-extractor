"""Request-time rejection of OCR languages this server has no weights for.

The container bakes a fixed set of EasyOCR recognizers (see the Dockerfile).
Asking for a language outside that set used to sail through the 400 boundary
and blow up ~a second later inside a worker thread, surfacing docling's raw
`FileNotFoundError` -- container-internal path and all -- to the caller.
"""

import json


from app.storage.paths import JobPaths


def _upload(client, auth, *, options=None):
    data = {"options": json.dumps(options)} if options is not None else None
    return client.post(
        "/v1/documents",
        headers=auth,
        files={"file": ("report.pdf", b"%PDF-1.4 fake", "application/pdf")},
        data=data,
    )


def _reject(message):
    def check(options):
        return message
    return check


def test_unservable_language_is_a_400_not_a_failed_job(client, auth, recording_service):
    client.app.state.check_ocr_support = _reject(
        "This server has no OCR weights for ['ja']."
    )

    response = _upload(client, auth, options={"ocr_languages": ["ja"]})

    assert response.status_code == 400
    body = response.json()["error"]
    assert body["code"] == "unsupported_ocr_language"
    assert body["message"] == "This server has no OCR weights for ['ja']."
    # Rejected before any work was scheduled.
    assert recording_service.calls == []


def test_rejected_upload_leaves_nothing_on_disk(client, auth, settings):
    """Rejection happens before `paths.create`, so no directory is even made.

    Stronger than "the jobs root is empty": the check runs ahead of the
    upload being spooled, so there is nothing to clean up afterwards.
    """
    client.app.state.check_ocr_support = _reject("nope")

    assert _upload(client, auth, options={"ocr_languages": ["ja"]}).status_code == 400

    jobs_root = JobPaths(settings.data_dir).jobs_root
    assert not jobs_root.exists() or list(jobs_root.iterdir()) == []


def test_servable_language_still_gets_202(client, auth):
    client.app.state.check_ocr_support = lambda options: None
    assert _upload(client, auth, options={"ocr_languages": ["en", "de"]}).status_code == 202


def test_substituted_service_accepts_every_language_by_default(client, auth):
    """A fake service has no converter provider, so it vetoes nothing."""
    assert _upload(client, auth, options={"ocr_languages": ["ja"]}).status_code == 202
