"""Read public text pages and publication metadata without running page scripts."""

import codecs
import json
import re
from collections import Counter
from datetime import date, datetime
from email.message import Message
from html.parser import HTMLParser
from urllib.parse import urlsplit

from clytie.sdk.types import Material


class WebError(Exception):
    def __init__(self, code, message=None):
        super().__init__(code)
        self.code = code
        self.message = message or code


def valid_url(url):
    try:
        parsed = urlsplit(url)
        return (
            parsed.scheme in {"https", "http"}
            and bool(parsed.hostname)
            and parsed.username is None
            and parsed.password is None
            and parsed.port in {None, 80, 443}
        )
    except ValueError:
        return False


def publication(value):
    if not isinstance(value, str):
        return None
    value = value.strip()
    try:
        if len(value) == 10:
            return date.fromisoformat(value).isoformat()
        return datetime.fromisoformat(value.replace("Z", "+00:00")).isoformat()
    except ValueError:
        return None


def structured_date(raw):
    try:
        obj = json.loads(raw)
    except (ValueError, RecursionError):
        return None
    pending = [obj]
    while pending:
        value = pending.pop()
        if isinstance(value, list):
            pending.extend(reversed(value))
        elif isinstance(value, dict):
            published = publication(value.get("datePublished"))
            if published:
                return published
            if "@graph" in value:
                pending.append(value["@graph"])
            if "mainEntity" in value:
                pending.append(value["mainEntity"])
    return None


class PageParser(HTMLParser):
    BLOCKS = {
        "article",
        "main",
        "p",
        "div",
        "li",
        "section",
        "h1",
        "h2",
        "h3",
        "h4",
        "h5",
        "h6",
        "blockquote",
        "pre",
        "tr",
    }
    HIDDEN = {
        "head",
        "nav",
        "header",
        "footer",
        "aside",
        "script",
        "style",
        "noscript",
        "template",
        "svg",
    }
    VOID = {
        "area",
        "base",
        "br",
        "col",
        "embed",
        "hr",
        "img",
        "input",
        "link",
        "meta",
        "param",
        "source",
        "track",
        "wbr",
    }

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.stack = []
        self.opened = Counter()
        self.title_parts = []
        self.body = []
        self.article = []
        self.published = self.time_date = self.structured = None
        self.ld = None
        self.login = False

    def handle_starttag(self, tag, attrs):
        attrs = dict(attrs)
        if tag == "meta" and attrs.get("property", attrs.get("name")) == "article:published_time":
            self.published = self.published or publication(attrs.get("content"))
        if tag == "time":
            self.time_date = self.time_date or publication(attrs.get("datetime"))
        if tag == "input" and (attrs.get("type") or "").lower() == "password":
            self.login = True
        if tag == "script" and attrs.get("type") == "application/ld+json":
            self.ld = []
        if tag in self.BLOCKS or tag == "br":
            self.body.append("\n")
            self.article.append("\n")
        if tag not in self.VOID:
            self.stack.append(tag)
            self.opened[tag] += 1

    def handle_endtag(self, tag):
        if tag == "script" and self.ld is not None:
            self.structured = self.structured or structured_date("".join(self.ld))
            self.ld = None
        if self.opened[tag]:
            while self.stack:
                closed = self.stack.pop()
                self.opened[closed] -= 1
                if not self.opened[closed]:
                    del self.opened[closed]
                if closed == tag:
                    break
        if tag in self.BLOCKS:
            self.body.append("\n")
            self.article.append("\n")

    def handle_data(self, data):
        if self.ld is not None:
            self.ld.append(data)
        if self.opened["title"]:
            self.title_parts.append(data)
        elif not any(self.opened[tag] for tag in self.HIDDEN):
            self.body.append(data)
            if self.opened["main"] or self.opened["article"]:
                self.article.append(data)
            if self.opened["time"] and not self.time_date:
                self.time_date = publication(data)

    @property
    def date(self):
        return self.published or self.structured or self.time_date

    def text(self):
        chunks = self.article if any(s.strip() for s in self.article) else self.body
        return "\n".join(
            v for line in "".join(chunks).splitlines() if (v := " ".join(line.split()))
        )


def decode(body, charset, html):
    candidates = [charset]
    if html:
        meta = re.search(rb"""<meta\b[^>]*?charset\s*=\s*['"]?\s*([\w.-]+)""", body[:4096], re.I)
        xml = re.match(rb"""\s*<\?xml\b[^>]*encoding=['"]([\w.-]+)""", body[:512], re.I)
        candidates += [m[1].decode("ascii") for m in (xml, meta) if m]
    for encoding in candidates:
        if encoding:
            try:
                return body.decode(encoding, "replace")
            except (LookupError, ValueError):
                pass
    if body.startswith((codecs.BOM_UTF16_LE, codecs.BOM_UTF16_BE)):
        return body.decode("utf-16", "replace")
    return body.decode("utf-8-sig", "replace")


async def fetch(ctx, url, *, metadata=False):
    if not valid_url(url):
        raise WebError("blocked_address")
    maximum = 512 * 1024 if metadata else 3 * 1024 * 1024
    try:
        response = await ctx.http.request(
            "GET",
            url,
            headers={"User-Agent": "Clytie"},
            max_bytes=maximum,
            timeout_s=5 if metadata else 20,
        )
    except TimeoutError as exc:
        raise WebError("timeout") from exc
    except ValueError as exc:
        if "max_bytes" in str(exc):
            raise WebError("too_large") from exc
        if str(exc) == "blocked_address":
            raise WebError("blocked_address") from exc
        if str(exc) in {"HTTP redirect limit exceeded", "HTTP redirect scheme downgrade forbidden"}:
            raise WebError("redirect_failed") from exc
        raise WebError("fetch_failed") from exc
    except OSError as exc:
        code = str(exc)
        if code not in {
            "dns_failed",
            "tls_failed",
            "connection_refused",
            "connection_reset",
            "invalid_payload",
        }:
            code = "fetch_failed"
        raise WebError(code) from exc
    if response.status in {401, 403}:
        raise WebError("needs_login")
    if not 200 <= response.status < 300:
        raise WebError(f"http_{response.status}")
    if len(response.body) > maximum:
        raise WebError("too_large")
    header = next((v for k, v in response.headers.items() if k.lower() == "content-type"), "")
    msg = Message()
    msg["content-type"] = header
    mime = msg.get_content_type()
    if not header or mime not in {"text/plain", "text/html", "application/xhtml+xml"}:
        raise WebError("not_text")
    text = decode(response.body, msg.get_content_charset(), mime != "text/plain")
    title = urlsplit(url).hostname
    published = None
    if mime != "text/plain":
        page = PageParser()
        page.feed(text)
        page.close()
        if metadata:
            return page.date
        title = " ".join("".join(page.title_parts).split()) or title
        published, text = page.date, page.text()
        if page.login and not any(s.strip() for s in page.article):
            raise WebError("needs_login")
    if metadata:
        return None
    if not text.strip():
        raise WebError("empty")
    return Material(title=title, url=url, text=text.strip(), published_at=published)
