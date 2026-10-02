"""API surface for import failure diagnosis: queue listing, the failures summary, and bulk fixes."""
from __future__ import annotations

from backend.db import models
from backend.db.session import session_scope


def _make_failed_item(signature: str = "quality_not_allowed:movie:999") -> int:
    with session_scope() as db:
        movie = models.Movie(title="Diagnosed Movie", year=2022, quality_profile_id=1, monitored=True, has_file=False)
        db.add(movie)
        db.flush()
        analysis = {
            "reason": "quality_not_allowed", "summary": "Diagnosed Movie isn't allowed by the quality profile.",
            "evidence": {}, "signature": signature, "confidence": 0.9, "analysed_at": "2026-01-01T00:00:00+00:00",
            "fixes": [
                {"kind": "force_import", "label": "Force import anyway", "params": {}, "safe_auto": False, "changes_settings": False, "bulk_applicable": False},
                {"kind": "remove", "label": "Remove from queue", "params": {}, "safe_auto": False, "changes_settings": False, "bulk_applicable": False},
            ],
        }
        q = models.DownloadQueueItem(
            title=movie.title, media_type="movie", media_id=movie.id, release_title="Diagnosed.Movie.2022.1080p",
            status="import_failed", error_message="not allowed", failure_reason="quality_not_allowed",
            failure_signature=signature, failure_analysis=analysis,
        )
        db.add(q)
        db.flush()
        return q.id


def test_queue_listing_exposes_failure_reason(client, admin_headers):
    queue_id = _make_failed_item("quality_not_allowed:movie:1001")
    r = client.get("/api/v1/queue", headers=admin_headers, params={"include_finished": True})
    assert r.status_code == 200
    row = next(i for i in r.json() if i["id"] == queue_id)
    assert row["failure_reason"] == "quality_not_allowed"
    assert row["failure_analysis"]["summary"]


def test_failures_summary_groups_by_signature(client, admin_headers):
    sig = "quality_not_allowed:movie:1002"
    _make_failed_item(sig)
    _make_failed_item(sig)
    r = client.get("/api/v1/activity/failures/summary", headers=admin_headers, params={"days": 7})
    assert r.status_code == 200
    body = r.json()
    group = next(g for g in body["top_signatures"] if g["signature"] == sig)
    assert group["count"] == 2
    assert group["reason"] == "quality_not_allowed"
    assert any(r["reason"] == "quality_not_allowed" for r in body["by_reason"])


def test_bulk_fix_removes_every_item_with_the_signature(client, admin_headers):
    sig = "quality_not_allowed:movie:1003"
    first = _make_failed_item(sig)
    second = _make_failed_item(sig)
    r = client.post("/api/v1/queue/fixes", headers=admin_headers, json={"signature": sig, "kind": "remove", "params": {}})
    assert r.status_code == 200, r.text
    assert r.json()["retried"] == 2
    with session_scope() as db:
        assert db.get(models.DownloadQueueItem, first) is None
        assert db.get(models.DownloadQueueItem, second) is None


def test_bulk_fix_unknown_signature_404s(client, admin_headers):
    r = client.post("/api/v1/queue/fixes", headers=admin_headers, json={"signature": "nope", "kind": "remove", "params": {}})
    assert r.status_code == 404


def test_single_item_fix_via_import_endpoint(client, admin_headers):
    queue_id = _make_failed_item("quality_not_allowed:movie:1004")
    r = client.post(f"/api/v1/queue/{queue_id}/import", headers=admin_headers, json={"fix": {"kind": "remove", "params": {}}})
    assert r.status_code == 200, r.text
    assert r.json()["retried"] == 1
    with session_scope() as db:
        assert db.get(models.DownloadQueueItem, queue_id) is None


def test_diagnose_endpoint_rereuns_probes(client, admin_headers):
    with session_scope() as db:
        series = models.Series(title="Gone Show", year=2020, quality_profile_id=1, monitored=True)
        db.add(series)
        db.flush()
        q = models.DownloadQueueItem(title="Gone Show", media_type="tv", media_id=999999, release_title="Gone.Show.S01E01",
                                     status="import_failed", error_message="Series no longer exists in the library",
                                     failure_reason="media_deleted", failure_signature="media_deleted:tv:999999", failure_analysis=None)
        db.add(q)
        db.flush()
        queue_id = q.id

    r = client.post(f"/api/v1/queue/{queue_id}/diagnose", headers=admin_headers)
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["reason"] == "media_deleted"
    assert any(f["kind"] == "remove" for f in body["fixes"])
    with session_scope() as db:
        assert db.get(models.DownloadQueueItem, queue_id).failure_analysis["reason"] == "media_deleted"


def test_diagnose_requires_a_recorded_failure(client, admin_headers):
    with session_scope() as db:
        q = models.DownloadQueueItem(title="Fine", media_type="movie", media_id=1, release_title="Fine.2020", status="completed")
        db.add(q)
        db.flush()
        queue_id = q.id
    r = client.post(f"/api/v1/queue/{queue_id}/diagnose", headers=admin_headers)
    assert r.status_code == 409
