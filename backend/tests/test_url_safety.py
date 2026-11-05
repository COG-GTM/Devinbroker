"""SSRF guards: URL validation, news feed config, and user-supplied URL fetching."""
import socket
from unittest.mock import MagicMock, patch

import pytest

from services import network_utils, news_feed_config
from services.network_utils import fetch_public_url, fetch_with_curl
from services.url_safety import UnsafeURLError, is_public_ip, validate_public_url


def _addrinfo(*ips):
    return [(socket.AF_INET6 if ":" in ip else socket.AF_INET, socket.SOCK_STREAM, 6, "", (ip, 0))
            for ip in ips]


@pytest.fixture
def public_dns():
    with patch("services.url_safety.socket.getaddrinfo", return_value=_addrinfo("93.184.216.34")) as m:
        yield m


class TestIsPublicIp:
    @pytest.mark.parametrize("ip", [
        "127.0.0.1", "10.0.0.5", "172.16.0.1", "192.168.1.1", "169.254.169.254",
        "100.64.0.1", "0.0.0.0", "::1", "fe80::1", "fc00::1", "::ffff:127.0.0.1",
        "::ffff:169.254.169.254", "224.0.0.1", "255.255.255.255",
    ])
    def test_non_public(self, ip):
        assert not is_public_ip(ip)

    @pytest.mark.parametrize("ip", ["93.184.216.34", "8.8.8.8", "2606:4700:4700::1111"])
    def test_public(self, ip):
        assert is_public_ip(ip)


class TestValidatePublicUrl:
    @pytest.mark.parametrize("url", [
        "file:///etc/hostname",
        "gopher://127.0.0.1:6379/_INFO",
        "dict://127.0.0.1:11211/stats",
        "ftp://example.com/feed.xml",
        "-K/etc/passwd",
        "//example.com/feed",
        "http://",
        "http://user:pass@example.com/feed",
        "http://example.com/feed\nX: y",
        "http://127.0.0.1:8000/api/health",
        "http://169.254.169.254/latest/meta-data/",
        "http://[::1]/",
        "http://[::ffff:7f00:1]/",
        "http://10.1.2.3/rss",
    ])
    def test_rejects(self, url, public_dns):
        with pytest.raises(UnsafeURLError):
            validate_public_url(url)

    def test_rejects_hostname_resolving_to_private(self):
        with patch("services.url_safety.socket.getaddrinfo",
                   return_value=_addrinfo("93.184.216.34", "127.0.0.1")), \
             pytest.raises(UnsafeURLError, match="non-public"):
            validate_public_url("http://rebind.example/feed")

    def test_rejects_unresolvable(self):
        with patch("services.url_safety.socket.getaddrinfo", side_effect=socket.gaierror("nope")), \
             pytest.raises(UnsafeURLError, match="resolve"):
            validate_public_url("https://does-not-exist.invalid/rss")

    def test_rejects_obfuscated_loopback(self):
        # Real resolver: inet_aton-style shorthand still resolves to loopback
        with pytest.raises(UnsafeURLError):
            validate_public_url("http://0x7f.1/")

    def test_accepts_public(self, public_dns):
        host, port, ips = validate_public_url("https://feeds.example.com/rss.xml")
        assert (host, port, ips) == ("feeds.example.com", 443, ["93.184.216.34"])


class TestSaveFeeds:
    @pytest.fixture(autouse=True)
    def _tmp_config(self, tmp_path, monkeypatch):
        monkeypatch.setattr(news_feed_config, "CONFIG_PATH", tmp_path / "news_feeds.json")

    @pytest.mark.parametrize("url", [
        "file:///etc/hostname",
        "http://127.0.0.1:8000/api/health",
        "http://169.254.169.254/latest/meta-data/",
        "gopher://127.0.0.1:6379/_FLUSHALL",
    ])
    def test_rejects_unsafe_urls(self, url):
        assert news_feed_config.save_feeds([{"name": "x", "url": url, "weight": 3}]) is False
        assert not news_feed_config.CONFIG_PATH.exists()

    def test_rejects_non_string_fields(self):
        assert news_feed_config.validate_feeds([{"name": 1, "url": "https://a.example", "weight": 3}])

    def test_accepts_public_feed(self, public_dns):
        feeds = [{"name": " Example ", "url": " https://feeds.example.com/rss ", "weight": 4.0}]
        assert news_feed_config.save_feeds(feeds) is True
        assert news_feed_config.get_feeds() == [
            {"name": "Example", "url": "https://feeds.example.com/rss", "weight": 4}]

    def test_reset_does_not_need_dns(self):
        with patch("services.url_safety.socket.getaddrinfo", side_effect=socket.gaierror("offline")):
            assert news_feed_config.reset_feeds() is True
        assert news_feed_config.get_feeds() == news_feed_config.DEFAULT_FEEDS


class TestFetchWithCurlHardening:
    def setup_method(self):
        network_utils._circuit_breaker.clear()
        network_utils._domain_fail_cache.clear()

    @pytest.mark.parametrize("url", ["file:///etc/hostname", "gopher://127.0.0.1:6379/_INFO", "-K/etc/passwd"])
    def test_non_http_schemes_never_reach_requests_or_curl(self, url):
        with patch("services.network_utils._session") as sess, patch("subprocess.run") as run:
            with pytest.raises(UnsafeURLError):
                fetch_with_curl(url)
            sess.get.assert_not_called()
            run.assert_not_called()

    def test_file_url_does_not_return_file_contents(self):
        with pytest.raises(ValueError):
            fetch_with_curl("file:///etc/hostname")

    def test_curl_fallback_restricts_protocols_and_ends_options(self):
        network_utils._domain_fail_cache["curl.example.com"] = network_utils.time.time()
        result = MagicMock(returncode=0, stdout="ok\n200", stderr="")
        with patch("subprocess.run", return_value=result) as run:
            fetch_with_curl("https://curl.example.com/x", timeout=7)
        cmd = run.call_args.args[0]
        assert cmd[cmd.index("--proto") + 1] == "=http,https"
        assert cmd[cmd.index("--proto-redir") + 1] == "=http,https"
        assert cmd[cmd.index("--max-time") + 1] == "7"
        assert cmd[-2:] == ["--", "https://curl.example.com/x"]


def _response(status=200, body=b"<rss/>", headers=None):
    res = MagicMock()
    res.status_code = status
    res.headers = headers or {}
    res.is_redirect = status in (301, 302, 303, 307, 308) and "Location" in res.headers
    res.encoding = "utf-8"
    res.iter_content.return_value = [body]
    res.__enter__ = MagicMock(return_value=res)
    res.__exit__ = MagicMock(return_value=False)
    return res


class TestFetchPublicUrl:
    def test_blocks_internal_targets_without_network(self):
        with patch.object(network_utils._public_session, "get") as get, patch("subprocess.run") as run:
            for url in ("http://127.0.0.1:8000/api/health", "http://169.254.169.254/latest/meta-data/",
                        "file:///etc/hostname", "gopher://127.0.0.1:6379/_INFO"):
                with pytest.raises(UnsafeURLError):
                    fetch_public_url(url)
            get.assert_not_called()
            run.assert_not_called()

    def test_fetches_public_feed(self, public_dns):
        with patch.object(network_utils._public_session, "get", return_value=_response()) as get:
            res = fetch_public_url("https://feeds.example.com/rss")
        assert res.status_code == 200 and res.text == "<rss/>"
        assert get.call_args.kwargs["allow_redirects"] is False

    def test_redirect_to_internal_host_is_blocked(self, public_dns):
        redirect = _response(302, headers={"Location": "http://169.254.169.254/latest/meta-data/"})
        with patch.object(network_utils._public_session, "get", return_value=redirect) as get, \
             pytest.raises(UnsafeURLError):
            fetch_public_url("https://feeds.example.com/rss")
        assert get.call_count == 1

    def test_body_size_is_capped(self, public_dns):
        big = _response(body=b"x" * 101)
        with patch.object(network_utils._public_session, "get", return_value=big), \
             pytest.raises(ValueError, match="exceeds"):
            fetch_public_url("https://feeds.example.com/rss", max_bytes=100)

    def test_curl_fallback_is_pinned_to_validated_ip(self, public_dns):
        result = MagicMock(returncode=0, stdout="<rss/>\n200", stderr="")
        with patch.object(network_utils._public_session, "get",
                          side_effect=network_utils.requests.ConnectionError("blocked")), \
             patch("subprocess.run", return_value=result) as run:
            res = fetch_public_url("https://feeds.example.com/rss")
        assert res.text == "<rss/>"
        cmd = run.call_args.args[0]
        assert cmd[cmd.index("--resolve") + 1] == "feeds.example.com:443:93.184.216.34"
        assert cmd[cmd.index("--proto") + 1] == "=http,https"
        assert cmd[-2:] == ["--", "https://feeds.example.com/rss"]
        assert "-L" not in cmd

    def test_peer_check_blocks_dns_rebinding(self):
        sock = MagicMock()
        sock.getpeername.return_value = ("127.0.0.1", 80)
        with pytest.raises(UnsafeURLError):
            network_utils._check_peer_is_public(sock)
        sock.close.assert_called_once()
