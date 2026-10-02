import time

import httpx


def trigger_and_wait_for_sync(scrob_api: httpx.Client, source: str, timeout: float = 60.0) -> dict:
    """POSTs /sync/{source} (a full pull) and blocks until that job leaves
    pending/running. Returns the finished SyncJob dict."""
    r = scrob_api.post(f"/sync/{source}")
    r.raise_for_status()
    return _wait_for_job(scrob_api, r.json()["job_id"], timeout)


def _wait_for_job(scrob_api: httpx.Client, job_id: int, timeout: float) -> dict:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        jobs = scrob_api.get("/sync/status").json()
        job = next((j for j in jobs if j["id"] == job_id), None)
        if job and job["status"] not in ("pending", "running"):
            return job
        time.sleep(1.5)
    raise TimeoutError(f"sync job {job_id} did not finish within {timeout}s")


def trigger_and_wait_for_push(scrob_api: httpx.Client, connection_id: int, timeout: float = 60.0) -> dict:
    """POSTs /sync/connection/{id}/push (a full upstream push, _run_full_push)
    and blocks until that job leaves pending/running. Returns the finished
    SyncJob dict."""
    r = scrob_api.post(f"/sync/connection/{connection_id}/push")
    r.raise_for_status()
    return _wait_for_job(scrob_api, r.json()["job_id"], timeout)


def find_media_by_tmdb_id(scrob_api: httpx.Client, media_type: str, tmdb_id: int) -> dict | None:
    r = scrob_api.get("/media", params={"type": media_type, "page_size": 100})
    r.raise_for_status()
    for item in r.json().get("results", []):
        if item.get("tmdb_id") == tmdb_id:
            return item
    return None
