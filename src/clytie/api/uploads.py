"""HTTP contract for the companion-owned plugin upload area."""

import re

from .auth import APIError

START = "/companions/<cid>/uploads"
CHUNK = START + "/<upload_id>/<sequence>"
COMPLETE = START + "/<upload_id>/complete"
CHUNK_BODY_LIMIT = 1024 * 1024
OPERATIONS = {START: "upload/start", CHUNK: "upload/chunk", COMPLETE: "upload/complete"}


def validate(route, params, data):
    fields = {START: {"plugin", "size", "sha256"}, CHUNK: {"content"}, COMPLETE: set()}
    if set(data) != fields[route]:
        raise APIError("invalid_request")
    if route == START:
        if (
            not isinstance(data["plugin"], str)
            or len(data["plugin"]) > 255
            or not re.fullmatch(r"[a-z][a-z0-9]*(?:-[a-z0-9]+)*", data["plugin"])
            or type(data["size"]) is not int
            or data["size"] < 0
            or not isinstance(data["sha256"], str)
            or not re.fullmatch("[0-9a-f]{64}", data["sha256"])
        ):
            raise APIError("invalid_request")
    elif not re.fullmatch("[0-9a-f]{32}", params["upload_id"]):
        raise APIError("invalid_request")
    if route == CHUNK:
        # Reject JSON escapes/control bytes before framing the base64 over IPC.
        if not re.fullmatch("0|[1-9][0-9]{0,7}", params["sequence"]) or not re.fullmatch(
            "[A-Za-z0-9+/]+={0,2}", data["content"]
        ):
            raise APIError("invalid_request")
