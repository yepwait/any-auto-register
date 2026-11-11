from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Optional
from urllib.parse import quote, unquote, urlsplit, urlunsplit


@dataclass(frozen=True)
class _LegacyProxyParts:
    scheme: str
    host: str
    port: str
    username: str = ""
    password: str = ""


def _parse_legacy_proxy(value: str) -> _LegacyProxyParts | None:
    fields = value.split(":")
    if len(fields) == 2 and fields[1].isdigit():
        return _LegacyProxyParts("http", fields[0], fields[1])
    if len(fields) == 4 and fields[1].isdigit():
        return _LegacyProxyParts("http", fields[0], fields[1], fields[2], fields[3])
    if len(fields) >= 5 and fields[2].isdigit() and fields[0].lower() in {
        "http",
        "https",
        "socks4",
        "socks5",
        "socks5h",
    }:
        return _LegacyProxyParts(
            fields[0].lower(),
            fields[1],
            fields[2],
            fields[3],
            ":".join(fields[4:]),
        )
    return None


def _legacy_to_url(parts: _LegacyProxyParts) -> str:
    scheme = "socks5h" if parts.scheme == "socks5" else parts.scheme
    host = parts.host
    if ":" in host and not host.startswith("["):
        host = f"[{host}]"
    auth = ""
    if parts.username or parts.password:
        username = quote(parts.username, safe="")
        password = quote(parts.password, safe="")
        auth = f"{username}:{password}@"
    return f"{scheme}://{auth}{host}:{parts.port}"


def _is_auth_socks_proxy(scheme: str, username: str, password: str) -> bool:
    normalized = (scheme or "").lower()
    return normalized in {"socks5", "socks5h"} and bool(username or password)


def is_authenticated_socks5_proxy(proxy_url: Optional[str]) -> bool:
    if not proxy_url:
        return False

    value = str(proxy_url).strip()
    if not value:
        return False

    if value.startswith("{"):
        try:
            data = json.loads(value)
            if isinstance(data, dict):
                server = str(data.get("server") or "").strip()
                if not server:
                    return False
                scheme = (urlsplit(server).scheme or "").lower()
                username = str(data.get("username") or "").strip()
                password = str(data.get("password") or "").strip()
                return _is_auth_socks_proxy(scheme, username, password)
        except Exception:
            return False

    parts = urlsplit(value)
    return _is_auth_socks_proxy(
        parts.scheme or "",
        unquote(parts.username or ""),
        unquote(parts.password or ""),
    )


def normalize_proxy_url(proxy_url: Optional[str]) -> Optional[str]:
    """补齐 HTTP 协议，并将 socks5:// 规范化为 socks5h://。"""
    if proxy_url is None:
        return None

    value = str(proxy_url).strip()
    if not value:
        return None
    if value.startswith("{"):
        return value

    # Parse host:port:user:pass BEFORE adding a scheme; otherwise the legacy
    # parser mistakes //host for the host, causing repeated failed retries.
    if "://" not in value:
        legacy = _parse_legacy_proxy(value)
        if legacy:
            return _legacy_to_url(legacy)
        # Proxy pools also return user:pass@host:port.
        value = f"http://{value}"

    parts = urlsplit(value)
    if (parts.scheme or "").lower() == "socks5":
        parts = parts._replace(scheme="socks5h")
        return urlunsplit(parts)
    return value


def redact_proxy_url(proxy_url: Optional[str]) -> str:
    """Return a log-safe proxy URL without authentication credentials."""
    value = str(proxy_url or "").strip()
    if not value:
        return ""

    legacy = _parse_legacy_proxy(value)
    if legacy and "://" not in value:
        if legacy.username or legacy.password:
            if value.split(":", 1)[0].lower() in {
                "http",
                "https",
                "socks4",
                "socks5",
                "socks5h",
            }:
                return f"{legacy.scheme}:{legacy.host}:{legacy.port}:***:***"
            return f"{legacy.host}:{legacy.port}:***:***"
        return f"{legacy.host}:{legacy.port}"

    parts = urlsplit(value)
    if not parts.scheme:
        return "(configured proxy)"

    host = parts.hostname or ""
    if ":" in host and not host.startswith("["):
        host = f"[{host}]"
    try:
        port = f":{parts.port}" if parts.port is not None else ""
    except ValueError:
        return f"{parts.scheme}://(configured proxy)"
    auth = (
        "***:***@" if parts.username is not None or parts.password is not None else ""
    )
    return urlunsplit(
        (parts.scheme, f"{auth}{host}{port}", parts.path, parts.query, parts.fragment)
    )


def build_requests_proxy_config(proxy_url: Optional[str]) -> Optional[dict[str, str]]:
    normalized = normalize_proxy_url(proxy_url)
    if not normalized:
        return None
    return {"http": normalized, "https": normalized}


def build_playwright_proxy_config(proxy_url: Optional[str]) -> Optional[dict[str, str]]:
    value = normalize_proxy_url(proxy_url)
    if not value:
        return None
    parts = urlsplit(value)
    if not parts.scheme or not parts.hostname or parts.port is None:
        server = value
        if server.startswith("socks5h://"):
            server = "socks5://" + server[len("socks5h://") :]
        return {"server": server}

    scheme = (parts.scheme or "").lower()
    if scheme == "socks5h":
        scheme = "socks5"

    config = {"server": f"{scheme}://{parts.hostname}:{parts.port}"}
    if parts.username:
        config["username"] = unquote(parts.username)
    if parts.password:
        config["password"] = unquote(parts.password)
    return config
