// Exercise the actual browser renderer with a minimal, non-HTML-parsing DOM.
import {readFileSync} from 'node:fs';
const source = readFileSync(new URL('../knowledge_capture/web/renderer.js', import.meta.url), 'utf8');
const {renderMarkdown} = await import('data:text/javascript;base64,' + Buffer.from(source).toString('base64'));
class Node {
  constructor(tag, value=''){this.tagName=tag;this.value=value;this.children=[];this.ownerDocument=doc;}
  set textContent(value){this.value=value;this.children=[];}
  get textContent(){return this.value+this.children.map(c=>c.textContent).join('');}
  appendChild(child){this.children.push(child);return child;}
  replaceChildren(){this.children=[];this.value='';}
  addEventListener(){}
}
const doc={createElement:tag=>new Node(tag),createTextNode:text=>new Node('#text',text)};
const root=new Node('div');
await renderMarkdown(readFileSync(0,'utf8'),root);
const codes=root.children.filter(n=>n.tagName==='pre').map(n=>n.textContent);
const prose=root.children.filter(n=>n.tagName!=='pre').map(n=>n.textContent).join('\n');
process.stdout.write(JSON.stringify({codes,prose}));
