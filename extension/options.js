import {DEFAULT_ENDPOINT, endpointOf} from './core.js';
const endpoint = document.querySelector('#endpoint'), token = document.querySelector('#token'), status = document.querySelector('#status');
const stored = await chrome.storage.local.get(['endpoint','token']);
endpoint.value = stored.endpoint || DEFAULT_ENDPOINT;
token.value = stored.token || '';
document.querySelector('#settings-form').addEventListener('submit', async event => {
  event.preventDefault();
  endpoint.removeAttribute('aria-invalid'); token.removeAttribute('aria-invalid');
  let address;
  try { address = endpointOf(endpoint.value.trim()); }
  catch(error) {endpoint.setAttribute('aria-invalid','true'); status.textContent=error.message; endpoint.focus(); return;}
  if (!token.value.trim()) {token.setAttribute('aria-invalid','true'); status.textContent='请填写访问令牌。'; token.focus(); return;}
  try {
    // Called before any await so the permission request retains the user gesture.
    const allowed = await chrome.permissions.request({origins:[address+'/*']});
    if (!allowed) { status.textContent='未获得服务访问权限，设置未保存。'; return; }
    if (stored.endpoint !== address || stored.token !== token.value.trim()) {
      await chrome.storage.local.remove(['lastJob','lastError']);
      await chrome.action.setBadgeText({text:''});
    }
    await chrome.storage.local.set({endpoint:address,token:token.value.trim()});
    if (stored.endpoint && stored.endpoint !== address) await chrome.permissions.remove({origins:[endpointOf(stored.endpoint)+'/*']});
    stored.endpoint = address; stored.token = token.value.trim();
    status.textContent='设置已保存。请返回网页开始采集；尚未验证服务连接。';
  } catch {status.textContent='未能保存设置，请检查浏览器权限。';}
});
