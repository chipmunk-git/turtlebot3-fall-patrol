"""Observation identity and append-only note formatting, independent of ROS."""

import json
import math
import uuid

CLASSIFICATIONS = ("도움 필요", "도움 불필요", "확인 필요")


class VoiceLink:
    def __init__(self, timeout=90.0):
        self.timeout = timeout
        self.request = None
        self.result = None
        self.cancel = None

    def start(self, now):
        self.request = {"request_id": str(uuid.uuid4()), "action": "observe",
                        "issued_at": now, "expires_at": now + self.timeout}
        self.result = None
        self.cancel = None

    def end(self):
        if self.request:
            self.cancel = dict(self.request, action="cancel")
        self.request = None
        self.result = None

    def message(self, now):
        if self.request and self.result is None:
            if now >= self.request["expires_at"]:
                self.accept({"request_id": self.request["request_id"], "status": "error",
                             "stage": "timeout", "message": "Pi 음성 결과가 제한 시간 안에 도착하지 않았습니다."}, now,
                            allow_expired=True)
                self.cancel = dict(self.request, action="cancel")
            else:
                return dict(self.request)
        return self.cancel

    def accept(self, data, now, allow_expired=False):
        if (not isinstance(data, dict) or not self.request or self.result is not None
                or data.get("request_id") != self.request["request_id"]
                or (now >= self.request["expires_at"] and not allow_expired)
                or data.get("status") not in ("ok", "error")):
            return False
        if data["status"] == "ok" and (not isinstance(data.get("text"), str)
                or data.get("classification") not in CLASSIFICATIONS):
            return False
        self.result = {"request_id": self.request["request_id"], "status": data["status"],
                       "text": str(data.get("text", ""))[:2000],
                       "classification": data.get("classification", "확인 필요"),
                       "stage": str(data.get("stage", ""))[:100],
                       "message": str(data.get("message", ""))[:1000]}
        if self.result["status"] == "error" or not self.result["text"].strip():
            self.result["classification"] = "확인 필요"
        return True


def append_note(existing, marker, text):
    """Keep existing notes and make a retried PATCH content-idempotent.

    The current server has no compare-and-swap API: simultaneous edits from
    another client can still race between GET and PATCH.
    """
    existing = existing or ""
    if marker in existing:
        return existing
    return "\n".join(part for part in (existing, marker + " " + text) if part)


def voice_note(result):
    if result["status"] == "error":
        return "음성 처리 실패 / 확인 필요 / " + result["stage"] + ": " + result["message"]
    # JSON quoting keeps a recognized newline from looking like another log entry.
    return ("음성 인식: " + json.dumps(result["text"], ensure_ascii=False)
            + " / 분류: " + result["classification"]
            + " / 실제 발화·낙상 여부를 확정하지 않으며 사건 상태는 변경하지 않음")


def valid_request(data, now):
    if not isinstance(data, dict) or data.get("action") not in ("observe", "cancel"):
        return False
    request_id = data.get("request_id")
    if not isinstance(request_id, str) or not 1 <= len(request_id) <= 100:
        return False
    try:
        issued, expires = float(data["issued_at"]), float(data["expires_at"])
        return (math.isfinite(issued) and math.isfinite(expires)
                and issued <= now + 2.0 and 0 < expires - issued <= 120.0
                and (data["action"] == "cancel" or now < expires))
    except (KeyError, ValueError, TypeError):
        return False
