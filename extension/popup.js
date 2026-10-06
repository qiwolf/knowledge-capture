import {statusText, jobDetails} from './core.js';
const status = document.querySelector('#status');
let currentURL;
function show(job) { status.textContent = [statusText(job), jobDetails(job)].filter(Boolean).join('\n'); document.querySelector('#task-id').textContent = job?.id ? `任务：${job.id}` : ''; }
async function request(message) {
  document.querySelector('main').setAttribute('aria-busy', 'true');
  document.querySelector('#capture').disabled = true;
  document.querySelector('#refresh').disabled = true;
  status.textContent = message.type === 'collect' ? '正在提交链接…' : '正在查询…';
  try {
    const result = await chrome.runtime.sendMessage(message);
    if (!result?.ok) throw new Error(result?.error || '插件服务未响应。');
    show(result.job);
  } catch(error) { status.textContent = error.message; }
  finally {
    document.querySelector('main').removeAttribute('aria-busy');
    document.querySelector('#capture').disabled = !currentURL;
    document.querySelector('#refresh').disabled = false;
  }
}
document.querySelector('#capture-form').addEventListener('submit', event => {event.preventDefault(); request({type:'collect', url:currentURL, note:document.querySelector('#note').value});});
document.querySelector('#refresh').addEventListener('click', () => request({type:'refresh'}));
document.querySelector('#settings').addEventListener('click', () => chrome.runtime.openOptionsPage());
try {
  const [tab] = await chrome.tabs.query({active:true,currentWindow:true});
  currentURL = tab?.url;
  document.querySelector('#current-url').textContent = currentURL || '当前标签没有可读取的链接。';
  document.querySelector('#capture').disabled = !currentURL;
  const {lastJob,lastError} = await chrome.storage.local.get(['lastJob','lastError']);
  show(lastJob);
  if (lastError) status.textContent = lastError;
} catch { status.textContent = '无法读取当前标签或保存的状态。'; }
