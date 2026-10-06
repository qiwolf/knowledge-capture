/** Source Markdown is always untrusted text. No HTML parsing or remote image loads.
 * request(path, {raw:true}) must resolve to a Response (with blob()) or a Blob.
 * The assets API is responsible for verifying registration and content integrity.
 */
const activeRenders = new WeakMap();
const imageTypes = new Set(['image/png', 'image/jpeg', 'image/gif', 'image/webp']);

function externalLink(value) {
  try {
    if (!/^https?:\/\//i.test(value) || /[\u0000-\u0020\u007f]/.test(value)) return null;
    const url = new URL(value);
    return ['http:', 'https:'].includes(url.protocol) && !url.username && !url.password ? url.href : null;
  } catch { return null; }
}

function assetName(value) {
  // Decode is intentionally not applied: percent escapes, directories and query
  // strings are not registered literal filenames in this rendering contract.
  const match = /^assets\/([^/\\%?#\u0000-\u0020\u007f]+)$/.exec(value);
  return match && match[1] !== '.' && match[1] !== '..' ? match[1] : null;
}

export async function renderMarkdown(markdown, container, options = {}) {
  const previous = activeRenders.get(container);
  if (previous) {
    previous.cancelled = true;
    for (const url of previous.urls) URL.revokeObjectURL(url);
  }
  const state = {cancelled: false, urls: []};
  activeRenders.set(container, state);
  const doc = container.ownerDocument;
  const pending = [];
  container.replaceChildren();
  const element = (tag, text) => {
    const node = doc.createElement(tag);
    if (text !== undefined) node.textContent = text;
    return node;
  };
  const text = (parent, value) => parent.appendChild(doc.createTextNode(value));

  function image(parent, alt, target) {
    const holder = element('span');
    holder.className = 'markdown-image';
    const fallback = element('span', `图片：${alt || '未提供说明'}（未加载）`);
    holder.appendChild(fallback);
    parent.appendChild(holder);
    let name = assetName(target);
    let {sourceId, version} = options;
    const {request} = options;
    if (!name) {
      const wikiAsset = /^\.\.\/versions\/(interest_[a-f0-9]{24})\/([a-f0-9]{32})\/sources\/([a-f0-9]{24})\/(assets\/[^/]+)$/.exec(target);
      const sources = options.sources;
      if (wikiAsset && sources && Object.prototype.hasOwnProperty.call(sources, wikiAsset[3])) {
        const snapshot = sources[wikiAsset[3]];
        const candidate = assetName(wikiAsset[4]);
        if (candidate && snapshot && /^[a-f0-9]{24}$/.test(snapshot.version_id || '')) {
          name = candidate;
          sourceId = wikiAsset[3];
          // The URL's Wiki version never selects a source version.
          version = snapshot.version_id;
        }
      }
    }
    if (!name || !/^[a-f0-9]{24}$/.test(sourceId || '') ||
        !/^[A-Za-z0-9_-]+$/.test(version || '') || typeof request !== 'function') {
      fallback.textContent = `图片：${alt || '未提供说明'}（未自动加载：${target}）`;
      return;
    }
    pending.push((async () => {
      try {
        const path = `/api/assets/${encodeURIComponent(sourceId)}/${encodeURIComponent(version)}/${encodeURIComponent(name)}`;
        const response = await request(path, {raw: true});
        if (response && response.ok === false) throw new Error('asset request failed');
        const blob = response && typeof response.blob === 'function' ? await response.blob() : response;
        if (!(blob instanceof Blob) || !imageTypes.has(blob.type.toLowerCase())) throw new Error('unsupported image');
        if (state.cancelled) return;
        const url = URL.createObjectURL(blob);
        state.urls.push(url);
        const img = element('img');
        img.alt = alt || '来源图片';
        img.className = 'markdown-source-image';
        img.addEventListener('load', () => { if (!state.cancelled) fallback.remove(); });
        img.addEventListener('error', () => {
          img.remove();
          fallback.textContent = `图片：${alt || '未提供说明'}（加载失败）`;
          URL.revokeObjectURL(url);
        });
        img.src = url;
        holder.appendChild(img);
      } catch {
        if (!state.cancelled) fallback.textContent = `图片：${alt || '未提供说明'}（加载失败）`;
      }
    })());
  }

  function sourceTarget(target) {
    if (typeof options.onSource !== 'function') return null;
    if (target === 'source.md') return [];
    const match = /^\.\.\/versions\/interest_[a-f0-9]{24}\/[a-f0-9]{32}\/sources\/([a-f0-9]{24})\/source\.md$/.exec(target);
    if (!match || !options.sources || !Object.prototype.hasOwnProperty.call(options.sources, match[1])) return null;
    const snapshot = options.sources[match[1]];
    return snapshot && /^[a-f0-9]{24}$/.test(snapshot.version_id || '') ? [match[1], snapshot.version_id] : null;
  }

  function inline(parent, value, depth = 0, inheritedRestore = value => value) {
    if (depth > 8) { text(parent, inheritedRestore(value)); return; }
    // Shield escaped punctuation before looking for delimiters. Code spans keep
    // their backslashes verbatim; escaped ticks outside code cannot open a span.
    let marker = '\u0000escape:';
    while (value.includes(marker)) marker += ':';
    const escaped = [];
    value = value.replace(/(`+)([^`\n]+)\1|\\([!"#$%&'()*+,\-./:;<=>?@[\\\]^_`{|}~])/g,
      (whole, ticks, code, punctuation) => {
        if (!punctuation) return whole;
        const placeholder = marker + escaped.length + '\u0000';
        escaped.push(punctuation);
        return placeholder;
      });
    const restore = part => {
      let output = part;
      escaped.forEach((punctuation, index) => { output = output.split(marker + index + '\u0000').join(punctuation); });
      return inheritedRestore(output);
    };
    // Unrecognized or malformed syntax stays literal, including all raw HTML.
    const tokens = /(!?\[([^\]\n]*)\]\(([^\s)]+)(?:\s+["']([^\n]*?)["'])?\))|(`+)([^`\n]+)\5|(\*\*|__)(?=\S)(.+?)\7|(\*|_)(?=\S)(.+?)\9/g;
    let start = 0, match;
    while ((match = tokens.exec(value))) {
      text(parent, restore(value.slice(start, match.index)));
      if (match[1]) {
        const label = match[2], target = restore(match[3]), source = sourceTarget(target);
        if (match[1].startsWith('!')) image(parent, restore(label), target);
        else if (source) {
          const button = element('button');
          button.type = 'button';
          button.className = 'markdown-source-link';
          inline(button, label, depth + 1, restore);
          button.addEventListener('click', () => options.onSource(...source));
          parent.appendChild(button);
        }
        else if (/^evidence\.md#[A-Za-z0-9_.:-]+$/.test(target)) {
          const anchor = target.slice('evidence.md#'.length);
          const button = element('button');
          button.type = 'button';
          button.className = 'markdown-evidence-link';
          inline(button, label, depth + 1, restore);
          if (typeof options.onEvidence === 'function') button.addEventListener('click', () => options.onEvidence(anchor));
          else { button.disabled = true; button.title = '证据入口暂不可用'; }
          parent.appendChild(button);
        } else {
          const href = externalLink(target);
          if (href) {
            const link = element('a');
            link.href = href;
            link.target = '_blank';
            link.rel = 'noopener noreferrer';
            inline(link, label, depth + 1, restore);
            parent.appendChild(link);
          } else text(parent, restore(match[0]));
        }
      } else if (match[5]) parent.appendChild(element('code', restore(match[6])));
      else {
        const span = element(match[7] ? 'strong' : 'em');
        inline(span, match[8] || match[10], depth + 1, restore);
        parent.appendChild(span);
      }
      start = tokens.lastIndex;
    }
    text(parent, restore(value.slice(start)));
  }

  const lines = String(markdown ?? '').replace(/\r\n?/g, '\n').split('\n');
  const cells = line => line.trim().replace(/^\|/, '').replace(/\|$/, '').split(/(?<!\\)\|/).map(cell => cell.trim().replace(/\\\|/g, '|'));
  const delimiter = line => line.includes('|') && cells(line).every(cell => /^:?-{3,}:?$/.test(cell));
  const fence = line => /^\s{0,3}(`{3,}|~{3,})(.*)$/.exec(line);
  const heading = line => /^\s{0,3}(#{1,6})\s+(.+)$/.exec(line);
  const list = line => /^\s{0,3}([-+*]|\d+[.)])\s+(.+)$/.exec(line);
  const special = index => !lines[index].trim() || fence(lines[index]) || heading(lines[index]) || list(lines[index]) || /^\s{0,3}>/.test(lines[index]) || (index + 1 < lines.length && delimiter(lines[index + 1]));
  for (let i = 0; i < lines.length;) {
    if (!lines[i].trim()) { i++; continue; }
    const f = fence(lines[i]);
    if (f) {
      const body = [];
      const opening = lines[i++];
      let closed = false;
      while (i < lines.length) {
        const close = fence(lines[i]);
        if (close && close[1][0] === f[1][0] && close[1].length >= f[1].length && !close[2].trim()) { i++; closed = true; break; }
        body.push(lines[i++]);
      }
      const pre = element('pre');
      pre.className = 'markdown-code';
      // Preserve an unmatched fence as text instead of silently dropping it.
      pre.appendChild(element('code', (closed ? body : [opening, ...body]).join('\n')));
      container.appendChild(pre); continue;
    }
    const h = heading(lines[i]);
    if (h) { const node = element(`h${h[1].length}`); inline(node, h[2].replace(/\s+#+\s*$/, '').trimEnd()); container.appendChild(node); i++; continue; }
    if (i + 1 < lines.length && lines[i].includes('|') && delimiter(lines[i + 1])) {
      const header = cells(lines[i]), alignment = cells(lines[i + 1]);
      if (header.length === alignment.length) {
        const wrap = element('div'); wrap.className = 'markdown-table-wrap';
        const table = element('table'), thead = element('thead'), row = element('tr');
        for (const cell of header) { const th = element('th'); th.scope = 'col'; inline(th, cell); row.appendChild(th); }
        thead.appendChild(row); table.appendChild(thead); i += 2;
        const tbody = element('tbody');
        while (i < lines.length && lines[i].trim() && lines[i].includes('|')) {
          const tr = element('tr');
          for (const cell of cells(lines[i++])) { const td = element('td'); inline(td, cell); tr.appendChild(td); }
          tbody.appendChild(tr);
        }
        table.appendChild(tbody); wrap.appendChild(table); container.appendChild(wrap); continue;
      }
    }
    if (/^\s{0,3}>/.test(lines[i])) {
      const quote = element('blockquote');
      while (i < lines.length && /^\s{0,3}>/.test(lines[i])) { const p = element('p'); inline(p, lines[i++].replace(/^\s{0,3}> ?/, '')); quote.appendChild(p); }
      container.appendChild(quote); continue;
    }
    const l = list(lines[i]);
    if (l) {
      const ordered = /^\d/.test(l[1]), ul = element(ordered ? 'ol' : 'ul');
      if (ordered) ul.start = parseInt(l[1], 10);
      while (i < lines.length) {
        const item = list(lines[i]);
        if (!item || /^\d/.test(item[1]) !== ordered) break;
        const li = element('li'); inline(li, item[2]); ul.appendChild(li); i++;
      }
      container.appendChild(ul); continue;
    }
    const paragraph = [lines[i++]];
    while (i < lines.length && !special(i)) paragraph.push(lines[i++]);
    const p = element('p'); p.className = 'markdown-paragraph'; inline(p, paragraph.join('\n')); container.appendChild(p);
  }
  await Promise.all(pending);
}
