"""File storage selection: Azure Blob Storage when configured, else Vercel Blob (run: python tests/test_storage.py).
No network: the Azure container client is stubbed; the SDK is only imported and constructed."""
import importlib.util
import os
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
_spec = importlib.util.spec_from_file_location("stockscan_api_storage", ROOT / "api" / "index.py")
api = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(api)

VARS = ("AZURE_STORAGE_CONNECTION_STRING", "AZURE_STORAGE_CONTAINER", "BLOB_READ_WRITE_TOKEN", "VERCEL_BLOB_READ_WRITE_TOKEN")


def _env(**values):
    saved = {k: os.environ.pop(k, None) for k in VARS}
    os.environ.update(values)
    return saved


def _restore(saved):
    for k in VARS:
        os.environ.pop(k, None)
        if saved[k] is not None:
            os.environ[k] = saved[k]


def test_backend_selection():
    saved = _env()
    try:
        assert api.storage_backend() is None and not api.blob_configured()
        os.environ["BLOB_READ_WRITE_TOKEN"] = "x"
        assert api.storage_backend() == "vercel" and api.storage_label() == "Vercel Blob"
        os.environ["AZURE_STORAGE_CONNECTION_STRING"] = "x"
        assert api.storage_backend() == "azure" and api.storage_label() == "Azure Blob Storage"
        assert api.azure_container_name() == "stockscan"
        os.environ["AZURE_STORAGE_CONTAINER"] = " Reports "
        assert api.azure_container_name() == "reports"
    finally:
        _restore(saved)


def test_azure_put_and_get_use_the_same_pathname():
    stored = {}

    class Blob:
        def __init__(self, name):
            self.url = f"https://acct.blob.core.windows.net/stockscan/{name}"

        def readall(self):
            return stored[self.name]

    class Container:
        def upload_blob(self, name, body, overwrite, content_settings):
            assert overwrite is True
            stored[name] = (body, content_settings.content_type)
            return Blob(name)

        def download_blob(self, name):
            b = Blob(name)
            b.readall = lambda: stored[name][0]
            return b

    saved = _env(AZURE_STORAGE_CONNECTION_STRING="x")
    old = api._azure_container
    api._azure_container = lambda: Container()
    try:
        r = api.upload_report("reports/warren-buffett/AAPL/2026/2026-03-02-7.pdf", b"%PDF")
        assert r["pathname"] == "reports/warren-buffett/AAPL/2026/2026-03-02-7.pdf" and r["url"].endswith(r["pathname"])
        assert stored[r["pathname"]] == (b"%PDF", "application/pdf")
        assert api.download_report(r["pathname"]) == b"%PDF"
        api.upload_html_report("reports/x.html", "<p>é</p>")
        assert stored["reports/x.html"] == ("<p>é</p>".encode(), "text/html; charset=utf-8")
    finally:
        api._azure_container = old
        _restore(saved)


def test_azure_sdk_names_exist():
    from azure.core.exceptions import HttpResponseError, ResourceExistsError  # noqa: F401
    from azure.storage.blob import BlobServiceClient, ContentSettings

    svc = BlobServiceClient.from_connection_string(
        "DefaultEndpointsProtocol=https;AccountName=example;AccountKey=ZmFrZQ==;EndpointSuffix=core.windows.net")
    c = svc.get_container_client("stockscan")
    assert all(hasattr(c, m) for m in ("create_container", "upload_blob", "download_blob"))
    assert ContentSettings(content_type="application/pdf").content_type == "application/pdf"


def test_connection_string_is_treated_as_secret():
    assert "AZURE_STORAGE_CONNECTION_STRING" in api.SECRET_VARS


if __name__ == "__main__":
    for name, fn in list(globals().items()):
        if name.startswith("test_"):
            fn()
            print("ok", name)
