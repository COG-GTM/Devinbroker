"""Authentication and validation for the AIS-catcher HTTP ingest endpoint.

POST /api/ais/feed writes into the shared vessel table that every viewer
sees, so it must only accept well-formed data from a trusted receiver.
"""

import base64
import binascii
import hmac
import math
import os

MAX_BODY_BYTES = 1024 * 1024
MAX_MSGS_PER_REQUEST = 1000
MAX_STRING_LEN = 64
MAX_MMSI = 999_999_999

POSITION_TYPES = (1, 2, 3, 18, 19)
STATIC_TYPES = (5, 24)

_STRING_FIELDS = ("shipname", "callsign", "destination")


class FeedError(Exception):
    def __init__(self, status_code: int, detail: str):
        super().__init__(detail)
        self.status_code = status_code
        self.detail = detail


def get_feed_token() -> str:
    return os.environ.get("AIS_FEED_TOKEN", "").strip()


def _presented_tokens(headers, query_params) -> list[str]:
    tokens = []
    header_token = headers.get("x-ais-feed-token")
    if header_token:
        tokens.append(header_token.strip())
    auth = headers.get("authorization", "")
    scheme, _, value = auth.partition(" ")
    if scheme.lower() == "bearer" and value:
        tokens.append(value.strip())
    elif scheme.lower() == "basic" and value:
        try:
            decoded = base64.b64decode(value.strip(), validate=True).decode("utf-8")
            tokens.append(decoded.partition(":")[2])
        except (binascii.Error, UnicodeDecodeError):
            pass
    query_token = query_params.get("token")
    if query_token:
        tokens.append(query_token.strip())
    return tokens


def check_feed_token(headers, query_params) -> None:
    """Raise FeedError unless the request carries the configured AIS_FEED_TOKEN.

    Accepted carriers: ``X-AIS-Feed-Token`` header, ``Authorization: Bearer``,
    the password of ``Authorization: Basic``, or a ``?token=`` query parameter.
    The endpoint is disabled (403) when AIS_FEED_TOKEN is not configured.
    """
    expected = get_feed_token()
    if not expected:
        raise FeedError(403, "AIS feed disabled — set AIS_FEED_TOKEN to enable local AIS ingest")
    presented = _presented_tokens(headers, query_params)
    if not presented:
        raise FeedError(401, "Missing AIS feed token")
    expected_b = expected.encode("utf-8")
    if not any(hmac.compare_digest(t.encode("utf-8"), expected_b) for t in presented):
        raise FeedError(403, "Invalid AIS feed token")


def _is_number(value) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value)


def _is_int(value) -> bool:
    return isinstance(value, int) and not isinstance(value, bool)


def _check_number(msg: dict, key: str, lo: float, hi: float) -> None:
    if key in msg and msg[key] is not None:
        value = msg[key]
        if not _is_number(value) or not lo <= value <= hi:
            raise ValueError(f"'{key}' must be a number in [{lo}, {hi}]")


def validate_message(msg) -> None:
    """Raise ValueError if an AIS-catcher message is malformed."""
    if not isinstance(msg, dict):
        raise ValueError("message must be an object")
    mmsi = msg.get("mmsi")
    if not _is_int(mmsi) or not 0 < mmsi <= MAX_MMSI:
        raise ValueError(f"'mmsi' must be an integer in [1, {MAX_MMSI}]")
    msg_type = msg.get("type", 0)
    if not _is_int(msg_type) or not 0 <= msg_type <= 63:
        raise ValueError("'type' must be an integer in [0, 63]")

    # 91 / 181 are the AIS "position not available" sentinels.
    _check_number(msg, "lat", -90.0, 91.0)
    _check_number(msg, "lon", -180.0, 181.0)
    _check_number(msg, "speed", 0.0, 102.3)
    _check_number(msg, "course", 0.0, 360.0)
    if "heading" in msg and msg["heading"] is not None:
        heading = msg["heading"]
        if not _is_number(heading) or not (0 <= heading <= 359 or heading == 511):
            raise ValueError("'heading' must be a number in [0, 359] or 511")

    for key in _STRING_FIELDS:
        if key in msg and msg[key] is not None:
            value = msg[key]
            if not isinstance(value, str) or len(value) > MAX_STRING_LEN:
                raise ValueError(f"'{key}' must be a string of at most {MAX_STRING_LEN} characters")

    for key in ("imo", "shiptype"):
        if key in msg and msg[key] is not None:
            value = msg[key]
            if not _is_int(value) or not 0 <= value <= MAX_MMSI:
                raise ValueError(f"'{key}' must be a non-negative integer")


def parse_feed_body(body) -> list[dict]:
    """Validate a decoded feed body and return its message list."""
    if not isinstance(body, dict):
        raise FeedError(400, "body must be a JSON object")
    msgs = body.get("msgs", [])
    if msgs is None:
        msgs = []
    if not isinstance(msgs, list):
        raise FeedError(400, "'msgs' must be a list")
    if len(msgs) > MAX_MSGS_PER_REQUEST:
        raise FeedError(413, f"too many messages (max {MAX_MSGS_PER_REQUEST} per request)")
    for i, msg in enumerate(msgs):
        try:
            validate_message(msg)
        except ValueError as e:
            raise FeedError(400, f"msgs[{i}]: {e}")
    return msgs


async def read_limited_body(request, limit: int = MAX_BODY_BYTES) -> bytes:
    """Read the request body, raising FeedError(413) once it exceeds ``limit``."""
    content_length = request.headers.get("content-length")
    if content_length:
        try:
            if int(content_length) > limit:
                raise FeedError(413, f"body too large (max {limit} bytes)")
        except ValueError:
            raise FeedError(400, "invalid Content-Length")
    chunks = []
    size = 0
    async for chunk in request.stream():
        size += len(chunk)
        if size > limit:
            raise FeedError(413, f"body too large (max {limit} bytes)")
        chunks.append(chunk)
    return b"".join(chunks)
