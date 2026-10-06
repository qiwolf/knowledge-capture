import {DEFAULT_ENDPOINT, endpointOf, captureURL, apiRequest} from './core.js';
chrome.runtime.onInstalled.addListener(() => {
  chrome.contextMenus.removeAll(() => chrome.contextMenus.create({id: 'collect-link', title: '添加到知识采集', contexts: ['link']}));
});
async function config() {
  const stored = await chrome.storage.local.get(['endpoint', 'token']);
  const endpoint = endpointOf(stored.endpoint || DEFAULT_ENDPOINT);
  if (!await chrome.permissions.contains({origins: [endpoint + '/*']})) throw new Error('请在设置中授权连接采集服务。');
  return {...stored, endpoint};
}
async function badge(status) {
  await chrome.action.setBadgeText({text: ({queued:'等', running:'…', complete:'✓', partial:'!', failed:'!'})[status] || '!'});
  await chrome.action.setBadgeBackgroundColor({color: '#334155'});
}
async function collect(url, note = '') {
  const validURL = captureURL(url);
  if (typeof note !== 'string' || note.length > 2000) throw new Error('备注不能超过 2000 字。');
  const job = await apiRequest(await config(), 'POST', '/api/captures', {url: validURL, note, origin: 'user'});
  await chrome.storage.local.set({lastJob: job, lastError: ''});
  await badge(job.status);
  return job;
}
async function dispatch(message) {
  if (message?.type === 'collect') return collect(message.url, message.note);
  if (message?.type === 'refresh') {
    const {lastJob} = await chrome.storage.local.get('lastJob');
    if (!lastJob?.id) throw new Error('尚无可刷新的采集任务。');
    const job = await apiRequest(await config(), 'GET', '/api/captures/' + encodeURIComponent(lastJob.id));
    if (job.id !== lastJob.id) throw new Error('服务返回了其他任务的状态。');
    await chrome.storage.local.set({lastJob: job, lastError: ''});
    await badge(job.status);
    return job;
  }
  throw new Error('不支持的操作。');
}
chrome.runtime.onMessage.addListener((message, sender, sendResponse) => {
  if (sender.id !== chrome.runtime.id || !sender.url?.startsWith(chrome.runtime.getURL(''))) return false;
  dispatch(message).then(job => sendResponse({ok: true, job})).catch(error => sendResponse({ok: false, error: error.message}));
  return true;
});
chrome.contextMenus.onClicked.addListener((info) => {
  if (info.menuItemId !== 'collect-link') return;
  collect(info.linkUrl).catch(async error => {
    await chrome.storage.local.set({lastError: error.message});
    await badge('failed');
  });
});
