"""Previews: what a link is, fetched only when the person points at it, and what a shared file is, for hover cards.

A link card is fetched by the server, never by the page, because the page's CSP (`img-src 'self' data:`) keeps it
from loading anything off the web: a chat could otherwise send data out through an image address. So the rule is the
README's: nothing remote is fetched from transcript text on its own. The page asks for a card only when the person
hovers or opens a link, and its picture and icon come back through the hub.

Every fetch is held to the same guards, at every hop of a redirect:
- http and https only, on the usual ports, with no user name or password in the address;
- the name is resolved here, every address it resolves to must be public (not private, loopback, link-local,
  multicast, reserved, or the tailnet's 100.64/10), and the connection goes to the address that was checked, so a
  second lookup can't swap in another one;
- two seconds for the whole fetch, redirects included, and at most 512 KB read;
- no cookies, no referrer, and a User-Agent that says nothing about who runs the hub.
Cards, pictures and refusals are kept on disk under the hub's state folder.
"""
import concurrent.futures, hashlib, html.parser, http.client, ipaddress, json, os, re, socket, ssl, struct, subprocess, tempfile, time
from pathlib import Path
from urllib.parse import urljoin, urlsplit

import hub_data as hub
from hub_base import STATE, Refused

CACHE = STATE / "previews"
TIMEOUT = 2.0
MAX_BYTES = 512 * 1024
MAX_HOPS = 4
PORTS = {"http": {80, 8080}, "https": {443, 8443}}
AGENT = "Mozilla/5.0 (compatible; LinkPreview)"
KEEP, KEEP_REFUSED = 7 * 86400, 600  # a card for a week; a refusal for ten minutes, so pointing again doesn't hammer a site
PAGE_TYPES = ("text/html", "application/xhtml+xml")
PICTURES = {"image/png": b"\x89PNG", "image/jpeg": b"\xff\xd8", "image/gif": b"GIF8", "image/webp": b"RIFF", "image/x-icon": b"\x00\x00\x01\x00", "image/vnd.microsoft.icon": b"\x00\x00\x01\x00"}
TAILNET = ipaddress.ip_network("100.64.0.0/10")
NAT64 = ipaddress.ip_network("64:ff9b::/96")
_lookups = concurrent.futures.ThreadPoolExecutor(4, thread_name_prefix="preview-dns")


class NotFetched(Refused):
    """The address is one the hub won't fetch, or the fetch failed. The text says which, plainly."""


# ── the guards ──
def public(address: str) -> bool:
    """Whether an address is somewhere on the public internet. Anything else (this Mac, the home network, the tailnet,
    link-local and cloud metadata addresses, multicast, reserved space) is refused, including IPv4 hidden in IPv6."""
    try:
        ip = ipaddress.ip_address(address.split("%", 1)[0])
    except ValueError:
        return False
    if ip.version == 6:
        if ip.ipv4_mapped:
            ip = ip.ipv4_mapped
        elif ip.sixtofour:
            ip = ip.sixtofour
        elif ip.teredo:
            return False
        elif ip in NAT64:
            ip = ipaddress.ip_address(int(ip) & 0xFFFFFFFF)
    if ip.version == 4 and ip in TAILNET:
        return False
    return ip.is_global and not (ip.is_private or ip.is_loopback or ip.is_link_local or ip.is_multicast or ip.is_reserved or ip.is_unspecified)


def checked_address(url: str):
    """(scheme, host, port, path, the public address to connect to), or NotFetched with the reason."""
    parts = urlsplit(url)
    scheme = parts.scheme.lower()
    if scheme not in PORTS:
        raise NotFetched("Only web addresses (http and https) are previewed.")
    if parts.username is not None or parts.password is not None:
        raise NotFetched("An address with a name or password in it isn't previewed.")
    host = (parts.hostname or "").rstrip(".").lower()
    if not host:
        raise NotFetched("That address has no host.")
    try:
        port = parts.port or (443 if scheme == "https" else 80)
    except ValueError:
        raise NotFetched("That address has a port that isn't a number.")
    future = _lookups.submit(socket.getaddrinfo, host, port, 0, socket.SOCK_STREAM)
    try:
        found = future.result(timeout=TIMEOUT)
    except concurrent.futures.TimeoutError:
        raise NotFetched(f"{host} didn't resolve in time.")
    except (OSError, UnicodeError):
        raise NotFetched(f"{host} doesn't resolve.")
    addresses = list(dict.fromkeys(info[4][0] for info in found))
    if not addresses or not all(public(a) for a in addresses):
        raise NotFetched(f"{host} is on this Mac or a private network, so the hub doesn't fetch it.")
    if port not in PORTS[scheme]:
        raise NotFetched(f"Only the usual web ports are previewed, not {port}.")
    path = (parts.path or "/") + (f"?{parts.query}" if parts.query else "")
    return scheme, host, port, path, addresses[0]


class _Pinned(http.client.HTTPConnection):
    """A connection to the address that was checked, whatever the name resolves to by now."""
    def __init__(self, host, port, address, timeout, secure):
        super().__init__(host, port, timeout=timeout)
        self._address, self._secure = address, secure

    def connect(self):
        sock = socket.create_connection((self._address, self.port), self.timeout)
        if self._secure:
            sock = ssl.create_default_context().wrap_socket(sock, server_hostname=self.host)
        self.sock = self.held = sock  # held: http.client lets go of .sock once a response says it will close


def fetch(url: str, want, deadline=None, check=None):
    """GET a public address, following redirects through the same guards. `want` is the content types accepted.
    Returns (final url, content type, body of at most MAX_BYTES bytes, whether the body was cut short)."""
    deadline = deadline or time.monotonic() + TIMEOUT
    check = check or checked_address
    for _ in range(MAX_HOPS + 1):
        scheme, host, port, path, address = check(url)
        left = deadline - time.monotonic()
        if left <= 0:
            raise NotFetched("That page took too long to answer.")
        conn = _Pinned(host, port, address, left, scheme == "https")
        try:
            default = 443 if scheme == "https" else 80
            conn.putrequest("GET", path, skip_host=True, skip_accept_encoding=True)
            conn.putheader("Host", host if port == default else f"{host}:{port}")
            conn.putheader("User-Agent", AGENT)
            conn.putheader("Accept", ", ".join(want))
            conn.putheader("Accept-Encoding", "identity")
            conn.endheaders()
            response = conn.getresponse()
            if response.status in (301, 302, 303, 307, 308):
                target = response.getheader("Location")
                if not target:
                    raise NotFetched("That page redirects nowhere.")
                url = urljoin(url, target.strip())
                continue
            if response.status != 200:
                raise NotFetched(f"That page answered {response.status}.")
            kind = (response.getheader("Content-Type") or "").split(";")[0].strip().lower()
            if kind not in want:
                raise NotFetched("That address isn't a web page." if "text/html" in want else "That picture isn't one the hub shows.")
            if (response.getheader("Content-Encoding") or "identity").lower() != "identity":
                raise NotFetched("That page came compressed.")
            body = b""
            while len(body) <= MAX_BYTES:
                if time.monotonic() > deadline:
                    raise NotFetched("That page took too long to answer.")
                if response.isclosed():
                    break
                conn.held.settimeout(max(0.05, deadline - time.monotonic()))
                chunk = response.read1(min(65536, MAX_BYTES + 1 - len(body)))  # whatever has come, so the deadline is checked between arrivals
                if not chunk:
                    break
                body += chunk
            return url, kind, body[:MAX_BYTES], len(body) > MAX_BYTES
        except (OSError, http.client.HTTPException) as error:
            if isinstance(error, socket.timeout) or "timed out" in str(error):
                raise NotFetched("That page took too long to answer.")
            raise NotFetched("That page couldn't be reached.")
        finally:
            conn.close()
    raise NotFetched("That page redirects too many times.")


# ── reading a page ──
class _Head(html.parser.HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.meta, self.icons, self.title, self._in_title, self.done = {}, [], "", False, False

    def handle_starttag(self, tag, attrs):
        if self.done:
            return
        a = {k.lower(): (v or "") for k, v in attrs}
        if tag == "title":
            self._in_title = True
        elif tag == "meta":
            key = (a.get("property") or a.get("name") or "").lower()
            if key and "content" in a and key not in self.meta:
                self.meta[key] = a["content"]
        elif tag == "link" and a.get("href"):
            rel = set(a.get("rel", "").lower().split())
            if rel & {"icon", "apple-touch-icon"}:
                self.icons.append((a["href"], "apple-touch-icon" in rel, a.get("sizes", "")))
        elif tag == "body":
            self.done = True

    def handle_endtag(self, tag):
        if tag == "title":
            self._in_title = False
        if tag == "head":
            self.done = True

    def handle_data(self, data):
        if self._in_title and len(self.title) < 400:
            self.title += data


def _tidy(text, limit):
    text = re.sub(r"\s+", " ", text or "").strip()
    return text if len(text) <= limit else text[:limit - 1].rstrip() + "…"


def _key(text: str) -> str:
    return hashlib.sha256(text.encode()).hexdigest()[:32]


def _picture(base, value):
    """An id for a picture the page names, kept so the hub fetches only pictures a fetched card pointed to."""
    if not value:
        return None
    url = urljoin(base, value.strip())
    if urlsplit(url).scheme not in PORTS:
        return None
    pid = _key(url)
    folder = CACHE / "img"
    folder.mkdir(parents=True, exist_ok=True)
    (folder / f"{pid}.url").write_text(url)
    return pid


def read_card(url, body):
    head = _Head()
    try:
        text = body.decode("utf-8")
    except UnicodeDecodeError:
        text = body.decode("latin-1")
    try:
        head.feed(text)
    except Exception:  # a broken page still gives what was read before it broke
        pass
    m = head.meta
    host = urlsplit(url).hostname or ""
    icons = sorted(head.icons, key=lambda i: (not i[1], i[2] == ""))  # a touch icon is sharper than a 16px favicon
    return {"url": url, "host": host.removeprefix("www."),
            "title": _tidy(m.get("og:title") or m.get("twitter:title") or head.title, 200),
            "description": _tidy(m.get("og:description") or m.get("twitter:description") or m.get("description"), 300),
            "site": _tidy(m.get("og:site_name"), 80),
            "image": _picture(url, m.get("og:image") or m.get("og:image:url") or m.get("twitter:image")),
            "icon": _picture(url, icons[0][0] if icons else "/favicon.ico")}


def _cached(path: Path, keep: int):
    try:
        kept = json.loads(path.read_text())
        return kept if time.time() - kept["at"] < keep else None
    except (OSError, ValueError, KeyError, TypeError):
        return None


def link_card(url: str, fetch_it: bool = True) -> dict:
    """The card for a link: title, description, site name, and ids for its picture and icon (see `picture`)."""
    url = str(url or "").strip()
    if not re.match(r"^https?://", url, re.I) or len(url) > 2048:
        raise NotFetched("Only web addresses (http and https) are previewed.")
    path = CACHE / f"{_key(url)}.json"
    kept = _cached(path, KEEP)
    if kept and "card" in kept:
        return kept["card"]
    if kept and "refused" in kept and time.time() - kept["at"] < KEEP_REFUSED:
        raise NotFetched(kept["refused"])
    if not fetch_it:
        return {}
    CACHE.mkdir(parents=True, exist_ok=True)
    try:
        final, _, body, _ = fetch(url, PAGE_TYPES)
        card = {**read_card(final, body), "asked": url}
    except NotFetched as refused:
        path.write_text(json.dumps({"at": time.time(), "refused": str(refused)}))
        raise
    path.write_text(json.dumps({"at": time.time(), "card": card}))
    return card


def cached_cards(urls) -> dict:
    """The cards already fetched for these links. Never fetches: this is what a list can show without anyone pointing."""
    out = {}
    for url in list(urls)[:60]:
        try:
            card = link_card(url, fetch_it=False)
        except Refused:
            continue
        if card:
            out[url] = card
    return out


def sniff(data: bytes, kind: str) -> bool:
    lead = PICTURES.get(kind)
    return bool(lead) and data.startswith(lead) and (kind != "image/webp" or data[8:12] == b"WEBP")


def picture(pid: str):
    """A card's picture or icon, through the hub. Only ids a fetched card named; fetched under the same guards."""
    if not re.fullmatch(r"[0-9a-f]{32}", pid or ""):
        raise NotFetched("That picture isn't available.")
    folder = CACHE / "img"
    data_path, meta_path = folder / f"{pid}.bin", folder / f"{pid}.json"
    meta = _cached(meta_path, KEEP)
    if meta and meta.get("type") and data_path.exists():
        return data_path.read_bytes(), meta["type"]
    if meta and meta.get("refused") and time.time() - meta["at"] < KEEP_REFUSED:
        raise NotFetched(meta["refused"])
    try:
        url = (folder / f"{pid}.url").read_text()
    except OSError:
        raise NotFetched("That picture isn't available.")
    try:
        _, kind, data, cut = fetch(url, tuple(PICTURES))
        if cut:
            raise NotFetched("That picture is over 512 KB.")
        if not sniff(data, kind):
            raise NotFetched("That picture isn't one the hub shows.")
    except NotFetched as refused:
        meta_path.write_text(json.dumps({"at": time.time(), "refused": str(refused)}))
        raise
    data_path.write_bytes(data)
    meta_path.write_text(json.dumps({"at": time.time(), "type": kind}))
    return data, kind


# ── what a shared file is, for a hover card ──
def image_size(path: Path):
    """Width and height from the file's header, for PNG, GIF, JPEG and WebP. None when it can't tell."""
    try:
        with open(path, "rb") as f:
            head = f.read(64 * 1024)
    except OSError:
        return None
    try:
        if head.startswith(b"\x89PNG") and head[12:16] == b"IHDR":
            return struct.unpack(">II", head[16:24])
        if head[:6] in (b"GIF87a", b"GIF89a"):
            return struct.unpack("<HH", head[6:10])
        if head[:4] == b"RIFF" and head[8:12] == b"WEBP":
            chunk = head[12:16]
            if chunk == b"VP8X":
                return int.from_bytes(head[24:27], "little") + 1, int.from_bytes(head[27:30], "little") + 1
            if chunk == b"VP8L":
                bits = int.from_bytes(head[21:25], "little")
                return (bits & 0x3FFF) + 1, ((bits >> 14) & 0x3FFF) + 1
            if chunk == b"VP8 ":
                w, h = struct.unpack("<HH", head[26:30])
                return w & 0x3FFF, h & 0x3FFF
        if head[:2] == b"\xff\xd8":
            at = 2
            while at + 9 < len(head):
                if head[at] != 0xFF:
                    at += 1
                    continue
                marker = head[at + 1]
                if marker in (0xC0, 0xC1, 0xC2, 0xC3, 0xC5, 0xC6, 0xC7, 0xC9, 0xCA, 0xCB, 0xCD, 0xCE, 0xCF):
                    h, w = struct.unpack(">HH", head[at + 5:at + 9])
                    return w, h
                if marker in (0xD8, 0x01) or 0xD0 <= marker <= 0xD7:
                    at += 2
                    continue
                at += 2 + struct.unpack(">H", head[at + 2:at + 4])[0]
    except struct.error:
        return None
    return None


def file_info(sid: str, raw: str) -> dict:
    """Name, folder, size, time, and for text its line count and first lines; for an image its pixel size."""
    path = hub.resolve_shared(sid, raw)
    st = path.stat()
    kind = hub.kind_of(path.name)
    home = str(Path.home())
    folder = str(path.parent)
    info = {"name": path.name, "path": str(path), "folder": "~" + folder[len(home):] if folder.startswith(home) else folder,
            "kind": kind, "size": st.st_size, "modified": st.st_mtime}
    if kind == "text":
        with open(path, "rb") as f:
            data = f.read(2_000_000)
        text = data.decode("utf-8", errors="replace")
        info["lines"] = text.count("\n") + (0 if text.endswith("\n") or not text else 1)
        info["more"] = st.st_size > len(data)
        info["head"] = [line[:160] for line in text.splitlines()[:8]]
    elif kind == "image":
        size = image_size(path)
        if size:
            info["width"], info["height"] = size
    return info


# ── thumbnails ──
THUMB_PX = 320


def thumbnail(sid: str, path_text: str = "", ref: str = ""):
    """A small copy of a shared image, made once with macOS's sips and kept, so a panel of screenshots stays light.
    An image sips can't read (or an SVG, which is drawn at any size) comes back whole."""
    if ref:
        data, mime = hub.blob_of(sid, ref)
        source, key = None, _key(f"{sid}:{ref}")
    else:
        source = hub.resolve_shared(sid, path_text)
        if hub.kind_of(source.name) != "image":
            raise Refused("That file isn't an image.")
        st = source.stat()
        mime, key = hub.mime_of(source), _key(f"{source}:{st.st_mtime_ns}:{st.st_size}")
        data = None
    if mime == "image/svg+xml":
        return source.read_bytes(), mime
    folder = CACHE / "thumbs"
    folder.mkdir(parents=True, exist_ok=True)
    out_kind = "jpeg" if mime == "image/jpeg" else "png"
    out = folder / f"{key}.{out_kind}"
    if out.exists():
        return out.read_bytes(), f"image/{out_kind}"
    small = (len(data) if data is not None else source.stat().st_size) < 48_000
    if not small:
        with tempfile.TemporaryDirectory(dir=folder) as scratch:
            given = source
            if data is not None:
                given = Path(scratch) / f"in.{mime.split('/')[1]}"
                given.write_bytes(data)
            made = Path(scratch) / f"out.{out_kind}"
            try:
                subprocess.run(["/usr/bin/sips", "-Z", str(THUMB_PX), "-s", "format", out_kind, str(given), "--out", str(made)],
                               capture_output=True, timeout=8, check=True)
                os.replace(made, out)
                return out.read_bytes(), f"image/{out_kind}"
            except (OSError, subprocess.SubprocessError):
                pass
    return (data if data is not None else source.read_bytes()), mime
