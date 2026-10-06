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


class Connection:
    def __init__(self, body, status=200):
        self.body, self.status = body, status
        self.requests = []
        self.closed = False

    def request(self, *args, **kwargs):
        self.requests.append((args, kwargs))

    def getresponse(self):
        return self

    def read(self, limit):
        return json.dumps(self.body).encode()

    def close(self):
        self.closed = True


def test_pin_verified_before_sending_credentials(monkeypatch):
    connection = Connection({})
    monkeypatch.setattr(apollo, "_connect", lambda url: (connection, "changed"))
    with pytest.raises(ValueError, match="certificate changed"):
        apollo.ApolloClient().connected({"apollo_url": "https://localhost", "credentials": "secret", "certificate_sha256": "original"})
    assert not connection.requests and connection.closed


@pytest.mark.parametrize("body,status,expected", [
    ({"status": True, "named_certs": [{"uuid": "a", "connected": True}, {"uuid": "b", "connected": False}]}, 200, ["a"]),
    ({"status": True, "named_certs": []}, 401, None),
    ({"status": True, "named_certs": [{"uuid": "a"}]}, 200, None),
    ({"status": False, "named_certs": []}, 200, None),
])
def test_apollo_connection_truth(monkeypatch, body, status, expected):
    connection = Connection(body, status)
    monkeypatch.setattr(apollo, "_connect", lambda url: (connection, "pin"))
    monkeypatch.setattr(apollo, "unprotect", lambda encrypted: json.dumps({"username": "user", "password": "pw"}))
    settings = {"apollo_url": "https://localhost", "credentials": "encrypted", "certificate_sha256": "pin"}
    if expected is None:
        with pytest.raises(ValueError):
            apollo.ApolloClient().connected(settings)
    else:
        assert apollo.ApolloClient().connected(settings) == expected
    assert connection.closed
