export const DEFAULT_ENDPOINT = 'http://127.0.0.1:8765';
export function endpointOf(input) {
  let url;
  try { url = new URL(input); } catch { throw new Error('请输入完整服务地址。'); }
  if (!['https:', 'http:'].includes(url.protocol) || url.username || url.password || url.search || url.hash || url.pathname !== '/') {
    throw new Error('服务地址只填写协议、主机和端口，不含路径或账号密码。');
  }
  if (url.protocol === 'http:' && !['127.0.0.1', 'localhost', '[::1]'].includes(url.hostname)) {
    throw new Error('远程服务必须使用 HTTPS；HTTP 仅用于本机。');
  }
  return url.origin;
}
export function captureURL(input) {
  let url;
  try { url = new URL(input); } catch { throw new Error('这个标签没有可采集的网页链接。'); }
  if (!['http:', 'https:'].includes(url.protocol) || url.username || url.password) {
    throw new Error('只能采集不含账号密码的 HTTP 或 HTTPS 链接。');
  }
  return url.href;
}
function inferenceDetail(item) {
  const states={complete:'完成',failed:'失败',partial:'部分完成',skipped:'未执行',insufficient_sources:'独立资料不足，尚未归纳',running:'正在处理',needs_review:'待检查'};
  const reasons={invalid_citation:'引用校验未通过',invalid_inference:'归纳结果校验未通过',invalid_subtopics:'细分主题校验未通过',inputs_changed:'输入资料已变化',input_too_large:'输入超过限制',already_attempted:'相同输入已有运行记录，需检查后处理',configuration_missing:'模型尚未配置',inference_failed:'归纳未完成'};
  const code=item?.error_code||item?.error?.code;
  return (states[item?.status]||'状态待检查')+(reasons[code]?`（${reasons[code]}）`:'');
}
export function statusText(job) {
  if (['failed','partial','needs_review'].includes(job?.result?.interest_inference?.status)) return '本次处理部分完成，兴趣归纳尚未完成，请查看下方阶段说明。';
  return ({queued: '已接收，等待处理。', running: '正在处理，请稍后刷新状态。', complete: '本次处理完成。', partial: '本次处理部分完成，请查看下方阶段说明。', failed: '采集失败，请检查服务端任务记录后重试。'})[job?.status] || '尚无采集任务。';
}
export function jobDetails(job) {
  const messages = [];
  if (job?.error?.message) messages.push(job.error.message);
  if (job?.error?.code) messages.push(`错误代码：${job.error.code}`);
  if (Array.isArray(job?.result?.warnings)) messages.push(...job.result.warnings);
  const labels = {analysis: 'AI 整理', interest_inference: '兴趣归纳', wiki: 'Wiki 更新', alerts: '关联提醒'};
  const states = {complete: '完成', partial: '部分完成', failed: '失败', skipped: '未执行', needs_review: '待检查'};
  for (const [stage, label] of Object.entries(labels)) {
    const item = job?.result?.[stage];
    if (stage === 'interest_inference' && item) { messages.push(`${label}：${inferenceDetail(item)}`); continue; }
    if (item?.status && states[item.status]) messages.push(`${label}：${states[item.status]}${item.reason ? `（${item.reason}）` : ''}`);
  }
  return messages.join('\n');
}
export async function apiRequest(config, method, path, body, fetcher = fetch) {
  const endpoint = endpointOf(config.endpoint || DEFAULT_ENDPOINT);
  if (typeof config.token !== 'string' || !config.token.trim()) throw new Error('请先在设置中填写服务令牌。');
  let response;
  try {
    response = await fetcher(endpoint + path, {method, redirect: 'error', cache: 'no-store', credentials: 'omit',
      headers: {'Authorization': `Bearer ${config.token}`, 'Content-Type': 'application/json'},
      ...(body ? {body: JSON.stringify(body)} : {}), signal: AbortSignal.timeout(20000)});
  } catch { throw new Error(method === 'POST' ? '未能确认服务是否接收，请先检查任务记录，勿立即重复提交。' : '无法读取任务状态，请检查服务连接。'); }
  if (!response.ok) throw new Error(response.status === 401 || response.status === 403 ? '认证失败，请检查服务令牌。' : `服务返回错误（${response.status}）。`);
  let job;
  try { job = await response.json(); } catch { throw new Error('服务响应格式不正确，请检查任务记录。'); }
  if (!job || typeof job.id !== 'string' || !job.id || !['queued','running','complete','partial','failed'].includes(job.status)
      || (method === 'POST' && response.status !== 202)) {
    throw new Error('服务响应不符合任务格式，请检查任务记录。');
  }
  const safe = {id: job.id, status: job.status};
  if (job.error && typeof job.error === 'object') {
    safe.error = {};
    for (const key of ['code', 'message']) {
      if (typeof job.error[key] === 'string') safe.error[key] = job.error[key].slice(0, key === 'code' ? 100 : 1000);
    }
  }
  if (Array.isArray(job.result?.warnings)) safe.result = {warnings: job.result.warnings.filter(value => typeof value === 'string').slice(0, 10).map(value => value.slice(0, 1000))};
  for (const stage of ['analysis', 'interest_inference', 'wiki', 'alerts']) {
    const item = job.result?.[stage];
    if (item && ['complete', 'partial', 'failed', 'skipped', 'needs_review', 'insufficient_sources', 'running'].includes(item.status)) {
      safe.result ||= {};
      safe.result[stage] = {status: item.status};
      if (stage === 'interest_inference') {
        const code=item.error_code||item.error?.code;
        if (['invalid_citation','invalid_inference','invalid_subtopics','inputs_changed','input_too_large','already_attempted','configuration_missing','inference_failed'].includes(code)) safe.result[stage].error_code=code;
        continue;
      }
      if (typeof item.reason === 'string') safe.result[stage].reason = item.reason.slice(0, 500);
    }
  }
  return safe;
}
