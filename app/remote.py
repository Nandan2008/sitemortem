"""Inputs that come from a URL: a public GitHub repository, or a live website.

Both are untrusted. Rules:
  * GitHub: only github.com/<owner>/<repo>[/tree/<ref>]; the zip is fetched from codeload.github.com (fixed host,
    no redirects followed), size-capped while streaming, then extracted by the same safe extractor as uploads.
  * Website: http(s) on ports 80/443 only. The hostname is resolved HERE and every address must be public
    (no loopback, private, link-local, metadata or reserved ranges). The audit browser is then pinned to those
    exact addresses, so a DNS answer cannot change between the check and the visit (DNS rebinding).
"""
from __future__ import annotations

import ipaddress
import re
import socket
from urllib.parse import urlparse

import httpx

from .safety import UserError

MAX_REPO_ZIP = 25 * 1024 * 1024
_NAME = re.compile(r"^[A-Za-z0-9_.-]{1,100}$")
_REF = re.compile(r"^[A-Za-z0-9_.\-/]{1,100}$")


def parse_github(url: str) -> tuple[str, str, str]:
    try:
        u = urlparse((url or "").strip())
        port = u.port
    except ValueError:
        raise UserError("That does not look like a valid GitHub repository URL.")
    if u.scheme != "https" or (u.hostname or "").lower() not in ("github.com", "www.github.com") or port or u.username:
        raise UserError("Enter a public GitHub repository URL, for example https://github.com/owner/repo")
    parts = [p for p in u.path.split("/") if p]
    if len(parts) < 2:
        raise UserError("That URL has no repository in it. Use https://github.com/owner/repo")
    owner, repo = parts[0], re.sub(r"\.git$", "", parts[1])
    ref = "/".join(parts[3:]) if len(parts) >= 4 and parts[2] == "tree" else ""
    if set(owner) == {"."} or set(repo) == {"."} or not (_NAME.match(owner) and _NAME.match(repo)) or (ref and (not _REF.match(ref) or ".." in ref)):
        raise UserError("That does not look like a valid GitHub repository URL.")
    return owner, repo, ref


def fetch_repo_zip(url: str) -> tuple[bytes, str]:
    owner, repo, ref = parse_github(url)
    src = f"https://codeload.github.com/{owner}/{repo}/zip/{ref or 'HEAD'}"
    try:
        with httpx.stream("GET", src, follow_redirects=False, timeout=30) as r:
            if r.status_code == 404:
                raise UserError("Repository not found. It must be public, and the URL must be correct.")
            if r.status_code != 200:
                raise UserError(f"GitHub returned status {r.status_code}. Try again in a minute.")
            buf = bytearray()
            for chunk in r.iter_bytes(64 * 1024):
                buf += chunk
                if len(buf) > MAX_REPO_ZIP:
                    raise UserError("The repository is larger than 25 MB, which is more than SiteMortem audits.")
    except httpx.HTTPError:
        raise UserError("Could not download the repository from GitHub. Check the URL and try again.")
    return bytes(buf), f"{owner}/{repo}"


def _public(ip: str) -> bool:
    a = ipaddress.ip_address(ip.split("%")[0])
    if getattr(a, "ipv4_mapped", None):
        a = a.ipv4_mapped
    return a.is_global and not a.is_multicast


def resolve_public(host: str) -> list[str]:
    try:
        infos = socket.getaddrinfo(host, None, type=socket.SOCK_STREAM)
    except socket.gaierror:
        raise UserError(f"Could not find the website {host}. Check the address.")
    ips = sorted({i[4][0] for i in infos})
    if not ips or not all(_public(ip) for ip in ips):
        raise UserError("That address points to a private or internal network. SiteMortem only audits public websites.")
    return ips


def validate_site_url(url: str) -> tuple[str, dict[str, str]]:
    """Returns (normalised url, {allowed hostname: pinned public IP})."""
    raw = (url or "").strip()
    if raw and "://" not in raw:
        raw = "https://" + raw
    try:
        u = urlparse(raw)
        port = u.port
    except ValueError:
        raise UserError("Enter a website address starting with http:// or https://")
    host = (u.hostname or "").lower()
    if u.scheme not in ("http", "https") or not host or u.username or u.password:
        raise UserError("Enter a website address starting with http:// or https://")
    if port not in (None, 80, 443):
        raise UserError("Only the standard web ports (80 and 443) are allowed.")
    try:
        ipaddress.ip_address(host)
        raise UserError("Enter a website name, not a raw IP address.")
    except ValueError:
        pass
    pins = {host: resolve_public(host)[0]}
    alt = host[4:] if host.startswith("www.") else "www." + host      # example.com <-> www.example.com redirects
    try:
        pins[alt] = resolve_public(alt)[0]
    except UserError:
        pass
    return raw, pins
