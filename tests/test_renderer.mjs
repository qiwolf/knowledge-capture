import test from 'node:test';
import assert from 'node:assert/strict';
import {readFile} from 'node:fs/promises';
// Load the browser ES module without changing the project's Node module mode.
const source = await readFile(new URL('../knowledge_capture/web/renderer.js', import.meta.url), 'utf8');
const {renderMarkdown} = await import('data:text/javascript;base64,' + Buffer.from(source).toString('base64'));

class Node {
  constructor(tag, doc, text = '') { this.tagName = tag; this.ownerDocument = doc; this.children = []; this.value = text; this.style = {}; this.listeners = {}; }
  set innerHTML(_) { throw new Error('Untrusted HTML sink used'); }
  set textContent(value) { this.value = String(value); this.children = []; }
  get textContent() { return this.value + this.children.map(child => child.textContent).join(''); }
  appendChild(child) { child.parent = this; this.children.push(child); return child; }
  replaceChildren(...children) { this.value = ''; this.children = []; for (const child of children) this.appendChild(child); }
  addEventListener(name, fn) { this.listeners[name] = fn; }
  remove() { if (this.parent) this.parent.children = this.parent.children.filter(child => child !== this); }
}
const doc = {createElement: tag => new Node(tag, doc), createTextNode: text => new Node('#text', doc, text)};
const root = () => doc.createElement('div');
const all = (node, tag) => [...(node.tagName === tag ? [node] : []), ...node.children.flatMap(child => all(child, tag))];
const ids = {sourceId: 'a'.repeat(24), version: 'b'.repeat(24)};

test('readable blocks, inline formatting and safe literal HTML', async () => {
  const container = root();
  await renderMarkdown('# Title\n\nA **bold** and *em* with `code`. <img src=x onerror=alert(1)>\n\n- item A\n- item B\n\n3. third\n\n> quote\n\n| Name | Value |\n| --- | --- |\n| x | y |\n\n```js\n<script>unsafe()</script>\n```\n\n[unknown][reference]', container);
  for (const tag of ['h1', 'strong', 'em', 'code', 'ul', 'ol', 'blockquote', 'table', 'pre']) assert.ok(all(container, tag).length, tag);
  assert.equal(all(container, 'ol')[0].start, 3);
  assert.equal(all(container, 'img').length, 0);
  assert.equal(all(container, 'script').length, 0);
  assert.ok(container.textContent.includes('<img src=x onerror=alert(1)>'));
  assert.ok(container.textContent.includes('[unknown][reference]'));
  assert.equal(all(container, 'pre')[0].className, 'markdown-code');
});

test('links reject active schemes, credentials and local paths; evidence delegates', async () => {
  const container = root(); let evidence;
  await renderMarkdown('[good](https://example.org/a) [js](javascript:alert) [file](file:///etc/passwd) [credentials](https://name:pass@example.org) [local](../file) [proof](evidence.md#item-42)', container, {onEvidence: anchor => { evidence = anchor; }});
  const links = all(container, 'a');
  assert.equal(links.length, 1);
  assert.equal(links[0].href, 'https://example.org/a');
  assert.equal(links[0].target, '_blank');
  assert.equal(links[0].rel, 'noopener noreferrer');
  assert.ok(container.textContent.includes('javascript:alert'));
  all(container, 'button')[0].listeners.click();
  assert.equal(evidence, 'item-42');
});

test('only registered asset route is requested; external/traversal images remain visible text', async () => {
  const container = root(), calls = [];
  const request = async (path, options) => { calls.push([path, options]); return new Response(new Blob(['bytes'], {type:'image/png'})); };
  await renderMarkdown('![ok](assets/frame.png) ![remote](https://remote.test/p.png) ![parent](assets/../secret.png) ![escaped](assets/%2e%2e.png) ![slash](assets/sub/file.png)', container, {...ids, request});
  assert.deepEqual(calls, [[`/api/assets/${ids.sourceId}/${ids.version}/frame.png`, {raw:true}]]);
  const img = all(container, 'img')[0];
  assert.equal(img.alt, 'ok'); assert.ok(img.src.startsWith('blob:'));
  assert.ok(container.textContent.includes('remote'));
  assert.ok(container.textContent.includes('未自动加载'));
  img.listeners.error();
  assert.ok(container.textContent.includes('加载失败'));
  assert.equal(all(container, 'img').length, 0);
});

test('Blob contract, failed request and unsupported SVG are handled safely', async () => {
  for (const request of [async () => { throw new Error('secret'); }, async () => new Blob(['svg'], {type:'image/svg+xml'}), async () => new Response('bad', {status:404})]) {
    const container = root();
    await renderMarkdown('![photo](assets/photo.png)', container, {...ids, request});
    assert.ok(container.textContent.includes('加载失败'));
    assert.ok(!container.textContent.includes('secret'));
    assert.equal(all(container, 'img').length, 0);
  }
  const container = root();
  await renderMarkdown('![photo](assets/photo.png)', container, {...ids, request: async () => new Blob(['ok'], {type:'image/png'})});
  assert.equal(all(container, 'img').length, 1);
});

test('re-render ignores stale image completion and preserves malformed syntax', async () => {
  const container = root(); let finish;
  const first = renderMarkdown('![photo](assets/photo.png)', container, {...ids, request: () => new Promise(resolve => {finish = resolve;})});
  await renderMarkdown('```unfinished\nraw content\n[broken](\n<iframe>', container);
  finish(new Blob(['ok'], {type:'image/png'})); await first;
  assert.equal(all(container, 'img').length, 0);
  assert.ok(container.textContent.includes('```unfinished'));
  assert.ok(container.textContent.includes('[broken]('));
  assert.ok(container.textContent.includes('<iframe>'));
});

test('source.md delegates only with an explicit callback', async () => {
  const container = root(); let opened = false;
  await renderMarkdown('[查看完整来源与图片](source.md)', container, {onSource: () => {opened = true;}});
  assert.equal(all(container, 'a').length, 0);
  all(container, 'button')[0].listeners.click();
  assert.equal(opened, true);
  await renderMarkdown('[查看完整来源与图片](source.md)', container);
  assert.equal(all(container, 'button').length, 0);
  assert.ok(container.textContent.includes('(source.md)'));
});

test('Wiki assets use source snapshot version, never the version in a link', async () => {
  const sid = 'c'.repeat(24), sourceVersion = 'd'.repeat(24), topic = 'interest_' + 'e'.repeat(24), wikiVersion = 'f'.repeat(32);
  const prefix = `../versions/${topic}/${wikiVersion}/sources/${sid}/`;
  const calls = [], container = root();
  const options = {sources: {[sid]: {version_id: sourceVersion}}, request: async path => {calls.push(path); return new Blob(['ok'], {type:'image/png'});}};
  await renderMarkdown(`![wiki](${prefix}assets/a.png) ![bad](${prefix}assets/../a.png) ![unknown](../versions/${topic}/${wikiVersion}/sources/${'0'.repeat(24)}/assets/a.png)`, container, options);
  assert.deepEqual(calls, [`/api/assets/${sid}/${sourceVersion}/a.png`]);
  assert.equal(all(container, 'img').length, 1);
  await renderMarkdown(`![unregistered](${prefix}assets/a.png)`, container, {request: options.request});
  assert.equal(calls.length, 1);
  assert.ok(container.textContent.includes('未自动加载'));
});

test('Wiki source links delegate registered snapshot IDs and retain untrusted paths as text', async () => {
  const sid = 'c'.repeat(24), version = 'd'.repeat(24);
  const target = `../versions/interest_${'e'.repeat(24)}/${'f'.repeat(32)}/sources/${sid}/source.md`;
  const container = root(); let received;
  const options = {sources: {[sid]: {version_id: version}}, onSource: (...args) => {received = args;}};
  await renderMarkdown(`[原文](${target})`, container, options);
  assert.equal(all(container, 'a').length, 0);
  all(container, 'button')[0].listeners.click();
  assert.deepEqual(received, [sid, version]);
  for (const opt of [{onSource: options.onSource}, {sources: options.sources}, {...options, sources: {[sid]: {version_id: '../../'}}}]) {
    await renderMarkdown(`[原文](${target})`, container, opt);
    assert.equal(all(container, 'button').length, 0);
    assert.ok(container.textContent.includes(target));
  }
});

test('analysis Markdown escapes render literal punctuation without spurious code or backslashes', async () => {
  const container = root();
  const prose = "说明：\\`\\_\\` 表示结果；print\\(\\) 与 \\*符号\\*、\\[资料\\]。";
  await renderMarkdown(prose, container);
  assert.equal(container.textContent, '说明：`_` 表示结果；print() 与 *符号*、[资料]。');
  assert.equal(all(container,'code').length,0);
  assert.equal(all(container,'em').length,0);
});

test('escaped delimiters remain inert inside real emphasis, links, and literal unsafe text', async () => {
  const container = root();
  await renderMarkdown(String.raw`**name\_value** \[not a link\]\(https://example.org\) \<script\>safe\</script\>`, container);
  assert.equal(all(container,'strong')[0].textContent,'name_value');
  assert.equal(all(container,'a').length,0);
  assert.equal(all(container,'script').length,0);
  assert.ok(container.textContent.includes('[not a link](https://example.org)'));
  assert.ok(container.textContent.includes('<script>safe</script>'));
});

test('real code spans retain their literal backslashes and escaped slash stays a slash', async () => {
  const container = root();
  await renderMarkdown('`a\\_b` and '+String.raw`\\ \q`, container);
  assert.equal(all(container,'code')[0].textContent,String.raw`a\_b`);
  assert.equal(container.textContent,'a\\_b and \\ \\q');
});
