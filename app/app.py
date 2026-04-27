"""Demo Databricks App: probes UC Volume access and uploads/processes files.

Status panel uses the Databricks SDK to test each privilege level
(USE_CATALOG, USE_SCHEMA, READ_VOLUME, WRITE_VOLUME) by attempting an
operation that requires it. File ops use the SDK files API because the
FUSE-mounted /Volumes path is read-only inside Databricks Apps.
"""

from __future__ import annotations

import io
import os
import pathlib
from datetime import datetime, timezone

from databricks.sdk import WorkspaceClient
from databricks.sdk.errors import NotFound
from flask import Flask, jsonify, render_template, request, send_file, abort, Response

CATALOG = os.environ["CATALOG"]
SCHEMA = os.environ["SCHEMA"]
VOLUME = os.environ["VOLUME"]
VOLUME_ROOT = f"/Volumes/{CATALOG}/{SCHEMA}/{VOLUME}"
UPLOADS_DIR = f"{VOLUME_ROOT}/uploads"

app = Flask(__name__)
app.config["MAX_CONTENT_LENGTH"] = 50 * 1024 * 1024  # 50 MB


def _w() -> WorkspaceClient:
    return WorkspaceClient()


def _short(err: BaseException) -> str:
    msg = str(err)
    return msg if len(msg) < 200 else msg[:197] + "..."


def check_use_catalog():
    try:
        next(iter(_w().schemas.list(catalog_name=CATALOG)), None)
        return True, "schemas.list succeeded"
    except Exception as e:
        return False, _short(e)


def check_use_schema():
    try:
        next(iter(_w().volumes.list(catalog_name=CATALOG, schema_name=SCHEMA)), None)
        return True, "volumes.list succeeded"
    except Exception as e:
        return False, _short(e)


def check_read_volume():
    try:
        list(_w().files.list_directory_contents(VOLUME_ROOT))
        return True, "files.list_directory_contents succeeded"
    except Exception as e:
        return False, _short(e)


def check_write_volume():
    probe = f"{VOLUME_ROOT}/.health-probe"
    try:
        _w().files.upload(probe, io.BytesIO(b"probe"), overwrite=True)
        _w().files.delete(probe)
        return True, "files.upload+delete succeeded"
    except Exception as e:
        return False, _short(e)


@app.get("/api/status")
def api_status():
    checks = [
        ("USE_CATALOG", "catalog", check_use_catalog),
        ("USE_SCHEMA", "schema", check_use_schema),
        ("READ_VOLUME", "volume_read", check_read_volume),
        ("WRITE_VOLUME", "volume_write", check_write_volume),
    ]
    out = []
    for label, key, fn in checks:
        ok, detail = fn()
        out.append({"label": label, "key": key, "ok": ok, "detail": detail})
    return jsonify(
        target={"catalog": CATALOG, "schema": SCHEMA, "volume": VOLUME,
                "path": VOLUME_ROOT},
        checks=out,
        ts=datetime.now(timezone.utc).isoformat(),
    )


@app.get("/api/files")
def api_files():
    try:
        entries = list(_w().files.list_directory_contents(UPLOADS_DIR))
    except NotFound:
        return jsonify(files=[])
    except Exception as e:
        return jsonify(files=[], error=_short(e))
    items = []
    for e in entries:
        if e.is_directory:
            continue
        mod = None
        if getattr(e, "last_modified", None):
            try:
                mod = datetime.fromtimestamp(e.last_modified / 1000, tz=timezone.utc).isoformat()
            except Exception:
                mod = None
        items.append({
            "name": e.name,
            "size": e.file_size or 0,
            "modified": mod,
        })
    items.sort(key=lambda x: x["name"])
    return jsonify(files=items)


@app.post("/api/upload")
def api_upload():
    f = request.files.get("file")
    if not f or not f.filename:
        return jsonify(ok=False, error="no file in form"), 400
    safe_name = pathlib.Path(f.filename).name
    target = f"{UPLOADS_DIR}/{safe_name}"
    try:
        _w().files.upload(target, f.stream, overwrite=True)
    except Exception as e:
        return jsonify(ok=False, error=_short(e)), 500
    try:
        md = _w().files.get_metadata(target)
        size = md.content_length or 0
    except Exception:
        size = 0
    return jsonify(ok=True, name=safe_name, size=size)


def _read_bytes(path: str) -> bytes:
    resp = _w().files.download(path)
    return resp.contents.read()


@app.post("/api/process/<name>")
def api_process(name: str):
    safe = pathlib.Path(name).name
    path = f"{UPLOADS_DIR}/{safe}"
    try:
        data = _read_bytes(path)
    except NotFound:
        abort(404)
    except Exception as e:
        return jsonify(ok=False, error=_short(e)), 500
    try:
        text = data.decode("utf-8")
        is_text = True
    except UnicodeDecodeError:
        text = ""
        is_text = False
    summary = {
        "name": safe,
        "bytes": len(data),
        "is_text": is_text,
        "lines": (text.count("\n") + (1 if text and not text.endswith("\n") else 0))
                 if is_text else None,
        "words": len(text.split()) if is_text else None,
        "preview": text[:500] if is_text else None,
    }
    return jsonify(ok=True, summary=summary)


@app.get("/api/file/<name>")
def api_download(name: str):
    safe = pathlib.Path(name).name
    path = f"{UPLOADS_DIR}/{safe}"
    try:
        data = _read_bytes(path)
    except NotFound:
        abort(404)
    return Response(
        data,
        mimetype="application/octet-stream",
        headers={"Content-Disposition": f'attachment; filename="{safe}"'},
    )


@app.delete("/api/file/<name>")
def api_delete(name: str):
    safe = pathlib.Path(name).name
    path = f"{UPLOADS_DIR}/{safe}"
    try:
        _w().files.delete(path)
    except NotFound:
        abort(404)
    except Exception as e:
        return jsonify(ok=False, error=_short(e)), 500
    return jsonify(ok=True)


@app.get("/")
def index():
    return render_template("index.html",
                           catalog=CATALOG, schema=SCHEMA, volume=VOLUME)


if __name__ == "__main__":
    port = int(os.environ.get("DATABRICKS_APP_PORT", "8000"))
    app.run(host="0.0.0.0", port=port)
