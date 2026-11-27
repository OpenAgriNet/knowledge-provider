"""Fetch one bounded public HTTPS source and retain its exact response bytes."""

import asyncio
import ipaddress
import socket
from contextlib import aclosing
from dataclasses import dataclass
from html.parser import HTMLParser
from pathlib import PurePosixPath
from urllib.parse import unquote

import httpx

from pipeline.documents.document_formats import binary_media_types
from pipeline.metadata.localization import normalize_language_tag

MAX_HTML_BYTES = 2 * 1024 * 1024
MAX_EXTRACTED_CHARS = 200_000
MAX_FILE_BYTES = 20 * 1024 * 1024
_HTML_TYPES = {"text/html", "application/xhtml+xml"}
_TEXT_TYPES = {"text/plain", "text/markdown"}

_HIDDEN = {"script", "style", "template", "noscript", "svg", "nav", "footer"}
_BREAKS = {"article", "main", "div", "section", "p", "br", "li", "tr", "h1", "h2", "h3", "h4"}


class SourcePageError(ValueError):
    """A source URL was disallowed or could not provide usable HTML."""


@dataclass(frozen=True)
class SourcePage:
    title: str
    text: str
    language: str | None
    attribution_url: str
    content: bytes = b""
    media_type: str = "text/plain"


@dataclass(frozen=True)
class SourceFile:
    filename: str
    media_type: str
    content: bytes
    attribution_url: str


class _PageParser(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.language = None
        self.title: list[str] = []
        self.body: list[str] = []
        self._hidden: list[str] = []
        self._in_head = False
        self._in_title = False

    def handle_starttag(self, tag, attrs):
        if tag == "html":
            self.language = normalize_language_tag(dict(attrs).get("lang"))
        if tag == "head":
            self._in_head = True
        if tag == "title":
            self._in_title = True
        if tag in _HIDDEN:
            self._hidden.append(tag)
        if tag in _BREAKS and not self._hidden and not self._in_head:
            self.body.append("\n")

    def handle_endtag(self, tag):
        if tag == "head":
            self._in_head = False
        if tag == "title":
            self._in_title = False
        if self._hidden and self._hidden[-1] == tag:
            self._hidden.pop()
        if tag in _BREAKS and not self._hidden and not self._in_head:
            self.body.append("\n")

    def handle_data(self, data):
        if self._hidden:
            return
        if self._in_title:
            self.title.append(data)
        elif not self._in_head:
            self.body.append(data)


def _is_public_unicast(address: ipaddress.IPv4Address | ipaddress.IPv6Address) -> bool:
    if isinstance(address, ipaddress.IPv6Address) and address.ipv4_mapped:
        return _is_public_unicast(address.ipv4_mapped)
    return address.is_global and not address.is_multicast and not address.is_reserved


def _resolve_public_address(host: str) -> str:
    try:
        addresses = {
            ipaddress.ip_address(result[4][0])
            for result in socket.getaddrinfo(host, 443, type=socket.SOCK_STREAM)
        }
    except (OSError, ValueError) as exc:
        raise SourcePageError("The source host could not be resolved") from exc
    if not addresses or not all(map(_is_public_unicast, addresses)):
        raise SourcePageError("The source host must resolve to public addresses")
    return str(min(addresses, key=lambda address: (address.version, int(address))))


async def fetch_source(url: str, allowed_hosts: set[str] | None = None, authorization: str | None = None) -> SourcePage | SourceFile:
    if len(url) > 4096 or any(character.isspace() or ord(character) < 32 for character in url):
        raise SourcePageError("Invalid source URL")
    try:
        target = httpx.URL(url)
    except (httpx.InvalidURL, ValueError) as exc:
        raise SourcePageError("Invalid source URL") from exc
    if (
        target.scheme != "https" or target.port not in (None, 443)
        or target.userinfo or not target.host or target.fragment
    ):
        raise SourcePageError("Use a public HTTPS source URL without embedded credentials or a fragment")
    host = target.raw_host.decode("ascii").lower()
    if authorization and allowed_hosts is None:
        raise SourcePageError("Authenticated sources require a saved host policy")
    if allowed_hosts is not None and host not in allowed_hosts:
        raise SourcePageError("This source host is not enabled for the provider")
    headers = {"Host": host, "Accept": "*/*", "Accept-Encoding": "identity"}
    if authorization:
        headers["Authorization"] = authorization
    try:
        media_type, body, encoding = await asyncio.wait_for(_read_source(target, host, headers), timeout=20)
    except asyncio.TimeoutError as exc:
        raise SourcePageError("The source did not respond in time") from exc
    except (httpx.HTTPError, ValueError) as exc:
        if isinstance(exc, SourcePageError):
            raise
        raise SourcePageError("The source did not return a usable HTTPS response") from exc

    attribution_url = str(target.copy_with(query=None))
    file_types = binary_media_types()
    if media_type in file_types or media_type == "application/json":
        suffix = file_types.get(media_type, ".json")
        raw_name = unquote(PurePosixPath(target.path).name)
        stem = "".join(char for char in PurePosixPath(raw_name).stem if char.isalnum() or char in "-_")[:180]
        return SourceFile((stem or "source") + suffix, media_type, body, attribution_url)

    title = unquote(PurePosixPath(target.path).name)[:200] or host
    return extract_text_source(body, media_type, title, attribution_url, encoding)


def extract_text_source(
    body: bytes, media_type: str, title: str, attribution_url: str = "", encoding: str = "utf-8",
) -> SourcePage:
    if len(body) > MAX_HTML_BYTES:
        raise SourcePageError("Text sources must be at most 2 MiB")
    try:
        content = body.decode(encoding, errors="strict")
    except (LookupError, UnicodeError) as exc:
        raise SourcePageError("The source does not use a supported text encoding") from exc
    language = None
    if media_type in _HTML_TYPES:
        parser = _PageParser()
        parser.feed(content)
        parser.close()
        lines = [" ".join(line.split()) for line in "".join(parser.body).splitlines()]
        text = "\n\n".join(line for line in lines if line)
        title = " ".join("".join(parser.title).split())[:200] or title
        language = parser.language
    else:
        text = content.strip()
    if not text or len(text) > MAX_EXTRACTED_CHARS or "\x00" in text:
        raise SourcePageError("The source must contain between 1 and 200,000 valid text characters")
    return SourcePage(title, text, language, attribution_url, body, media_type)


async def fetch_html_source(url: str, allowed_hosts: set[str], authorization: str | None = None) -> SourcePage:
    source = await fetch_source(url, allowed_hosts, authorization)
    if not isinstance(source, SourcePage):
        raise SourcePageError("The source must return text")
    return source


async def _read_source(target: httpx.URL, host: str, headers: dict[str, str]) -> tuple[str, bytes, str]:
    address = await asyncio.to_thread(_resolve_public_address, host)
    request = httpx.Request(
        "GET", target.copy_with(host=address), headers=headers,
        extensions={"sni_hostname": host, "timeout": httpx.Timeout(8).as_dict()},
    )
    # Use the transport directly so the request URL never goes through the
    # HTTP client's INFO logger. Attribution omits credentials and query values.
    async with httpx.AsyncHTTPTransport(trust_env=False) as transport:
        async with aclosing(await transport.handle_async_request(request)) as response:
            response.request = request
            response.raise_for_status()
            media_type = response.headers.get("content-type", "").split(";", 1)[0].strip().lower()
            file_types = binary_media_types()
            if media_type not in (_HTML_TYPES | _TEXT_TYPES | file_types.keys() | {"application/json"}):
                raise SourcePageError("The source returned an unsupported media type")
            if response.headers.get("content-encoding", "identity").lower() != "identity":
                raise SourcePageError("The source must return uncompressed content")
            limit = MAX_FILE_BYTES if media_type in file_types else MAX_HTML_BYTES
            body = bytearray()
            async for block in response.aiter_raw():
                if len(body) + len(block) > limit:
                    raise SourcePageError(f"The source exceeds the {limit // (1024 * 1024)} MiB download limit")
                body.extend(block)
            return media_type, bytes(body), response.encoding or "utf-8"
