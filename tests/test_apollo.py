import base64
import json
import os

import pytest

import apollo


@pytest.mark.parametrize("url", ["http://localhost:47990", "https://example.org", "https://user:pass@localhost", "https://localhost/path", "https://localhost?secret=1"])
def test_apollo_rejects_nonlocal_or_embedded_credentials(url):
    with pytest.raises(ValueError):
        apollo.parse_url(url)


@pytest.mark.skipif(os.name != "nt", reason="Windows DPAPI")
def test_dpapi_round_trip():
    protected = apollo.protect("private-test-value")
    assert "private-test-value" not in protected
    assert apollo.unprotect(protected) == "private-test-value"


class ApolloServer:
    """Independent sessions with a capability check before JSON login."""
    def __init__(self):
        self.requests = []
        self.connections = []
        self.fingerprint = "pin"
        self.cookie = None
        self.sessions = set()
        self.capability = {"status": True, "locale": "en", "auth_sessions": "multiple-v1"}
        self.capability_status = 200
        self.login_status = 200
        self.login_cookie = True
        self.api_status = 200
        self.data = {"status": True, "named_certs": [{"uuid": "a", "connected": True},
                                                     {"uuid": "b", "connected": False}]}

    def connect(self, url):
        connection = Connection(self)
        self.connections.append(connection)
        return connection, self.fingerprint


class Connection:
    def __init__(self, server):
        self.server = server
        self.closed = False
        self.status = None
        self.headers = []
        self.body = b""

    def request(self, method, path, **kwargs):
        self.server.requests.append((method, path, kwargs))
        self.headers, self.body = [], b""
        if path == "/api/configLocale":
            assert method == "GET"
            self.status = self.server.capability_status
            self.body = json.dumps(self.server.capability).encode()
        elif path == "/api/login":
            assert method == "POST" and kwargs["headers"]["Content-Type"] == "application/json"
            assert json.loads(kwargs["body"]) == {"username": "user", "password": "pw"}
            self.status = self.server.login_status
            if self.status == 200 and self.server.login_cookie:
                self.server.cookie = "auth=session-" + str(len(self.server.requests))
                self.server.sessions.add(self.server.cookie)
                self.headers = [("Set-Cookie", self.server.cookie + "; Secure; SameSite=Strict; Path=/")]
        else:
            assert method == "GET" and path == "/api/clients/list"
            self.status = (self.server.api_status if kwargs["headers"].get("Cookie") in self.server.sessions else 401)
            self.body = json.dumps(self.server.data).encode()

    def getresponse(self):
        return self

    def read(self, limit):
        return self.body

    def getheaders(self):
        return self.headers

    def close(self):
        self.closed = True


@pytest.fixture
def server(monkeypatch):
    server = ApolloServer()
    monkeypatch.setattr(apollo, "_connect", server.connect)
    monkeypatch.setattr(apollo, "unprotect", lambda encrypted: json.dumps({"username": "user", "password": "pw"}))
    return server


SETTINGS = {"apollo_url": "https://localhost", "credentials": "encrypted", "certificate_sha256": "pin"}


def test_pin_verified_before_sending_credentials(server):
    server.fingerprint = "changed"
    with pytest.raises(ValueError, match="certificate changed"):
        apollo.ApolloClient().connected(SETTINGS)
    assert not server.requests and all(c.closed for c in server.connections)


@pytest.mark.parametrize("body,status,expected", [
    ({"status": True, "named_certs": [{"uuid": "a", "connected": True}, {"uuid": "b", "connected": False}]}, 200, ["a"]),
    ({"status": True, "named_certs": []}, 403, None),
    ({"status": True, "named_certs": [{"uuid": "a"}]}, 200, None),
    ({"status": False, "named_certs": []}, 200, None),
])
def test_apollo_connection_truth(server, body, status, expected):
    server.data, server.api_status = body, status
    if expected is None:
        with pytest.raises(ValueError):
            apollo.ApolloClient().connected(SETTINGS)
    else:
        assert apollo.ApolloClient().connected(SETTINGS) == expected
    assert all(c.closed for c in server.connections)


def test_json_login_and_cookie_are_used_without_basic_auth(server):
    assert apollo.ApolloClient().connected(SETTINGS) == ["a"]
    assert [path for _, path, _ in server.requests] == ["/api/configLocale", "/api/login", "/api/clients/list"]
    assert all("Authorization" not in request[2].get("headers", {}) for request in server.requests)
    assert server.requests[2][2]["headers"] == {"Cookie": server.cookie}


def test_cookie_reused_across_polls(server):
    client = apollo.ApolloClient()
    client.connected(SETTINGS)
    client.connected(SETTINGS)
    assert [method for method, _, _ in server.requests] == ["GET", "POST", "GET", "GET"]


def test_expired_session_is_renewed_once(server):
    client = apollo.ApolloClient()
    client.connected(SETTINGS)
    server.sessions.clear()
    assert client.connected(SETTINGS) == ["a"]
    assert [method for method, _, _ in server.requests] == ["GET", "POST", "GET", "GET", "GET", "POST", "GET"]
    assert all(c.closed for c in server.connections)


def test_changed_credentials_clear_cached_cookie(server):
    client = apollo.ApolloClient()
    client.connected(SETTINGS)
    client.connected({**SETTINGS, "credentials": "new-encrypted"})
    assert [method for method, _, _ in server.requests] == ["GET", "POST", "GET", "GET", "POST", "GET"]


def test_bad_login_has_actionable_error_without_password(server):
    server.login_status = 401
    with pytest.raises(ValueError, match="Apollo rejected the saved username/password") as error:
        apollo.ApolloClient().connected(SETTINGS)
    assert "pw" not in str(error.value)
    assert len(server.requests) == 2 and server.connections[0].closed


def test_login_without_cookie_is_rejected(server):
    server.login_cookie = False
    with pytest.raises(ValueError, match="authentication cookie"):
        apollo.ApolloClient().connected(SETTINGS)
    assert len(server.requests) == 2


def test_repeated_api_401_is_bounded(server):
    server.api_status = 401
    with pytest.raises(ValueError, match="HTTP 401"):
        apollo.ApolloClient().connected(SETTINGS)
    assert len(server.requests) == 6
    assert all(c.closed for c in server.connections)


@pytest.mark.parametrize("capability,status", [
    ({"status": True, "locale": "en"}, 200),
    ({"status": True, "auth_sessions": "single"}, 200),
    ({"status": False, "auth_sessions": "multiple-v1"}, 200),
    (["multiple-v1"], 200),
    ({"auth_sessions": "multiple-v1"}, 404),
])
def test_incompatible_apollo_never_receives_login(server, capability, status, monkeypatch):
    server.capability, server.capability_status = capability, status
    monkeypatch.setattr(apollo, "unprotect", lambda _: pytest.fail("Credentials must not be decrypted"))
    with pytest.raises(ValueError, match="independent authentication sessions"):
        apollo.ApolloClient().connected(SETTINGS)
    assert [path for _, path, _ in server.requests] == ["/api/configLocale"]
    assert all(c.closed for c in server.connections)


def test_downgrade_after_401_does_not_attempt_login(server):
    client = apollo.ApolloClient()
    client.connected(SETTINGS)
    server.sessions.clear()
    server.capability = {"status": True, "locale": "en"}
    with pytest.raises(ValueError, match="No login was attempted"):
        client.connected(SETTINGS)
    assert sum(path == "/api/login" for _, path, _ in server.requests) == 1


def test_browser_and_another_monitor_login_do_not_invalidate_cookie(server):
    first, second = apollo.ApolloClient(), apollo.ApolloClient()
    first.connected(SETTINGS)
    second.connected(SETTINGS)
    server.sessions.add("auth=browser-login")
    assert first.connected(SETTINGS) == second.connected(SETTINGS) == ["a"]
    assert sum(path == "/api/login" for _, path, _ in server.requests) == 2


def test_capacity_has_actionable_error(server):
    server.login_status = 503
    with pytest.raises(ValueError, match="sessions are full"):
        apollo.ApolloClient().connected(SETTINGS)


@pytest.mark.parametrize("peer,certificate", [("192.168.1.1", b"original"), ("127.0.0.1", b"changed")])
def test_implicit_tls_reconnect_is_checked(monkeypatch, peer, certificate):
    class Socket:
        def getpeername(self):
            return (peer, 47990)

        def getpeercert(self, **kwargs):
            return certificate

        def close(self):
            pass

    connection = apollo._LocalHTTPSConnection("localhost")
    connection.fingerprint = apollo.hashlib.sha256(b"original").hexdigest()
    monkeypatch.setattr(apollo.http.client.HTTPSConnection, "connect", lambda self: setattr(self, "sock", Socket()))
    with pytest.raises(ValueError):
        connection.connect()
    assert connection.sock is None
