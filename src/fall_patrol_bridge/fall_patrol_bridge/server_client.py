import json
import urllib.error
import urllib.parse
import urllib.request
import uuid
from typing import Any

ROBOT_STATES = ("idle", "patrol", "suspect", "approach", "observe", "alert")
EVENT_RESULTS = ("confirmed", "uncertain", "false_alarm")
SNAPSHOT_KINDS = ("detected", "observing", "decision")

Image = tuple[bytes, str]


class ServerError(Exception):
    pass


class ServerClient:
    def __init__(self, base_url: str = "http://127.0.0.1:8000", timeout: float = 2.0) -> None:
        self.base_url = base_url.rstrip("/")
        self.timeout = timeout

    def _request(
        self, method: str, path: str, body: bytes | None = None, content_type: str | None = None
    ) -> Any:
        request = urllib.request.Request(f"{self.base_url}{path}", data=body, method=method)
        if content_type:
            request.add_header("Content-Type", content_type)
        try:
            with urllib.request.urlopen(request, timeout=self.timeout) as response:
                return json.loads(response.read())
        except (urllib.error.URLError, TimeoutError, ValueError) as error:
            raise ServerError(f"{method} {path}: {error}") from error

    def _send_json(self, method: str, path: str, payload: dict[str, Any]) -> Any:
        return self._request(method, path, json.dumps(payload).encode(), "application/json")

    def _send_multipart(self, path: str, fields: dict[str, Any], files: dict[str, Image]) -> Any:
        boundary = uuid.uuid4().hex
        body = b"".join(
            f'--{boundary}\r\nContent-Disposition: form-data; name="{name}"\r\n\r\n{value}\r\n'.encode()
            for name, value in fields.items()
            if value is not None
        )
        for name, (data, content_type) in files.items():
            extension = "png" if content_type == "image/png" else "jpg"
            body += (
                f"--{boundary}\r\n"
                f'Content-Disposition: form-data; name="{name}"; filename="{name}.{extension}"\r\n'
                f"Content-Type: {content_type}\r\n\r\n"
            ).encode()
            body += data + b"\r\n"
        body += f"--{boundary}--\r\n".encode()
        return self._request("POST", path, body, f"multipart/form-data; boundary={boundary}")

    def put_status(
        self,
        state: str,
        x: float | None = None,
        y: float | None = None,
        yaw: float | None = None,
        waypoint: int | None = None,
    ) -> None:
        self._send_json(
            "PUT", "/status", {"state": state, "x": x, "y": y, "yaw": yaw, "current_waypoint": waypoint}
        )

    def is_ignored(self, x: float, y: float) -> bool:
        query = urllib.parse.urlencode({"x": x, "y": y})
        try:
            return bool(self._request("GET", f"/ignore-zones/check?{query}")["ignored"])
        except ServerError:
            return False

    def create_event(
        self,
        x: float | None,
        y: float | None,
        confidence: float | None = None,
        detected_image: Image | None = None,
        observing_image: Image | None = None,
    ) -> int:
        files = {
            name: image
            for name, image in (("detected_snapshot", detected_image), ("observing_snapshot", observing_image))
            if image is not None
        }
        fields = {"x": x, "y": y, "confidence": confidence, "status": "observing"}
        return int(self._send_multipart("/events", fields, files)["id"])

    def add_snapshot(self, event_id: int, kind: str, image: Image) -> None:
        self._send_multipart(f"/events/{event_id}/snapshots", {"kind": kind}, {"snapshot": image})

    def set_result(self, event_id: int, status: str) -> None:
        self._send_json("PATCH", f"/events/{event_id}", {"status": status})
