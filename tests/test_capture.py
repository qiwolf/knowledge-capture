import hashlib
import socket
from unittest.mock import Mock, patch

import pytest

from knowledge_capture import capture


def dns(*ips):
    return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", (ip, 443)) for ip in ips]


@pytest.mark.parametrize("ip", ["127.0.0.1", "10.0.0.2", "169.254.169.254", "192.168.1.1", "::1", "fc00::1", "0.0.0.0", "224.0.0.1", "ff02::1"])
def test_private_dns_rejected_before_network(ip):
    with patch.object(capture.socket, "getaddrinfo", return_value=dns(ip)), patch.object(capture.requests, "Session") as session:
        with pytest.raises(capture.CaptureError) as error:
            capture._request("https://example.org/", 100)
    assert error.value.code == "unsafe_url"
    session.assert_not_called()


def test_mixed_public_private_dns_rejected():
    with patch.object(capture.socket, "getaddrinfo", return_value=dns("93.184.216.34", "10.0.0.1")):
        with pytest.raises(capture.CaptureError, match="非公开"):
            capture._target("https://example.org")


@pytest.mark.parametrize("url", ["file:///etc/passwd", "https://user:secret@example.org/", "http://example.org:bad/", "ftp://example.org/"])
def test_invalid_urls(url):
    with pytest.raises(capture.CaptureError) as error:
        capture._target(url)
    assert error.value.code == "invalid_url"


def response(status=200, headers=None, chunks=None):
    result = Mock()
    result.status_code = status
    result.headers = headers or {"Content-Type": "text/html"}
    result.iter_content.return_value = chunks or [b"hello"]
    result.__enter__ = Mock(return_value=result)
    result.__exit__ = Mock(return_value=False)
    return result


def test_redirect_cannot_enter_private_network():
    session = Mock()
    session.__enter__ = Mock(return_value=session)
    session.__exit__ = Mock(return_value=False)
    session.get.return_value = response(302, {"Location": "http://private.test/"})
    with patch.object(capture.socket, "getaddrinfo", side_effect=[dns("93.184.216.34"), dns("10.1.1.1")]), patch.object(capture.requests, "Session", return_value=session):
        with pytest.raises(capture.CaptureError) as error:
            capture._request("https://public.test/", 100)
    assert error.value.code == "unsafe_url"
    assert session.get.call_count == 1
    assert session.trust_env is False


def test_tls_connects_to_pinned_ip_but_verifies_hostname():
    with patch.object(capture, "HTTPSConnectionPool") as pool:
        adapter = capture._PinnedAdapter("public.test", "93.184.216.34", 443, True)
    assert pool.call_args.kwargs["host"] == "93.184.216.34"
    assert pool.call_args.kwargs["server_hostname"] == "public.test"
    assert pool.call_args.kwargs["assert_hostname"] == "public.test"
    assert pool.call_args.kwargs["cert_reqs"] == "CERT_REQUIRED"
    adapter.close()


def test_stream_limit_even_without_content_length():
    session = Mock()
    session.__enter__ = Mock(return_value=session)
    session.__exit__ = Mock(return_value=False)
    session.get.return_value = response(chunks=[b"1234", b"5678"])
    with patch.object(capture, "_target", return_value=("public.test", "93.184.216.34", 443, True)), patch.object(capture.requests, "Session", return_value=session):
        with pytest.raises(capture.CaptureError) as error:
            capture._request("https://public.test", 6)
    assert error.value.code == "too_large"


ARTICLE = '''<html><head><meta charset="utf-8"><title>知识采集的工程实践</title></head><body><article>
<h1>知识采集的工程实践</h1>
<p>知识采集需要保存完整来源，并且区分用户主动添加的材料与系统推荐的内容。收集到链接之后，必须先检查网页是否真的包含正文，再保留有效的信息、表格和图片。任何处理失败都应留下清楚的原因，以便用户补充资料。</p>
<p>对于设备漏洞的分析，还需要知道设备正在运行的版本，以及这条版本记录的更新时间。旧资料不能自动当作当前事实，推断也不应该覆盖原始证据。这样的知识库才能同时支持人工阅读和机器检索。</p>
<table><tr><th>项目</th><th>要求</th></tr><tr><td>来源</td><td>保留地址</td></tr></table>
<pre><code>print("capture")</code></pre>
<p><img data-src="/diagram.png" alt="知识流程图"></p>
</article></body></html>'''.encode()


def test_article_and_wechat_lazy_image(tmp_path):
    image = b"\x89PNG\r\n\x1a\n" + b"test-image" * 10
    with patch.object(capture, "_request", side_effect=[(ARTICLE, "text/html", "https://public.test/article"), (image, "image/png", "https://public.test/diagram.png")]) as fetch:
        result = capture.capture_url("https://public.test/short", tmp_path / "assets")
    assert result["status"] == "complete"
    assert "工程实践" in result["title"]
    assert "保留地址" in result["markdown"]
    assert "capture" in result["markdown"]
    assert result["assets"][0]["sha256"] == hashlib.sha256(image).hexdigest()
    assert (tmp_path / result["assets"][0]["relative_path"]).read_bytes() == image
    assert fetch.call_args_list[1].args[0] == "https://public.test/diagram.png"


def test_failed_image_is_partial_and_preserves_reference(tmp_path):
    with patch.object(capture, "_request", side_effect=[(ARTICLE, "text/html", "https://public.test/article"), capture.CaptureError("unsafe_url", "已拒绝")]):
        result = capture.capture_url("https://public.test/article", tmp_path / "assets")
    assert result["status"] == "partial"
    assert result["assets"][0]["status"] == "failed"
    assert "https://public.test/diagram.png" in result["markdown"]
    assert result["warnings"]


@pytest.mark.parametrize("body", [b"<html><h1>Title only</h1></html>", ("<article><h1>安全验证</h1><p>" + "请完成验证码验证后再继续访问本文内容。" * 20 + "</p></article>").encode()])
def test_no_article_or_captcha_is_not_success(body, tmp_path):
    with patch.object(capture, "_request", return_value=(body, "text/html", "https://public.test")):
        with pytest.raises(capture.CaptureError) as error:
            capture.capture_url("https://public.test", tmp_path)
    assert error.value.code == "no_content"


@pytest.mark.parametrize("mime", ["video/mp4", "application/pdf", "application/json"])
def test_non_html_is_explicitly_unsupported(mime, tmp_path):
    with patch.object(capture, "_request", return_value=(b"bytes", mime, "https://public.test")):
        with pytest.raises(capture.CaptureError) as error:
            capture.capture_url("https://public.test", tmp_path)
    assert error.value.code == "unsupported"


def test_copyright_year_is_not_publication_date(tmp_path):
    body = ARTICLE.replace(b"</body>", b"<footer>Copyright 2001-2026</footer></body>")
    with patch.object(capture, "_request", return_value=(body, "text/html", "https://public.test/article")):
        result = capture.capture_url("https://public.test/article", tmp_path)
    assert result["published_at"] is None


@pytest.mark.parametrize("metadata", [
    '<meta property="article:published_time" content="2026-10-06T09:15:00+08:00">',
    '<meta itemprop="datePublished" content="2026-10-06T09:15:00+08:00">',
    '<script type="application/ld+json">{"@context":"https://schema.org","@type":"Article","datePublished":"2026-10-06T09:15:00+08:00"}</script>',
])
def test_explicit_publication_metadata_retained(metadata, tmp_path):
    body = ARTICLE.replace(b"</head>", metadata.encode() + b"</head>")
    with patch.object(capture, "_request", return_value=(body, "text/html", "https://public.test/article")):
        result = capture.capture_url("https://public.test/article", tmp_path)
    assert result["published_at"] == "2026-10-06T09:15:00+08:00"


def test_modification_date_and_invalid_publication_ignored():
    tree = capture.html.fromstring('<html><head><meta property="article:modified_time" content="2026-10-06"><meta property="article:published_time" content="2001"></head></html>')
    assert capture._published_at(tree) is None


def test_html_multiline_code_preserves_exact_text_and_excludes_navigation(tmp_path):
    """Syntax-highlighted pre contents must survive as exact fenced code blocks."""
    from lxml import html
    import re
    body = ARTICLE.decode().replace('<p><img data-src="/diagram.png" alt="知识流程图"></p>', '')
    snippets = '''<div class="highlight-python"><pre><span class="k">for</span> item in values:
    <span class="k">if</span> item &lt; 3:
        print(<span class="s">"中文 &amp; preserved"</span>)

    # literal Markdown fence: ```
    print("done")
</pre></div>
<pre><code>def render():<br>\treturn `value`<br>    #  spaces retained  </code></pre>
<code>if ready:
    nested_call()
</code>'''
    body = body.replace('</article>', snippets + '</article>')
    body = body.replace('<body>', '<body><nav><pre>navigation_only()\n    do_not_import()</pre></nav>')
    tree = html.fromstring(body)
    expected = []
    for block in tree.xpath('//article//pre | //article//code[not(ancestor::pre)]'):
        for br in block.xpath('.//br'):
            br.tail = '\n' + (br.tail or '')
        expected.append(block.text_content())
    output = capture.extract_html(body.encode(), 'https://public.test/article', tmp_path)
    assert output['status'] == 'complete'
    assert 'navigation_only' not in output['markdown']
    assert 'do_not_import' not in output['markdown']
    assert 'KCCODE' not in output['markdown']
    for code in expected:
        fence = '`' * max(3, 1 + max((len(run) for run in re.findall(r'`+', code)), default=0))
        assert fence + '\n' + code + ('' if code.endswith('\n') else '\n') + fence in output['markdown']
    assert len(expected) == 4
    assert '完整来源' in output['markdown']


@pytest.mark.parametrize('fence,shorter', [('````', '```'), ('~~~~', '~~~')])
def test_markdown_code_image_examples_never_fetch_or_change(tmp_path, fence, shorter):
    prose = '公开资料的图像示例用于解释引用方式，代码必须保留原始字符和换行。' * 5
    code = (fence + 'markdown\r\n'
            '    ![example](https://public.test/example.png)\r\n'
            '<img src="https://public.test/also-code.png">\r\n'
            + shorter + '\r\n'
            '![still code](https://public.test/still-code.png)\r\n'
            + fence + '\r\n')
    original = prose + '\n\n' + code + '\nTrailing prose.'
    with patch.object(capture, '_request') as fetch:
        result = capture.extract_markdown(original, 'https://public.test/article', tmp_path, title='代码示例')
    fetch.assert_not_called()
    assert result['markdown'] == original
    assert result['status'] == 'complete'
    assert result['assets'] == []


def test_markdown_localizes_prose_image_but_preserves_unclosed_code(tmp_path):
    original = ('![real](https://public.test/real.png)\n\n'
                '```markdown\n![example](https://public.test/example.png)\n')
    image = b'\x89PNG\r\n\x1a\nfixture'
    with patch.object(capture, '_request', return_value=(image, 'image/png', 'https://public.test/real.png')) as fetch:
        result = capture.extract_markdown(original, 'https://public.test/article', tmp_path, title='代码', validate_article=False)
    assert fetch.call_count == 1
    assert fetch.call_args.args[0] == 'https://public.test/real.png'
    assert result['markdown'].endswith('```markdown\n![example](https://public.test/example.png)\n')
    assert '![real](assets/' in result['markdown']


def test_highlighted_code_is_rendered_as_code_not_prose(tmp_path):
    """Use the shipped renderer, not string containment, to verify fence meaning."""
    import json
    import subprocess
    from pathlib import Path
    code = '>>> for item in [1, 2]:\n...     print(item)\n...\n1\n2\n'
    article = ARTICLE.decode().replace('<p><img data-src="/diagram.png" alt="知识流程图"></p>', '')
    article = article.replace('<pre><code>print("capture")</code></pre>',
                              '<div class="highlight-python notranslate"><div class="highlight"><pre><span></span>' + code + '</pre></div></div>')
    output = capture.extract_html(article.encode(), 'https://public.test/article', tmp_path)
    probe = Path(__file__).with_name('capture_renderer_probe.mjs')
    rendered = json.loads(subprocess.run(['node', str(probe)], input=output['markdown'], text=True,
                                        capture_output=True, check=True).stdout)
    assert rendered['codes'] == [code.removesuffix('\n')]
    assert '保存完整来源' in rendered['prose']
    assert '设备正在运行的版本' in rendered['prose']
    assert 'print(item)' not in rendered['prose']


def test_code_inside_list_does_not_invert_following_prose_and_code(tmp_path):
    import json
    import subprocess
    from pathlib import Path
    prose = '正文说明必须留在正文，列表项中的代码示例也必须保留明确的代码边界。' * 5
    first = 'from enum import Enum\nclass Color(Enum):\n    RED = "red"\n'
    second = 'def fib(n):\n    return n + 1\n'
    body = ('<html><meta charset="utf-8"><article><h1>列表内示例</h1><p>' + prose + '</p>'
            '<ul><li><p>模式可以使用具名常量，下面给出完整示例：</p>'
            '<div class="highlight"><pre>' + first + '</pre></div></li></ul>'
            '<p>第二个独立示例之前的正文，必须作为段落呈现。</p>'
            '<div class="highlight"><pre>' + second + '</pre></div>'
            '<p>示例之后的结论说明，也必须作为段落呈现。</p></article></html>')
    result = capture.extract_html(body.encode(), 'https://public.test/article', tmp_path)
    output = json.loads(subprocess.run(['node', str(Path(__file__).with_name('capture_renderer_probe.mjs'))],
                                      input=result['markdown'], text=True, capture_output=True, check=True).stdout)
    assert output['codes'] == [first.removesuffix('\n'), second.removesuffix('\n')]
    assert '第二个独立示例之前的正文' in output['prose']
    assert '示例之后的结论说明' in output['prose']
    assert 'def fib(n)' not in output['prose']


def test_referenced_semantic_footnotes_retained_without_site_notes(tmp_path):
    import json
    import subprocess
    from pathlib import Path
    article = ARTICLE.decode().replace('<p><img data-src="/diagram.png" alt="知识流程图"></p>', '')
    article = article.replace('</article>', '''<p>文章中的运算结果需要结合条件说明<a role="doc-noteref" href="#fn-1">[1]</a>。</p>
<aside id="fn-1" role="doc-footnote"><span class="label">[1]</span><p><code>**</code> 比 <code>-</code> 优先，因此 <code>-3**2</code> 为 <code>-9</code>；仅当加括号时条件变化。</p></aside>
<aside id="unused" role="doc-footnote"><p>UNREFERENCED_SITE_NOTE</p></aside></article>
<nav><a href="#nav-note">菜单脚注</a><aside id="nav-note" role="doc-footnote"><p>NAVIGATION_NOTE</p></aside></nav>
<footer><aside id="footer-note" role="doc-footnote"><p>FOOTER_NOTE</p></aside></footer>''')
    result = capture.extract_html(article.encode(), 'https://public.test/article', tmp_path)
    parsed = json.loads(subprocess.run(['node', str(Path(__file__).with_name('capture_renderer_probe.mjs'))],
                                     input=result['markdown'], text=True, capture_output=True, check=True).stdout)
    assert '** 比 - 优先，因此 -3**2 为 -9；仅当加括号时条件变化。' in parsed['prose']
    assert result['status'] == 'complete'
    for excluded in ['UNREFERENCED_SITE_NOTE', 'NAVIGATION_NOTE', 'FOOTER_NOTE']:
        assert excluded not in result['markdown']


def test_unsupported_referenced_footnote_marks_partial(tmp_path):
    article = ARTICLE.decode().replace('<p><img data-src="/diagram.png" alt="知识流程图"></p>', '')
    article = article.replace('</article>', '<p>详情见说明<a href="#complex">[1]</a>。</p><aside id="complex" role="doc-footnote"><table><tr><td>复杂条件</td></tr></table></aside></article>')
    result = capture.extract_html(article.encode(), 'https://public.test/article', tmp_path)
    assert result['status'] == 'partial'
    assert any('complex' in warning and '脚注' in warning for warning in result['warnings'])
