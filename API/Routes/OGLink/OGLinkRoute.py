"""OG-CLEWS link endpoints.

All routes live under /oglink. They are session-free by design (like /run and
/createCaseRun): the callers are headless -- the ogclews-link CLI, the post-run
hook, and MUIOGO-AI over HTTP -- and every request names its case explicitly.
No route here ever writes into a live case; patches land in case copies.
"""
import json
import os
from pathlib import Path
from urllib.parse import urlparse

from flask import Blueprint, jsonify, request

from Classes.OGLink.PatchApply import OGLinkPatch, OGLinkPatchError

oglink_api = Blueprint("OGLinkRoute", __name__, url_prefix="/oglink")

_LOCAL_HOSTS = {"127.0.0.1", "localhost", "::1"}


def _err(message, http=400, status="error", **extra):
    payload = {"message": message, "status_code": status}
    payload.update(extra)
    return jsonify(payload), http


def _blocked_cross_site():
    """Refuse a state-changing request driven cross-site from a browser; same
    rule as the OGCore install routes (non-browser callers send no Origin)."""
    origin = request.headers.get("Origin")
    if origin:
        host = urlparse(origin).hostname
        if host not in _LOCAL_HOSTS:
            return _err("Cross-site request refused.", http=403)
    return None


@oglink_api.route("/applyPatch", methods=["POST"])
def applyPatch():
    """Materialize a clews_patch.json into a solved caserun of a case copy.

    Body: {"patch": {...}} or {"patch_path": "/abs/path/clews_patch.json"},
    plus "base_caserun" (required; the caserun whose scenario set the new run
    clones), and optional "caserun_name", "copy_name", "solver" (default CBC),
    "overwrite_copy". Solves synchronously, like /run.
    """
    blocked = _blocked_cross_site()
    if blocked:
        return blocked
    data = request.get_json(silent=True)
    if data is None:
        return _err("Request body must be valid JSON.")

    patch = data.get("patch")
    patch_path = data.get("patch_path")
    if patch is None and patch_path:
        if "\x00" in str(patch_path):
            return _err("patch_path is not a valid path.")
        path = Path(os.path.abspath(os.path.normpath(str(patch_path))))
        if not path.is_file():
            return _err(f"patch_path does not exist: {path}", http=404)
        try:
            patch = json.loads(path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError) as exc:
            return _err(f"patch_path is not readable JSON: {exc}")
    if not isinstance(patch, dict):
        return _err("Provide the patch inline ('patch') or as a file "
                    "('patch_path').")

    base_caserun = data.get("base_caserun")
    if not base_caserun:
        return _err("Missing required field: base_caserun")

    try:
        result = OGLinkPatch.apply(
            patch,
            base_caserun,
            caserun_name=data.get("caserun_name") or None,
            copy_name=data.get("copy_name") or None,
            solver=data.get("solver") or "CBC",
            overwrite_copy=bool(data.get("overwrite_copy")),
        )
    except OGLinkPatchError as exc:
        return _err(exc.message, http=exc.http, **exc.details)
    except PermissionError as exc:  # Config.validate_path on a hostile name
        return _err(str(exc))
    except ValueError as exc:  # a non-numeric change value
        return _err(str(exc))

    result["status_code"] = "success"
    result["message"] = "Patch applied and solved."
    return jsonify(result), 200
