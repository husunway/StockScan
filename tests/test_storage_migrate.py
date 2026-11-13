"""Vercel Blob -> Azure Blob Storage migration endpoints (run: python tests/test_storage_migrate.py).
Both stores are stubbed: no network."""
import importlib.util
import os
import sys
import types
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
_spec = importlib.util.spec_from_file_location("stockscan_api_migrate", ROOT / "api" / "index.py")
api = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(api)

VERCEL = {
    "reports/warren-buffett/AAPL/2026/2026-03-02-7.pdf": (b"%PDF-1", "application/pdf"),
    "reports/warren-buffett/AAPL/2026/2026-03-02-7.html": (b"<html>", "text/html; charset=utf-8"),
    "data/AAPL/latest.json": (b"{}", "application/json"),
    "broken.json": (None, None),
}


class _Blob:
    def __init__(self, name, size):
        self.name, self.size = name, size


class _Container:
    def __init__(self):
        self.files = {"data/AAPL/latest.json": (b"{}", "application/json")}

    def list_blobs(self):
        return [_Blob(n, len(b)) for n, (b, _) in self.files.items()]

    def upload_blob(self, name, body, overwrite, content_settings):
        self.files[name] = (body, content_settings.content_type)


def _fake_vercel():
    mod = types.ModuleType("vercel.blob.sync")

    def query_blobs(page_size=None):
        return [types.SimpleNamespace(pathname=p, size=len(b or b"xx")) for p, (b, _) in VERCEL.items()]

    def get_blob(pathname, access):
        body, ctype = VERCEL[pathname]
        if body is None:
            raise RuntimeError("BlobStoreSuspendedError")
        return types.SimpleNamespace(body=body, metadata=types.SimpleNamespace(content_type=ctype))

    mod.query_blobs, mod.get_blob = query_blobs, get_blob
    return mod


def _run(fn):
    saved_env = {k: os.environ.get(k) for k in ("AZURE_STORAGE_CONNECTION_STRING", "BLOB_READ_WRITE_TOKEN")}
    saved_mod = sys.modules.get("vercel.blob.sync")
    saved_container = api._azure_container
    container = _Container()
    os.environ["AZURE_STORAGE_CONNECTION_STRING"] = "x"
    os.environ["BLOB_READ_WRITE_TOKEN"] = "x"
    sys.modules["vercel.blob.sync"] = _fake_vercel()
    api._azure_container = lambda: container
    try:
        return fn(container)
    finally:
        api._azure_container = saved_container
        if saved_mod is None:
            sys.modules.pop("vercel.blob.sync", None)
        else:
            sys.modules["vercel.blob.sync"] = saved_mod
        for k, v in saved_env.items():
            os.environ.pop(k, None)
            if v is not None:
                os.environ[k] = v


def test_plan_marks_files_already_in_azure():
    def check(_):
        p = api.storage_migrate_plan()
        assert p["total"] == 4 and p["present"] == 1 and p["target_files"] == 1
        assert p["folders"] == 2  # reports/warren-buffett/AAPL/2026 and data/AAPL
        assert {i["pathname"]: i["present"] for i in p["items"]}["data/AAPL/latest.json"] is True
    _run(check)


def test_copy_keeps_pathnames_and_content_types_and_survives_a_bad_file():
    def check(container):
        names = [p for p in VERCEL if p != "data/AAPL/latest.json"]
        r = api.storage_migrate_copy(api.StorageCopyRequest(pathnames=names + ["../etc/passwd"]))
        assert r["copied"] == 2 and r["failed"] == 2, r
        assert container.files["reports/warren-buffett/AAPL/2026/2026-03-02-7.pdf"] == (b"%PDF-1", "application/pdf")
        assert container.files["reports/warren-buffett/AAPL/2026/2026-03-02-7.html"][1] == "text/html; charset=utf-8"
        errors = {x["pathname"]: x.get("error") for x in r["results"] if not x["ok"]}
        assert "Suspended" in errors["broken.json"] and errors["../etc/passwd"] == "invalid pathname"
    _run(check)


def test_needs_both_stores():
    saved = {k: os.environ.pop(k, None) for k in ("AZURE_STORAGE_CONNECTION_STRING", "BLOB_READ_WRITE_TOKEN", "VERCEL_BLOB_READ_WRITE_TOKEN")}
    try:
        try:
            api.storage_migrate_plan()
            raise AssertionError("expected 422")
        except api.HTTPException as e:
            assert e.status_code == 422 and "BLOB_READ_WRITE_TOKEN" in e.detail and "AZURE_STORAGE_CONNECTION_STRING" in e.detail
    finally:
        for k, v in saved.items():
            if v is not None:
                os.environ[k] = v


if __name__ == "__main__":
    for name, fn in list(globals().items()):
        if name.startswith("test_"):
            fn()
            print("ok", name)
