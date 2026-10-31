const el = (id) => document.getElementById(id);
let state = null;
let actionBusy = false;
let sending = false;
const messages = {
  policy_denied: 'Blocked by policy. No request was sent. Refresh models if your selection expired.',
  operation_failed: 'The operation could not complete. Check the connection and try again explicitly.',
  owner_session_required: 'This management session expired. Reload the page.',
  owner_action_required: 'This management session expired. Reload the page.',
  busy: 'Another operation is running. Wait for it or cancel the active test.',
  connected: 'Connection saved. Refresh models to see your choices.',
  disconnected: 'Disconnected. New requests are blocked.',
  connection_failed: 'Sign-in did not complete. Your previous connection was preserved.',
  permission_missing: 'Signed in without plan permission. Choose Grant plan permission to continue.',
  revocation_unconfirmed: 'Disconnected locally. Remote revocation was not confirmed; disconnect this app in ChatGPT Settings.',
  interrupted: 'Request cancelled. OpenAI may already have processed part of it.',
  upstream_failed: 'The request failed. It has not been retried.',
  invalid_stream: 'OpenAI returned an unsupported stream. It has not been retried.',
  output_limit: 'The local output limit was reached. This is not a usage cap.',
  incomplete: 'The response was incomplete. It has not been retried.'
};
function notice(text, error = false) {
  el('notice').textContent = text;
  el('notice').hidden = !text;
  el('notice').classList.toggle('error', error);
}
function controls() {
  const busy = actionBusy || sending || Boolean(state?.busy);
  el('connect').disabled = busy || !state;
  el('disconnect').disabled = actionBusy || !state;
  el('refresh').disabled = busy || !state || !['authorized', 'expired'].includes(state.connection);
  el('send').disabled = busy || !state?.inference_enabled || !el('model').value;
  el('cancel').disabled = !(sending || state?.busy);
  for (const id of ['model', 'prompt', 'processing', 'usage']) el(id).disabled = busy || (id === 'model' && !el('model').options[0]?.value);
}
function render(value) {
  const previousNotice = state?.notice;
  state = value;
  const labels = {authorized: 'Connected', disconnected: 'Disconnected', identity_only: 'Plan permission needed', expired: 'Session expired'};
  el('connection-label').textContent = value.connecting ? 'Waiting for sign-in…' : labels[value.connection];
  el('connection-badge').classList.toggle('connected', value.connection === 'authorized');
  el('connection-detail').textContent = ({authorized: 'ChatGPT account connected', disconnected: 'Connect your ChatGPT account to get started.', identity_only: 'Sign-in succeeded. Plan usage permission is missing.', expired: 'Refresh models to renew this session, or reconnect.'})[value.connection];
  el('connect').textContent = value.connection === 'identity_only' ? 'Grant plan permission' : value.connection === 'disconnected' ? 'Connect with ChatGPT' : 'Reconnect';
  el('disconnect').hidden = value.connection === 'disconnected';
  el('inference-off').hidden = value.inference_enabled;
  const picker = el('model');
  const selected = picker.value;
  const options = value.models.length ? value.models : [{slug: '', title: 'Refresh models to see your choices'}];
  if (JSON.stringify([...picker.options].map((x) => [x.value, x.textContent])) !== JSON.stringify(options.map((x) => [x.slug, x.title]))) {
    picker.replaceChildren(...options.map((model) => {const option = document.createElement('option'); option.value = model.slug; option.textContent = model.title; return option;}));
    if (options.some((x) => x.slug === selected)) picker.value = selected;
  }
  if (value.notice && value.notice !== previousNotice) notice(messages[value.notice] || 'Connection updated.', ['revocation_unconfirmed','permission_missing','connection_failed'].includes(value.notice));
  controls();
}
async function refreshState() {
  const response = await fetch('/api/state', {cache: 'no-store'});
  const value = await response.json();
  if (!response.ok) throw new Error(value.error);
  render(value);
}
async function post(path, body = {}) {
  const response = await fetch(path, {method: 'POST', headers: {'Content-Type': 'application/json', 'X-CSRF-Token': state.csrf}, body: JSON.stringify(body), cache: 'no-store'});
  if (!response.ok) {const value = await response.json(); throw new Error(value.error);}
  return response;
}
async function action(callback) {
  actionBusy = true; controls(); notice('');
  try {await callback(); await refreshState();}
  catch (error) {notice(messages[error.message] || messages.operation_failed, true);}
  finally {actionBusy = false; controls();}
}
el('connect').addEventListener('click', () => {
  // Open on the trusted click so async consent preparation cannot be blocked
  // as a popup. The one-use launch URL is never written into browser storage.
  const popup = window.open('about:blank', '_blank');
  if (!popup) {notice('Allow a sign-in tab for this page, then try Connect again.', true); return;}
  popup.opener = null;
  void action(async () => {
    try {const response = await post('/api/connect', {request_plan_consent: state.connection === 'identity_only'}); const value = await response.json(); popup.location.replace(value.launch_url);}
    catch (error) {popup.close(); throw error;}
  });
});
el('disconnect').addEventListener('click', () => {el('disconnect-confirmation').hidden = false; el('keep-connected').focus();});
el('keep-connected').addEventListener('click', () => {el('disconnect-confirmation').hidden = true; el('disconnect').focus();});
el('confirm-disconnect').addEventListener('click', () => void action(async () => {await post('/api/disconnect'); el('disconnect-confirmation').hidden = true;}));
el('refresh').addEventListener('click', () => void action(async () => {render(await (await post('/api/models')).json());}));
function policyHint() {
  el('policy-hint').textContent = el('processing').value === 'local_only' ? 'Local-only policy blocks this OpenAI request before anything is sent.' : el('usage').value === 'included_only' ? 'An included-usage-only guarantee is unavailable. This request will be blocked.' : 'This sends your prompt to OpenAI. Usage follows your ChatGPT app settings.';
}
el('processing').addEventListener('change', policyHint);
el('usage').addEventListener('change', policyHint);
el('cancel').addEventListener('click', () => void action(async () => {await post('/api/cancel');}));
el('test-form').addEventListener('submit', async (event) => {
  event.preventDefault();
  if (sending || !state || el('send').disabled) return;
  sending = true; controls(); notice('');
  const output = el('response'); output.textContent = ''; output.classList.add('has-response');
  try {
    const response = await post('/api/generate', {prompt: el('prompt').value, model: el('model').value, catalog_revision: state.catalog_revision, processing: el('processing').value, usage: el('usage').value});
    const reader = response.body.getReader(); const decoder = new TextDecoder('utf-8', {fatal: true}); let pending = ''; let terminal = false;
    try {
      while (true) {
        const chunk = await reader.read(); pending += decoder.decode(chunk.value, {stream: !chunk.done});
        if (pending.length > 65536) throw new Error('invalid_stream');
        let index;
        while ((index = pending.indexOf('\n')) >= 0) {
          const item = JSON.parse(pending.slice(0, index)); pending = pending.slice(index + 1);
          if (terminal) throw new Error('invalid_stream');
          if (item.kind === 'text' && typeof item.text === 'string') output.textContent += item.text;
          else if (item.kind === 'completed') {terminal = true; notice('Request complete.');}
          else if (item.kind === 'failed') {terminal = true; notice(messages[item.error] || messages.upstream_failed, true);}
          else throw new Error('invalid_stream');
          if (output.textContent.length > 4096) throw new Error('invalid_stream');
        }
        if (chunk.done) {if (!terminal || pending) throw new Error('invalid_stream'); break;}
      }
    } finally {await reader.cancel(); reader.releaseLock();}
  } catch (error) {notice(messages[error.message] || messages.upstream_failed, true);}
  finally {
    if (!output.textContent) {output.textContent = 'No response text.'; output.classList.remove('has-response');}
    sending = false;
    try {await refreshState();} catch {notice(messages.operation_failed, true);}
    controls();
  }
});
async function poll() {
  try {await refreshState();} catch {notice('This capability is not answering. Reload after it restarts.', true);}
  window.setTimeout(poll, 3000);
}
void poll();
