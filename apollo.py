"""Loopback-only Apollo API access and Windows user-bound secrets."""
from __future__ import annotations

import base64
import ctypes
import hashlib
import http.client
from http.cookies import SimpleCookie
import ipaddress
import json
import os
import ssl
import threading
from urllib.parse import urlparse


def protect(value: str) -> str:
    return _crypt(value.encode(), False)


def unprotect(value: str) -> str:
    return _crypt(base64.b64decode(value), True).decode()


def _crypt(data: bytes, decrypt: bool):
    if os.name != "nt":
        raise ValueError("Apollo credentials require Windows DPAPI.")
    class Blob(ctypes.Structure):
        _fields_ = [("size", ctypes.c_ulong), ("data", ctypes.POINTER(ctypes.c_ubyte))]
    buffer = ctypes.create_string_buffer(data)
    source = Blob(len(data), ctypes.cast(buffer, ctypes.POINTER(ctypes.c_ubyte)))
    target = Blob()
    crypt = ctypes.WinDLL("crypt32", use_last_error=True)
    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    fn = crypt.CryptUnprotectData if decrypt else crypt.CryptProtectData
    fn.argtypes = [ctypes.POINTER(Blob), ctypes.c_void_p, ctypes.c_void_p,
                   ctypes.c_void_p, ctypes.c_void_p, ctypes.c_ulong, ctypes.POINTER(Blob)]
    fn.restype = ctypes.c_int
    kernel.LocalFree.argtypes = [ctypes.c_void_p]
    kernel.LocalFree.restype = ctypes.c_void_p
    if not fn(ctypes.byref(source), None, None, None, None, 1, ctypes.byref(target)):
        raise ctypes.WinError(ctypes.get_last_error())
    try:
        result = ctypes.string_at(target.data, target.size)
        return result if decrypt else base64.b64encode(result).decode()
    finally:
        kernel.LocalFree(target.data)


def parse_url(url):
    parsed = urlparse(url)
    if (parsed.scheme != "https" or parsed.hostname not in ("localhost", "127.0.0.1", "::1")
            or parsed.username or parsed.password or parsed.path not in ("", "/")
            or parsed.query or parsed.fragment):
        raise ValueError("Apollo URL must be HTTPS on localhost without a path or credentials.")
    return parsed


class _LocalHTTPSConnection(http.client.HTTPSConnection):
    fingerprint = None

    def connect(self):
        super().connect()
        if not ipaddress.ip_address(self.sock.getpeername()[0]).is_loopback:
            self.close()
            raise ValueError("Apollo resolved to a nonlocal address; refusing to send credentials.")
        fingerprint = hashlib.sha256(self.sock.getpeercert(binary_form=True)).hexdigest()
        # http.client can reconnect internally after a Connection: close. Pin
        # that new socket too, before it can send the subsequent login payload.
        if self.fingerprint is not None and self.fingerprint != fingerprint:
            self.close()
            raise ValueError("Apollo certificate changed; test and trust the local connection again.")
        self.fingerprint = fingerprint


def _connect(url):
    parsed = parse_url(url)
    connection = _LocalHTTPSConnection(parsed.hostname, parsed.port or 47990,
                                      timeout=3, context=ssl._create_unverified_context())
    connection.connect()
    return connection, connection.fingerprint


def certificate_fingerprint(url):
    connection, fingerprint = _connect(url)
    connection.close()
    return fingerprint


class ApolloClient:
    def __init__(self):
        self._lock = threading.Lock()
        self._session_key = None
        self._cookie = None

    def _connection(self, settings):
        connection, fingerprint = _connect(settings["apollo_url"])
        if fingerprint != settings["certificate_sha256"]:
            connection.close()
            raise ValueError("Apollo certificate changed; test and trust the local connection again.")
        return connection

    def _login(self, settings):
        connection = self._connection(settings)
        try:
            # Check on every login, even after a 401: a downgrade may keep the
            # same TLS certificate. Never replace a stock Apollo browser session.
            connection.request("GET", "/api/configLocale")
            response = connection.getresponse()
            body = response.read(1024 * 1024)
            try:
                capability = json.loads(body)
            except (ValueError, TypeError):
                capability = None
            if (response.status != 200 or not isinstance(capability, dict)
                    or capability.get("status") is not True
                    or capability.get("auth_sessions") != "multiple-v1"):
                raise ValueError("Install the Apollo build with independent authentication sessions from ridaken/Apollo before testing or enabling this integration. No login was attempted.")
            credentials = json.loads(unprotect(settings["credentials"]))
            payload = json.dumps({"username": credentials["username"], "password": credentials["password"]}).encode("utf-8")
            connection.request("POST", "/api/login", body=payload,
                               headers={"Content-Type": "application/json"})
            response = connection.getresponse()
            response.read(1024 * 1024)
            if response.status == 401:
                raise ValueError("Apollo rejected the saved username/password. Enter the credentials used for Apollo's web UI, save the connection, and test again.")
            if response.status == 503:
                raise ValueError("Apollo's authentication sessions are full. Existing logins were preserved; AI restoration is on hold.")
            if response.status != 200:
                raise ValueError(f"Apollo login failed (HTTP {response.status}).")
            cookies = SimpleCookie()
            for name, value in response.getheaders():
                if name.lower() == "set-cookie":
                    cookies.load(value)
            if "auth" not in cookies or not cookies["auth"].value:
                raise ValueError("Apollo did not return an authentication cookie after login.")
            self._cookie = "auth=" + cookies["auth"].coded_value
        finally:
            connection.close()

    def connected(self, settings):
        if not settings.get("credentials") or not settings.get("certificate_sha256"):
            raise ValueError("Save Apollo credentials and test the connection before enabling.")
        with self._lock:
            key = (settings["apollo_url"], settings["certificate_sha256"], settings["credentials"])
            if key != self._session_key:
                self._session_key = key
                self._cookie = None
            for attempt in range(2):
                if not self._cookie:
                    self._login(settings)
                connection = self._connection(settings)
                try:
                    connection.request("GET", "/api/clients/list", headers={"Cookie": self._cookie})
                    response = connection.getresponse()
                    body = response.read(1024 * 1024)
                    if response.status == 401:
                        self._cookie = None
                        if attempt == 0:
                            continue  # Expiry or Apollo restart; check capability again.
                    if response.status != 200:
                        raise ValueError(f"Apollo connection failed (HTTP {response.status}).")
                    data = json.loads(body)
                    if not isinstance(data, dict) or data.get("status") is not True or not isinstance(data.get("named_certs"), list):
                        raise ValueError("Apollo returned an invalid client list.")
                    if any(not isinstance(c, dict) or not isinstance(c.get("connected"), bool) for c in data["named_certs"]):
                        raise ValueError("Apollo did not report connection status for every client.")
                    return [c["uuid"] for c in data["named_certs"] if c["connected"]]
                except (json.JSONDecodeError, KeyError, TypeError) as exc:
                    raise ValueError("Apollo returned an invalid client list.") from exc
                finally:
                    connection.close()
