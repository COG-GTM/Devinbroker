import json
import logging
import shutil
import subprocess
import threading
import time
from urllib.parse import urljoin, urlparse

import requests
from requests.adapters import HTTPAdapter
from urllib3.connection import HTTPConnection, HTTPSConnection
from urllib3.connectionpool import HTTPConnectionPool, HTTPSConnectionPool
from urllib3.util.retry import Retry

from services.url_safety import UnsafeURLError, is_public_ip, validate_public_url

logger = logging.getLogger(__name__)

# Reusable session with connection pooling and retry logic.
# Only retry once (total=1) to fail fast — the curl fallback is the real safety net.
_session = requests.Session()
_retry = Retry(total=1, backoff_factor=0.3, status_forcelist=[502, 503, 504])
_session.mount("https://", HTTPAdapter(max_retries=_retry, pool_maxsize=20))
_session.mount("http://", HTTPAdapter(max_retries=_retry, pool_maxsize=10))

# Find bash for curl fallback — Git bash's curl has the TLS features
# needed to pass CDN fingerprint checks (brotli, zstd, libpsl)
_BASH_PATH = shutil.which("bash") or "bash"

# Cache domains where requests fails — skip straight to curl for 5 minutes
_domain_fail_cache: dict[str, float] = {}
_DOMAIN_FAIL_TTL = 300  # 5 minutes

# Circuit breaker: track domains where BOTH requests AND curl fail
# If a domain failed completely within the last 2 minutes, skip it entirely
_circuit_breaker: dict[str, float] = {}
_CIRCUIT_BREAKER_TTL = 120  # 2 minutes

# Lock protecting _domain_fail_cache and _circuit_breaker mutations
_cb_lock = threading.Lock()

def _curl_base_cmd(timeout):
    """Common curl arguments: http(s) only (including redirects), bounded runtime."""
    curl_path = shutil.which("curl") or "curl"
    return [curl_path, "-s", "-w", "\n%{http_code}",
            "--proto", "=http,https", "--proto-redir", "=http,https",
            "--max-time", str(timeout)]


class _DummyResponse:
    """Minimal response object matching requests.Response interface."""
    def __init__(self, status_code, text):
        self.status_code = status_code
        self.text = text
        self.content = text.encode('utf-8', errors='replace')

    def json(self):
        return json.loads(self.text)

    def raise_for_status(self):
        if self.status_code >= 400:
            raise Exception(f"HTTP {self.status_code}: {self.text[:100]}")


def fetch_with_curl(url, method="GET", json_data=None, timeout=15, headers=None):
    """Wrapper to bypass aggressive local firewall that blocks Python but permits curl.

    Falls back to running curl through Git bash, which has the TLS features
    (brotli, zstd, libpsl) needed to pass CDN fingerprint checks that block
    both Python requests and the barebones Windows system curl.
    """
    default_headers = {
        "User-Agent": "ShadowBroker-OSINT/1.0 (live-risk-dashboard)",
    }
    if headers:
        default_headers.update(headers)

    if urlparse(url).scheme.lower() not in ("http", "https"):
        raise UnsafeURLError(f"Refusing to fetch non-http(s) URL: {url!r}")

    domain = urlparse(url).netloc

    # Circuit breaker: if domain failed completely <2min ago, fail fast
    with _cb_lock:
        if domain in _circuit_breaker and (time.time() - _circuit_breaker[domain]) < _CIRCUIT_BREAKER_TTL:
            raise Exception(f"Circuit breaker open for {domain} (failed <{_CIRCUIT_BREAKER_TTL}s ago)")

    # Check if this domain recently failed with requests — skip straight to curl
    with _cb_lock:
        _skip_requests = domain in _domain_fail_cache and (time.time() - _domain_fail_cache[domain]) < _DOMAIN_FAIL_TTL
    if not _skip_requests:
        try:
            # Use a short connect timeout (3s) so firewall blocks fail fast,
            # but allow the full timeout for reading the response body.
            req_timeout = (min(3, timeout), timeout)
            if method == "POST":
                res = _session.post(url, json=json_data, timeout=req_timeout, headers=default_headers)
            else:
                res = _session.get(url, timeout=req_timeout, headers=default_headers)
            res.raise_for_status()
            # Clear failure caches on success
            with _cb_lock:
                _domain_fail_cache.pop(domain, None)
                _circuit_breaker.pop(domain, None)
            return res
        except (requests.RequestException, ConnectionError, TimeoutError, OSError) as e:
            logger.warning(f"Python requests failed for {url} ({e}), falling back to bash curl...")
            with _cb_lock:
                _domain_fail_cache[domain] = time.time()

    # Curl fallback — reached from both _skip_requests and requests-exception paths
    cmd = _curl_base_cmd(timeout)
    for k, v in default_headers.items():
        cmd += ["-H", f"{k}: {v}"]
    if method == "POST" and json_data:
        cmd += ["-X", "POST", "-H", "Content-Type: application/json",
                "--data-binary", "@-"]
    cmd += ["--", url]

    try:
        stdin_data = json.dumps(json_data) if (method == "POST" and json_data) else None
        res = subprocess.run(
            cmd, capture_output=True, text=True, timeout=timeout + 5,
            input=stdin_data
        )
        if res.returncode == 0 and res.stdout.strip():
            # Parse HTTP status code from -w output (last line)
            lines = res.stdout.rstrip().rsplit("\n", 1)
            body = lines[0] if len(lines) > 1 else res.stdout
            http_code = int(lines[-1]) if len(lines) > 1 and lines[-1].strip().isdigit() else 200
            if http_code < 400:
                with _cb_lock:
                    _circuit_breaker.pop(domain, None)  # Clear circuit breaker on success
            return _DummyResponse(http_code, body)
        else:
            logger.error(f"bash curl fallback failed: exit={res.returncode} stderr={res.stderr[:200]}")
            with _cb_lock:
                _circuit_breaker[domain] = time.time()
            return _DummyResponse(500, "")
    except (subprocess.SubprocessError, ConnectionError, TimeoutError, OSError) as curl_e:
        logger.error(f"bash curl fallback exception: {curl_e}")
        with _cb_lock:
            _circuit_breaker[domain] = time.time()
        return _DummyResponse(500, "")


# ---------------------------------------------------------------------------
# Fetching user-supplied URLs (e.g. custom news feeds)
# ---------------------------------------------------------------------------
FEED_MAX_BYTES = 5 * 1024 * 1024
FEED_MAX_REDIRECTS = 3


def _check_peer_is_public(sock):
    """Close and reject sockets connected to a non-public address (DNS rebinding guard)."""
    try:
        peer_ip = sock.getpeername()[0]
    except OSError:
        sock.close()
        raise
    if not is_public_ip(peer_ip):
        sock.close()
        raise UnsafeURLError(f"Connection to non-public address {peer_ip} blocked")
    return sock


class _PublicOnlyHTTPConnection(HTTPConnection):
    def _new_conn(self):
        return _check_peer_is_public(super()._new_conn())


class _PublicOnlyHTTPSConnection(HTTPSConnection):
    def _new_conn(self):
        return _check_peer_is_public(super()._new_conn())


class _PublicOnlyHTTPPool(HTTPConnectionPool):
    ConnectionCls = _PublicOnlyHTTPConnection


class _PublicOnlyHTTPSPool(HTTPSConnectionPool):
    ConnectionCls = _PublicOnlyHTTPSConnection


class _PublicOnlyAdapter(HTTPAdapter):
    """HTTPAdapter whose connections may only reach public IP addresses."""

    def init_poolmanager(self, *args, **kwargs):
        super().init_poolmanager(*args, **kwargs)
        self.poolmanager.pool_classes_by_scheme = {
            "http": _PublicOnlyHTTPPool,
            "https": _PublicOnlyHTTPSPool,
        }


_public_session = requests.Session()
# Ignore environment proxies: a proxy would resolve hosts itself, bypassing the peer check.
_public_session.trust_env = False
_public_session.mount("http://", _PublicOnlyAdapter(pool_maxsize=10))
_public_session.mount("https://", _PublicOnlyAdapter(pool_maxsize=20))


def _read_capped(res, max_bytes):
    chunks, total = [], 0
    for chunk in res.iter_content(chunk_size=65536):
        total += len(chunk)
        if total > max_bytes:
            raise ValueError(f"Response exceeds {max_bytes} bytes")
        chunks.append(chunk)
    return b"".join(chunks)


def _curl_fetch_pinned(url, host, port, ip, timeout, headers, max_bytes):
    """curl fallback pinned to an already-validated IP, no redirects, no proxies.

    Returns ``(response, redirect_url)``; redirects are left to the caller so each
    hop is re-validated.
    """
    pinned = f"[{ip}]" if ":" in ip else ip
    cmd = _curl_base_cmd(timeout) + [
        "-w", "\n%{http_code}\n%{redirect_url}",
        "--noproxy", "*",
        "--resolve", f"{host}:{port}:{pinned}",
        "--max-filesize", str(max_bytes),
    ]
    for k, v in headers.items():
        cmd += ["-H", f"{k}: {v}"]
    cmd += ["--", url]
    try:
        res = subprocess.run(cmd, check=False, capture_output=True, text=True, timeout=timeout + 5)
    except (subprocess.SubprocessError, OSError) as e:
        logger.error(f"curl fallback exception for {url}: {e}")
        return _DummyResponse(500, ""), None
    parts = res.stdout.rsplit("\n", 2)
    if res.returncode != 0 or len(parts) != 3 or not parts[1].strip().isdigit():
        logger.error(f"curl fallback failed for {url}: exit={res.returncode} stderr={res.stderr[:200]}")
        return _DummyResponse(500, ""), None
    body, http_code, redirect_url = parts[0], int(parts[1]), parts[2].strip()
    if len(body) > max_bytes:
        raise ValueError(f"Response exceeds {max_bytes} bytes")
    if 300 <= http_code < 400 and redirect_url:
        return _DummyResponse(http_code, ""), redirect_url
    return _DummyResponse(http_code, body), None


def fetch_public_url(url, timeout=10, max_bytes=FEED_MAX_BYTES, headers=None):
    """GET a user-supplied URL with SSRF protections.

    Every hop (initial URL and each redirect) must be http(s) and resolve only to
    public addresses; the actual connected peer is re-checked to defeat DNS
    rebinding, and the body is capped at *max_bytes*. Raises UnsafeURLError for
    disallowed targets.
    """
    req_headers = {"User-Agent": "ShadowBroker-OSINT/1.0 (live-risk-dashboard)"}
    if headers:
        req_headers.update(headers)

    current = url
    for _ in range(FEED_MAX_REDIRECTS + 1):
        host, port, ips = validate_public_url(current)
        try:
            with _public_session.get(current, timeout=(min(3, timeout), timeout),
                                     headers=req_headers, allow_redirects=False,
                                     stream=True) as res:
                if res.is_redirect:
                    location = res.headers.get("Location", "")
                    if not location:
                        return _DummyResponse(res.status_code, "")
                    current = urljoin(current, location)
                    continue
                body = _read_capped(res, max_bytes)
                text = body.decode(res.encoding or "utf-8", errors="replace")
                return _DummyResponse(res.status_code, text)
        except (requests.RequestException, ConnectionError, TimeoutError) as e:
            logger.warning(f"Python requests failed for {current} ({e}), falling back to pinned curl...")
            response, redirect_url = _curl_fetch_pinned(
                current, host, port, ips[0], timeout, req_headers, max_bytes)
            if redirect_url is None:
                return response
            current = urljoin(current, redirect_url)
    raise UnsafeURLError(f"Too many redirects fetching {url}")
