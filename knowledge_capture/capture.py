"""Public HTML capture, with DNS-pinned outbound requests and local images."""
from __future__ import annotations

import hashlib
import ipaddress
import json
import re
import socket
import time
import uuid
from pathlib import Path
from datetime import datetime
from urllib.parse import urljoin, urlsplit

import requests
import trafilatura
from lxml import html
from requests.adapters import HTTPAdapter
from urllib3 import HTTPConnectionPool, HTTPSConnectionPool
from urllib3.util import Timeout


class CaptureError(Exception):
    def __init__(self, code: str, message: str):
        self.code = code
        super().__init__(message)


class _PinnedAdapter(HTTPAdapter):
    """Connect to the validated numeric address, retaining TLS SNI/verification."""
    def __init__(self, host: str, ip: str, port: int, secure: bool):
        super().__init__(max_retries=0)
        options = dict(host=ip, port=port, maxsize=1, block=True)
        self.pool = (HTTPSConnectionPool(
            **options, server_hostname=host, assert_hostname=host,
            cert_reqs="CERT_REQUIRED", ca_certs=requests.certs.where())
            if secure else HTTPConnectionPool(**options))

    def send(self, request, **kwargs):
        response = self.pool.urlopen(
            request.method, request.path_url, headers=request.headers,
            redirect=False, retries=False, preload_content=False,
            timeout=Timeout(connect=8, read=15))
        return self.build_response(request, response)

    def close(self):
        self.pool.close()
        super().close()


def _target(url: str) -> tuple[str, str, int, bool]:
    try:
        parts = urlsplit(url)
        if (parts.scheme not in {"http", "https"} or not parts.hostname
                or parts.username is not None or parts.password is not None):
            raise ValueError()
        host = parts.hostname.encode("idna").decode("ascii")
        port = parts.port or (443 if parts.scheme == "https" else 80)
        addresses = socket.getaddrinfo(host, port, type=socket.SOCK_STREAM)
        ips = {item[4][0] for item in addresses}
        def public(ip):
            address = ipaddress.ip_address(ip)
            return (address.is_global and not address.is_multicast
                    and not address.is_reserved and not address.is_unspecified)
        if not ips or not all(public(ip) for ip in ips):
            raise CaptureError("unsafe_url", "链接指向非公开网络地址，已拒绝采集。")
        return host, sorted(ips)[0], port, parts.scheme == "https"
    except CaptureError:
        raise
    except (ValueError, UnicodeError):
        raise CaptureError("invalid_url", "请输入不含账号密码的有效 HTTP 或 HTTPS 链接。") from None
    except OSError:
        raise CaptureError("dns_failed", "无法解析链接域名，请稍后重试。") from None


def _request(url: str, limit: int) -> tuple[bytes, str, str]:
    """Validate every redirect; no proxy, environment credentials or second DNS lookup."""
    current = url
    deadline = time.monotonic() + 60
    for hop in range(6):
        host, ip, port, secure = _target(current)
        host_header = f"[{host}]" if ":" in host else host
        if port != (443 if secure else 80):
            host_header += f":{port}"
        try:
            with requests.Session() as session:
                session.trust_env = False
                session.mount("https://" if secure else "http://",
                              _PinnedAdapter(host, ip, port, secure))
                with session.get(current, allow_redirects=False, stream=True,
                                 headers={"Host": host_header, "User-Agent": "KnowledgeCapture/0.1",
                                          "Accept": "*/*"}, timeout=(8, 15)) as response:
                    if response.status_code in {301, 302, 303, 307, 308}:
                        location = response.headers.get("Location")
                        if not location or hop == 5:
                            raise CaptureError("redirect_failed", "链接跳转过多或跳转地址缺失。")
                        current = urljoin(current, location)
                        continue
                    if response.status_code != 200:
                        raise CaptureError("http_error", f"来源返回 HTTP {response.status_code}，未取得有效正文。")
                    declared = response.headers.get("Content-Length", "")
                    if declared.isdigit() and int(declared) > limit:
                        raise CaptureError("too_large", "来源内容超过采集大小限制。")
                    chunks, size = [], 0
                    for chunk in response.iter_content(65536):
                        if time.monotonic() > deadline:
                            raise CaptureError("timeout", "来源下载超时，请稍后重试。")
                        size += len(chunk)
                        if size > limit:
                            raise CaptureError("too_large", "来源内容超过采集大小限制。")
                        chunks.append(chunk)
                    return b"".join(chunks), response.headers.get("Content-Type", "").split(";")[0].lower().strip(), current
        except CaptureError:
            raise
        except Exception as exc:
            # Do not expose tokens or credentials embedded in source URLs/errors.
            raise CaptureError("fetch_failed", "来源连接失败、超时或证书校验失败，请稍后重试。") from exc
    raise CaptureError("redirect_failed", "链接跳转过多。")


_IMAGE = re.compile(r"!\[([^\]]*)\]\(([^\s]+?)(?:\s+\"[^\"]*\")?\)")


def _published_at(tree) -> str | None:
    """Only explicit publication metadata; copyright and inferred dates are not evidence."""
    def valid(value):
        if not isinstance(value, str):
            return None
        value = value.strip()
        if not re.match(r"^\d{4}-\d{2}-\d{2}(?:$|T| )", value):
            return None
        try:
            datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError:
            return None
        return value

    candidates = []
    for node in tree.xpath("//meta"):
        keys = {node.get(key, "").lower() for key in ("property", "name", "itemprop")}
        if keys & {"article:published_time", "datepublished"}:
            candidates.append(valid(node.get("content")))
    for node in tree.xpath('//*[@itemprop="datePublished" and not(self::meta)]'):
        candidates.append(valid(node.get("datetime") or node.get("content") or node.text_content()))
    for node in tree.xpath('//script[@type="application/ld+json"]'):
        try:
            data = json.loads(node.text or "")
        except (ValueError, RecursionError):
            continue
        roots = data if isinstance(data, list) else [data]
        entries = []
        for root in roots:
            if isinstance(root, dict):
                entries.append(root)
                graph = root.get("@graph", [])
                if isinstance(graph, list):
                    entries.extend(graph)
        for entry in entries:
            if not isinstance(entry, dict):
                continue
            types = entry.get("@type", [])
            types = [types] if isinstance(types, str) else types
            if isinstance(types, list) and any(
                    isinstance(kind, str) and (kind.rsplit("/", 1)[-1].endswith("Article")
                                               or kind.rsplit("/", 1)[-1] == "BlogPosting")
                    for kind in types):
                candidates.append(valid(entry.get("datePublished")))
    dates = {value for value in candidates if value}
    return next(iter(dates)) if len(dates) == 1 else None


def _image_extension(data: bytes) -> str | None:
    if data.startswith(b"\x89PNG\r\n\x1a\n"):
        return "png"
    if data.startswith(b"\xff\xd8\xff"):
        return "jpg"
    if data.startswith((b"GIF87a", b"GIF89a")):
        return "gif"
    if data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return "webp"
    return None


def capture_url(url: str, asset_dir: Path) -> dict:
    data, mime, final_url = _request(url, 8 * 1024 * 1024)
    if mime not in {"text/html", "application/xhtml+xml"}:
        raise CaptureError("unsupported", "当前链接不是公开 HTML 文章；视频或其他文件请使用对应导入入口。")
    return extract_html(data, final_url, asset_dir, original_url=url)


def _protect_code_blocks(tree):
    """Keep extraction's body selection, then restore selected code losslessly.

    Markers surround the original text (rather than replacing it), so the
    extractor still sees its content when deciding which page region to keep.
    Only markers that survive body extraction authorize restoration; navigation
    examples are never appended from a page-wide list of pre elements.
    """
    blocks = []
    for node in tree.xpath('//pre | //code[not(ancestor::pre) and not(ancestor::code)]'):
        def text_of(element):
            value = element.text or ''
            for child in element:
                if isinstance(child.tag, str):
                    value += '\n' if child.tag.lower() == 'br' else text_of(child)
                value += child.tail or ''
            return value
        code = text_of(node).replace('\r\n', '\n').replace('\r', '\n')
        if node.tag == 'code' and '\n' not in code:
            continue
        marker = 'KCCODE' + uuid.uuid4().hex.upper()
        start, end = marker + 'START', marker + 'END'
        for child in list(node):
            node.remove(child)
        node.text = start + '\n' + code + '\n' + end
        fence = '`' * max(3, 1 + max((len(run) for run in re.findall(r'`+', code)), default=0))
        blocks.append((start, end, '\n\n' + fence + '\n' + code + ('' if code.endswith('\n') else '\n') + fence + '\n\n'))
    return blocks


def _append_referenced_footnotes(markdown, tree, document, final_url):
    """Restore semantic notes referenced by the extracted body, not site chrome."""
    page = urlsplit(final_url)._replace(fragment='')
    references = set()
    for link in document.body.xpath('.//*[@target]'):
        target = urlsplit(urljoin(final_url, link.get('target')))
        if target.fragment and target._replace(fragment='') == page:
            references.add(target.fragment)
    additions, warnings = [], []

    def inline(node):
        value = node.text or ''
        for child in node:
            if not isinstance(child.tag, str):
                value += child.tail or ''
                continue
            content = inline(child)
            if child.tag == 'code':
                ticks = '`' * max(1, 1 + max((len(x) for x in re.findall(r'`+', content)), default=0))
                content = ticks + content + ticks
            elif child.tag in {'em', 'i'}:
                content = '*' + content + '*'
            elif child.tag in {'strong', 'b'}:
                content = '**' + content + '**'
            elif child.tag == 'br':
                content = '\n'
            elif child.tag == 'a' and child.get('href'):
                content = '[' + content + '](' + child.get('href') + ')'
            value += content + (child.tail or '')
        return value

    notes = tree.xpath('//*[@role="doc-footnote" and @id and not(ancestor::nav) and not(ancestor::*[@role="navigation"])]')
    for note in notes:
        identifier = note.get('id')
        if identifier not in references:
            continue
        paragraphs = note.xpath('./p')
        unsupported = note.xpath('.//*[not(self::p or self::span or self::code or self::em or self::i or self::strong or self::b or self::br or self::a)]')
        extra_blocks = note.xpath('./*[not(self::p) and not(self::span[contains(concat(" ", normalize-space(@class), " "), " label ")])]')
        if not paragraphs or unsupported or extra_blocks or (note.text or '').strip():
            warnings.append(f'正文引用的脚注 {identifier} 结构尚不能完整保留，请核对原文。')
            continue
        additions.append('### 原文脚注 ' + identifier + '\n\n' + '\n\n'.join(inline(p) for p in paragraphs))
    if additions:
        markdown += '\n\n## 原文脚注\n\n' + '\n\n'.join(additions) + '\n'
    return markdown, warnings


def extract_html(data: bytes, final_url: str, asset_dir: Path, *, original_url: str | None = None, image_loader=None) -> dict:
    """Extract already acquired HTML; acquisition providers never execute page code."""
    try:
        tree = html.fromstring(data)
        tree.make_links_absolute(final_url)
        for node in tree.xpath("//img"):
            src = node.get("data-src") or node.get("data-original") or node.get("src")
            if src:
                node.set("src", urljoin(final_url, src))
            width, height = node.get("width", ""), node.get("height", "")
            if width.isdigit() and height.isdigit() and max(int(width), int(height)) <= 64:
                node.drop_tree()
        source = html.tostring(tree, encoding="unicode")
        options = dict(url=final_url, include_images=True, include_tables=True,
                       include_links=True, include_formatting=True, include_comments=False)
        document = trafilatura.bare_extraction(source, with_metadata=True, **options)
        blocks = _protect_code_blocks(tree)
        source = html.tostring(tree, encoding="unicode")
        markdown = trafilatura.extract(source, output_format="markdown", **options) or ""
        for start, end, fenced in blocks:
            begin = markdown.find(start)
            finish = markdown.find(end, begin + len(start)) if begin >= 0 else -1
            if begin >= 0 and finish >= 0:
                stop = finish + len(end)
                left, right = markdown[:begin], markdown[stop:]
                # Inspect only the immediate marker boundaries. A Markdown
                # wrapper can start after list prose on the same line; requiring
                # a line-start fence leaves its closing fence orphaned.
                opening = re.search(r'(`{3,}|~{3,})[ \t]*\r?\n[ \t]*$', left)
                closing = (re.match(r'^[ \t\r\n]*' + re.escape(opening[1])
                                    + r'[ \t]*(?=\r?\n|$)', right) if opening else None)
                if opening and closing:
                    begin = opening.start()
                    stop += closing.end()
                else:
                    ticks = re.search(r'(`+)$', left)
                    if ticks and right.startswith(ticks[1]):
                        begin = ticks.start()
                        stop += len(ticks[1])
                markdown = markdown[:begin] + fenced + markdown[stop:]
            # A damaged/incomplete extracted boundary is not visible page text.
            markdown = markdown.replace(start, '').replace(end, '')
    except Exception as exc:
        raise CaptureError("extraction_failed", "无法识别文章正文，请提供可阅读的页面或导出文件。") from exc
    if document is None:
        raise CaptureError("no_content", "未取得有效正文，可能是验证页、失效页面或内容过短。")
    markdown, footnote_warnings = _append_referenced_footnotes(markdown, tree, document, final_url)
    result = extract_markdown(markdown, final_url, asset_dir, title=document.title or "未命名文章",
                              original_url=original_url, image_loader=image_loader)
    if footnote_warnings:
        result['warnings'].extend(footnote_warnings)
        result['status'] = 'partial'
    result.update(author=document.author, published_at=_published_at(tree))
    return result


def extract_markdown(markdown: str, final_url: str, asset_dir: Path, *, title: str,
                     original_url: str | None = None, image_loader=None, validate_article: bool = True) -> dict:
    """Normalize provider Markdown and localize images, preserving partial failures."""
    if not isinstance(markdown, str) or not isinstance(title, str) or not title.strip():
        raise CaptureError("no_content", "读取服务未返回有效标题和正文。")
    plain = re.sub(r"\s+", "", _IMAGE.sub("", markdown))
    if validate_article and (len(plain) < 100 or len(set(plain)) < 25
            or (len(plain) < 1500 and re.search(
                r"访问过于频繁|环境异常|完成.{0,8}验证|验证码|verify you are human|access denied|captcha",
                plain, re.I))):
        raise CaptureError("no_content", "未取得有效正文，可能是验证页、失效页面或内容过短。")
    assets, warnings, cache = [], [], {}
    asset_dir = Path(asset_dir)

    def local_image(match):
        alt, original = match.group(1), urljoin(final_url, match.group(2).strip("<>"))
        if original not in cache:
            record = {"original_url": original, "relative_path": None, "sha256": None, "status": "failed"}
            try:
                image, image_mime, _ = (image_loader or _request)(original, 12 * 1024 * 1024)
                extension = _image_extension(image)
                if not image_mime.startswith("image/") or extension is None:
                    raise CaptureError("unsupported_image", "图片格式不受支持或返回内容不是图片。")
                digest = hashlib.sha256(image).hexdigest()
                filename = f"{digest}.{extension}"
                asset_dir.mkdir(parents=True, exist_ok=True)
                (asset_dir / filename).write_bytes(image)
                record.update(relative_path=f"assets/{filename}", sha256=digest, status="complete")
            except (CaptureError, OSError) as exc:
                record["error_code"] = getattr(exc, "code", "asset_write_failed")
                warnings.append(f"正文图片未保存：{record['error_code']}；已保留原图片链接。")
            assets.append(record)
            cache[original] = record
        record = cache[original]
        return f"![{alt}]({record['relative_path'] or original})"

    # Localize images only in prose. Fenced examples are source code, including
    # shorter fence-like lines and unclosed fences, and must remain byte-for-byte.
    parts, prose, fence = [], [], None

    def flush_prose():
        if prose:
            parts.append(_IMAGE.sub(local_image, ''.join(prose)))
            prose.clear()

    html_image_in_prose = False
    for line in markdown.splitlines(keepends=True):
        match = re.match(r'^ {0,3}(`{3,}|~{3,})([^\r\n]*)', line)
        if fence:
            parts.append(line)
            if (match and match[1][0] == fence[0] and len(match[1]) >= len(fence)
                    and not match[2].strip()):
                fence = None
        elif match and (match[1][0] != '`' or '`' not in match[2]):
            flush_prose()
            fence = match[1]
            parts.append(line)
        else:
            prose.append(line)
            html_image_in_prose |= bool(re.search(r'<img\b', line, re.I))
    flush_prose()
    markdown = ''.join(parts)
    if html_image_in_prose:
        warnings.append("读取服务的 Markdown 含未转换的 HTML 图片；需补充处理。")
    return {"title": title, "markdown": markdown,
            "original_url": original_url or final_url, "final_url": final_url,
            "author": None, "published_at": None,
            "assets": assets, "warnings": warnings,
            "status": "partial" if warnings else "complete"}
