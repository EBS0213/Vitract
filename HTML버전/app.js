/* clipsave: one current download, held in memory only. No requests in file:// mode. */
(() => {
  'use strict';
  const $ = id => document.getElementById(id);
  const FILE_MODE = location.protocol === 'file:';
  const POLL_INTERVAL = 1500;
  const ACTIVE = new Set(['queued', 'downloading', 'converting', 'validating']);
  const STATUS = { queued: 'Preparing', downloading: 'Downloading', converting: 'Converting', validating: 'Checking', completed: 'Ready', cancelled: 'Cancelled', failed: 'Failed' };
  const QUALITY = { 1080: 'Full HD', 720: 'HD', 480: 'Standard', 360: 'Basic' };
  const PLATFORMS = {
    instagram: { label: 'Instagram', placeholder: 'https://www.instagram.com/reel/…' },
    threads: { label: 'Threads', placeholder: 'https://www.threads.com/@user/post/…' },
    x: { label: 'X', placeholder: 'https://x.com/user/status/…' },
    facebook: { label: 'Facebook', placeholder: 'https://www.facebook.com/reel/…' },
    youtube: { label: 'YouTube', placeholder: 'https://www.youtube.com/watch?v=…' }
  };
  const lifetime = new AbortController();
  // Remove the former app's retry links once; clipsave never stores new download data.
  try { localStorage.removeItem('video-saver.retry-links.v1'); } catch { /* Storage access is optional. */ }
  const state = { connection: FILE_MODE ? 'local' : 'connecting', session: null, connectionError: '', refreshing: false,
    metadata: null, inspectedUrl: '', quality: null, inspecting: false, starting: false, titleExpanded: false,
    inspectError: '', startError: '', job: null, jobSource: null, jobError: '', polling: false,
    cancelling: false, retrying: false, resetAfterCancel: false, downloadAttempted: false,
    loginBusy: false, authDismissed: false, destroyed: false, platform: 'youtube' };
  let inspectController = null, inspectGeneration = 0, pollTimer = null, toastTimer = null, refreshPromise = null, lastMetadata = null;
  const on = (element, event, handler) => element.addEventListener(event, handler, { signal: lifetime.signal });
  const text = (element, value) => { const next = String(value ?? ''); if (element.textContent !== next) element.textContent = next; };
  const show = (element, visible) => { element.hidden = !visible; };
  const announce = message => text($('announcer'), message);
  const isActive = () => !!state.job && ACTIVE.has(state.job.status);
  const authenticated = () => !FILE_MODE && !!state.session?.authenticated;
  const ready = () => authenticated() && state.connection === 'online' && state.session?.readiness?.ready === true;
  const platformAvailable = () => Array.isArray(state.session?.platforms)
    ? state.session.platforms.some(item => item.id === state.platform && item.available === true)
    : state.platform === 'youtube';
  function toast(message) { text($('toast-text'), message); show($('toast'), true); clearTimeout(toastTimer); toastTimer = setTimeout(() => show($('toast'), false), 6000); announce(message); }
  function duration(value) {
    if (!Number.isFinite(value) || value <= 0) return '—';
    const seconds = Math.floor(value), minutes = Math.floor(seconds / 60), hours = Math.floor(minutes / 60);
    return hours ? `${hours}:${String(minutes % 60).padStart(2, '0')}:${String(seconds % 60).padStart(2, '0')}` : `${minutes}:${String(seconds % 60).padStart(2, '0')}`;
  }
  function bytes(value) {
    if (!Number.isFinite(value) || value <= 0) return '';
    const index = Math.min(3, Math.floor(Math.log(value) / Math.log(1000)));
    return `${(value / 1000 ** index).toLocaleString('en-US', { maximumFractionDigits: index ? 1 : 0 })} ${['B', 'KB', 'MB', 'GB'][index]}`;
  }
  function httpUrl(value) { try { const url = new URL(value); return ['http:', 'https:'].includes(url.protocol) ? url.href : null; } catch { return null; } }
  function fileUrl(job) {
    if (FILE_MODE || job?.status !== 'completed') return null;
    try {
      const path = `/api/jobs/${encodeURIComponent(job.id)}/file`, url = new URL(job.download_url || path, location.href);
      return url.origin === location.origin && ['http:', 'https:'].includes(url.protocol) && url.pathname === path ? url.href : null;
    } catch { return null; }
  }
  class ApiError extends Error { constructor(message, status = 0, code = '') { super(message); this.name = 'ApiError'; this.status = status; this.code = code; } }
  async function api(path, { method = 'GET', body, signal, timeout = 30000 } = {}) {
    if (FILE_MODE) throw new ApiError('Start the web server.');
    if (state.destroyed) throw new DOMException('Closed', 'AbortError');
    if (method === 'POST' && !state.session?.csrf_token) throw new ApiError('Reconnect and retry.', 403, 'SESSION_REQUIRED');
    const controller = new AbortController(), abort = () => controller.abort();
    lifetime.signal.addEventListener('abort', abort, { once: true }); signal?.addEventListener('abort', abort, { once: true });
    if (signal?.aborted || lifetime.signal.aborted) controller.abort();
    let timedOut = false;
    const timer = setTimeout(() => { timedOut = true; controller.abort(); }, timeout);
    const headers = { Accept: 'application/json' };
    if (method === 'POST') { headers['Content-Type'] = 'application/json'; headers['X-CSRF-Token'] = state.session.csrf_token; }
    try {
      const response = await fetch(path, { method, credentials: 'same-origin', cache: 'no-store', headers, signal: controller.signal, ...(method === 'POST' ? { body: JSON.stringify(body || {}) } : {}) });
      state.connection = 'online'; state.connectionError = '';
      let result;
      try { result = await response.json(); } catch { throw new ApiError('Invalid response. Retry.', response.status, 'INVALID_RESPONSE'); }
      if (!response.ok) throw new ApiError(typeof result.error === 'string' ? result.error : 'Request failed. Retry.', response.status, result.code || '');
      return result;
    } catch (error) {
      if (error.name === 'AbortError' && !timedOut) throw error;
      if (error instanceof ApiError) throw error;
      state.connection = 'offline'; state.connectionError = timedOut ? 'Request timed out.' : 'Connection failed.';
      throw new ApiError(state.connectionError, 0, timedOut ? 'TIMEOUT' : 'NETWORK_ERROR');
    } finally {
      clearTimeout(timer); lifetime.signal.removeEventListener('abort', abort); signal?.removeEventListener('abort', abort); renderConnection(); renderForm();
    }
  }
  function applySession(session) {
    if (!session || typeof session.authenticated !== 'boolean' || typeof session.csrf_token !== 'string') throw new ApiError('Session unavailable. Reconnect.');
    state.session = session; state.connection = 'online'; state.connectionError = '';
    if (session.authenticated && $('login-dialog').open) $('login-dialog').close();
    if (!session.authenticated) { clearTimeout(pollTimer); invalidateMetadata(); }
    renderConnection(); renderForm(); renderJob();
    if (session.requires_password && !session.authenticated && !state.authDismissed) openLogin();
  }
  async function recoverAuth(error) {
    if (error.status !== 401 && !(error.status === 403 && ['invalid_csrf', 'CSRF_FAILED', 'CSRF_INVALID', 'SESSION_REQUIRED'].includes(error.code))) return;
    state.authDismissed = false;
    try {
      const session = await api('/api/session', { timeout: 12000 });
      invalidateMetadata(); applySession(session);
      if (session.authenticated) toast('Session refreshed. Inspect again.');
    } catch { /* The connection notice remains visible. */ }
  }
  async function refresh() {
    if (FILE_MODE || state.destroyed) return;
    if (refreshPromise) return refreshPromise;
    state.refreshing = true; renderConnection();
    refreshPromise = (async () => {
      try {
        applySession(await api('/api/session', { timeout: 12000 }));
        if (authenticated() && state.job && (isActive() || state.jobError)) await pollJob();
      } catch (error) { if (error.name !== 'AbortError') { state.connectionError = error.message; state.connection = 'offline'; } }
      finally { state.refreshing = false; refreshPromise = null; renderConnection(); renderForm(); renderJob(); schedulePoll(); }
    })();
    return refreshPromise;
  }
  function schedulePoll() {
    clearTimeout(pollTimer); pollTimer = null;
    if (!state.destroyed && authenticated() && isActive()) pollTimer = setTimeout(pollJob, POLL_INTERVAL);
  }
  function acceptJob(job) {
    if (!job || typeof job.id !== 'string' || !Object.hasOwn(STATUS, job.status)) throw new ApiError('Status unavailable. Reconnect.');
    if (state.job && state.job.id !== job.id) throw new ApiError('Unexpected response.');
    if (state.job && !ACTIVE.has(state.job.status) && ACTIVE.has(job.status)) return;
    const previous = state.job?.status;
    state.job = job; state.jobError = '';
    renderForm(); renderJob();
    if (state.resetAfterCancel && !ACTIVE.has(job.status)) { resetNow(); return; }
    if (previous !== job.status) announce(STATUS[job.status]);
    if (job.status === 'completed') attemptDownload();
  }
  function expireJob(error, id) {
    if (state.job?.id !== id || error.status !== 404 || !['job_not_found', 'job_expired', 'not_found'].includes(error.code)) return false;
    clearTimeout(pollTimer); pollTimer = null;
    acceptJob({ ...state.job, status: 'failed', progress: null, stage_message: '',
      error: 'Download expired. Retry.' });
    return true;
  }
  async function pollJob() {
    clearTimeout(pollTimer); pollTimer = null;
    if (!authenticated() || !state.job || state.polling || state.destroyed) { schedulePoll(); return; }
    const id = state.job.id; state.polling = true;
    try { const job = await api(`/api/jobs/${encodeURIComponent(id)}`, { timeout: 15000 }); if (state.job?.id === id) acceptJob(job); }
    catch (error) { if (error.name !== 'AbortError' && state.job?.id === id && !expireJob(error, id)) { state.jobError = error.message; await recoverAuth(error); } }
    finally { state.polling = false; renderJob(); schedulePoll(); }
  }
  function invalidateMetadata() {
    inspectGeneration++; inspectController?.abort(); inspectController = null;
    state.inspecting = false; state.metadata = null; state.inspectedUrl = ''; state.quality = null;
    state.titleExpanded = false; state.inspectError = ''; state.startError = ''; lastMetadata = null;
  }
  function renderConnection() {
    const connecting = state.refreshing || state.connection === 'connecting';
    $('connection-status').className = `connection-status is-${connecting ? 'connecting' : state.connection}`;
    text($('connection-label'), FILE_MODE ? 'Local preview' : connecting ? 'Connecting' : state.connection === 'online' ? 'Connected' : 'Offline');
    let title = '', description = '', action = 'Reconnect';
    if (FILE_MODE) { title = 'Start clipsave'; description = 'Run the web launcher, then open localhost.'; }
    else if (state.connection === 'offline') { title = 'Connection lost'; description = state.connectionError + (isActive() ? ' Reconnect to check status.' : ''); }
    else if (state.session?.requires_password && !state.session.authenticated) { title = 'Password required'; description = ''; action = 'Enter password'; }
    else if (state.session?.authenticated && state.session.readiness?.ready === false) {
      title = 'Server not ready'; action = 'Retry';
      const missing = Array.isArray(state.session.readiness.missing_tools) ? state.session.readiness.missing_tools.join(', ') : '';
      description = missing ? `Missing: ${missing}.` : 'Try again later.';
    } else if (state.session && !state.session.authenticated) { title = 'Session unavailable'; description = 'Reconnect.'; }
    text($('notice-title'), title); text($('notice-description'), description); show($('notice-description'), !!description); show($('connection-notice'), !!title);
    text($('reconnect-button'), state.refreshing ? 'Checking…' : action); $('reconnect-button').disabled = state.refreshing;
    show($('reconnect-button'), !FILE_MODE); show($('local-app-link'), FILE_MODE);
  }
  function renderForm() {
    const locked = state.starting || isActive() || state.cancelling || state.resetAfterCancel;
    for (const id of Object.keys(PLATFORMS)) {
      $(`platform-${id}`).setAttribute('aria-pressed', String(state.platform === id));
      $(`platform-${id}`).disabled = locked || state.retrying;
    }
    $('video-url').placeholder = PLATFORMS[state.platform].placeholder;
    const unavailable = !FILE_MODE && !!state.session && !platformAvailable();
    show($('platform-status'), unavailable);
    text($('platform-status'), unavailable ? `${PLATFORMS[state.platform].label} unavailable.` : '');
    $('video-url').disabled = FILE_MODE || locked; $('paste-button').disabled = FILE_MODE || locked;
    $('inspect-button').disabled = !ready() || !platformAvailable() || locked || state.inspecting || !$('video-url').value.trim();
    $('inspect-button').classList.toggle('is-loading', state.inspecting); text($('inspect-button-text'), state.inspecting ? 'Inspecting' : 'Inspect');
    $('video-url').setAttribute('aria-invalid', String(!!state.inspectError));
    show($('inspect-error'), !!state.inspectError); text($('inspect-error'), state.inspectError);
    show($('start-error'), !!state.startError); text($('start-error'), state.startError);
    const metadata = state.metadata;
    show($('video-empty'), !metadata); show($('video-card'), !!metadata);
    $('video-empty').classList.toggle('is-loading', state.inspecting);
    text($('empty-title'), state.inspecting ? 'Inspecting…' : 'Add a video.');
    if (metadata && metadata !== lastMetadata) {
      text($('video-title'), metadata.title || 'Untitled'); $('video-title').title = metadata.title || '';
      text($('video-channel'), metadata.channel || 'Unknown channel'); text($('video-duration'), duration(metadata.duration));
      show($('title-toggle'), (metadata.title || '').length > 24);
      const thumbnail = httpUrl(metadata.thumbnail);
      show($('thumbnail-image'), !!thumbnail); show($('thumbnail-fallback'), !thumbnail);
      if (thumbnail) $('thumbnail-image').src = thumbnail; else $('thumbnail-image').removeAttribute('src');
      $('thumbnail-image').alt = metadata.title ? `Preview: ${metadata.title}` : 'Video preview';
      $('quality-select').replaceChildren();
      for (const quality of metadata.qualities) { const option = document.createElement('option'); option.value = String(quality); option.textContent = `${quality}p${QUALITY[quality] ? ` · ${QUALITY[quality]}` : ''}`; $('quality-select').append(option); }
      if (!metadata.qualities.length) { const option = document.createElement('option'); option.textContent = 'Unavailable'; option.value = ''; $('quality-select').append(option); }
    } else if (!metadata) { $('quality-select').replaceChildren(); $('thumbnail-image').removeAttribute('src'); }
    lastMetadata = metadata;
    $('video-title').classList.toggle('is-expanded', state.titleExpanded); $('title-toggle').setAttribute('aria-expanded', String(state.titleExpanded)); text($('title-toggle'), state.titleExpanded ? 'Less' : 'More');
    $('quality-select').value = state.quality == null ? '' : String(state.quality);
    $('quality-select').disabled = !ready() || !metadata || locked || !metadata.qualities.length;
    const hasJob = !!state.job;
    $('save-button').disabled = !ready() || !metadata || !state.quality || locked || state.inspecting || hasJob;
    $('save-button').classList.toggle('is-loading', state.starting);
    text($('save-button-text'), state.starting ? 'Starting…' : hasJob && state.job.status === 'completed' ? 'Ready' : hasJob && isActive() ? 'Preparing' : 'Download');
  }
  function renderJob() {
    const job = state.job;
    show($('job-panel'), !!job || state.starting);
    if (!job && !state.starting) return;
    const status = job?.status || 'queued', active = ACTIVE.has(status), complete = status === 'completed';
    $('job-panel').dataset.status = status;
    text($('job-heading'), state.starting && !job ? 'Starting' : state.resetAfterCancel ? 'Cancelling' : state.cancelling ? 'Cancelling' : STATUS[status]);
    $('job-icon-use').setAttribute('href', complete ? '#i-check-circle' : status === 'failed' ? '#i-info' : status === 'cancelled' ? '#i-close' : '#i-download');
    const percent = typeof job?.progress === 'number' && Number.isFinite(job.progress) ? Math.max(0, Math.min(100, job.progress)) : null;
    text($('job-percent'), active && percent !== null ? `${Math.round(percent)}%` : '');
    show($('job-progress'), active);
    if (percent === null) $('download-progress').removeAttribute('value'); else $('download-progress').value = percent;
    $('download-progress').setAttribute('aria-valuetext', percent === null ? STATUS[status] : `${Math.round(percent)} percent`);
    text($('job-stage'), job?.stage_message || 'Preparing');
    show($('job-file'), complete && !!job.filename); text($('job-file'), [job?.filename, bytes(job?.bytes)].filter(Boolean).join(' · '));
    show($('job-error'), status === 'failed'); text($('job-error'), job?.error || 'Download failed.');
    show($('job-network-error'), !!state.jobError); text($('job-network-error'), state.jobError);
    const link = fileUrl(job); show($('manual-download'), !!link);
    if (link) { $('manual-download').href = link; $('manual-download').download = job.filename || ''; } else $('manual-download').removeAttribute('href');
    show($('cancel-button'), !!job && active); $('cancel-button').disabled = state.cancelling || state.resetAfterCancel || !authenticated(); text($('cancel-button'), state.cancelling || state.resetAfterCancel ? 'Cancelling' : 'Cancel');
    show($('retry-button'), ['failed', 'cancelled'].includes(status)); $('retry-button').disabled = !ready() || state.retrying || state.starting || state.inspecting; text($('retry-button'), state.retrying ? 'Retrying…' : 'Retry');
    show($('check-job-button'), !!state.jobError); $('check-job-button').disabled = state.refreshing || state.polling;
    $('next-button').disabled = state.starting || state.cancelling || state.retrying || state.resetAfterCancel;
  }
  async function inspect(url, { preferredQuality = 720, autoStart = false } = {}) {
    const entered = url.trim();
    if (!ready() || !platformAvailable() || state.starting || isActive()) return;
    const platform = state.platform;
    if (!httpUrl(entered)) { state.inspectError = 'Enter a valid video URL.'; renderForm(); $('video-url').focus(); return; }
    clearTimeout(pollTimer); state.job = null; state.jobError = ''; state.jobSource = null; state.downloadAttempted = false;
    invalidateMetadata(); const generation = inspectGeneration;
    inspectController = new AbortController(); state.inspecting = true; renderForm(); renderJob(); announce('Inspecting.');
    try {
      const result = await api('/api/inspect', { method: 'POST', body: { url: entered, platform }, signal: inspectController.signal, timeout: 120000 });
      if (generation !== inspectGeneration || $('video-url').value.trim() !== entered || state.platform !== platform) return;
      if (!result || typeof result.metadata_token !== 'string' || !Array.isArray(result.qualities)) throw new ApiError('Video unavailable. Check the URL.');
      if (result.platform && result.platform !== platform) throw new ApiError('Platform mismatch. Inspect again.');
      const qualities = [...new Set(result.qualities.map(Number).filter(value => [1080, 720, 480, 360].includes(value)))].sort((a, b) => b - a);
      state.metadata = { ...result, title: String(result.title || ''), channel: String(result.channel || ''), qualities };
      state.inspectedUrl = entered; state.quality = qualities.includes(preferredQuality) ? preferredQuality : qualities[0] || null;
      state.inspectError = ''; announce(qualities.length ? 'Video ready.' : 'No available quality.');
    } catch (error) { if (error.name !== 'AbortError' && generation === inspectGeneration) { state.inspectError = error.message; await recoverAuth(error); } }
    finally { if (generation === inspectGeneration) { state.inspecting = false; inspectController = null; renderForm(); } }
    if (autoStart && generation === inspectGeneration && state.metadata && state.quality) await startJob();
  }
  async function startJob() {
    if (!ready() || !state.metadata || !state.quality || state.starting || state.inspecting || state.job) return;
    if ($('video-url').value.trim() !== state.inspectedUrl) { invalidateMetadata(); renderForm(); return; }
    const source = { url: state.inspectedUrl, quality: state.quality, platform: state.platform }, token = state.metadata.metadata_token;
    state.starting = true; state.startError = ''; state.downloadAttempted = false; renderForm(); renderJob();
    try {
      const job = await api('/api/jobs', { method: 'POST', body: { metadata_token: token, quality: source.quality } });
      state.jobSource = source; acceptJob(job); schedulePoll();
    } catch (error) {
      if (error.name !== 'AbortError') { state.startError = error.status === 0 ? `${error.message} Request status unknown.` : error.message; await recoverAuth(error); }
    } finally { state.starting = false; renderForm(); renderJob(); }
  }
  function attemptDownload() {
    const link = fileUrl(state.job);
    if (!link || state.downloadAttempted || state.resetAfterCancel) return;
    state.downloadAttempted = true;
    const anchor = document.createElement('a'); anchor.href = link; anchor.download = state.job.filename || ''; anchor.hidden = true;
    document.body.append(anchor);
    try { anchor.click(); announce('Download ready.'); }
    catch { toast('Click Download to save.'); }
    finally { anchor.remove(); }
  }
  async function cancelJob({ reset = false } = {}) {
    if (!state.job || !isActive() || state.cancelling || !authenticated()) return false;
    const id = state.job.id; state.cancelling = true; state.resetAfterCancel = reset; renderForm(); renderJob();
    try { const job = await api(`/api/jobs/${encodeURIComponent(id)}/cancel`, { method: 'POST', body: {} }); if (state.job?.id === id) acceptJob(job); return true; }
    catch (error) {
      if (error.name !== 'AbortError' && expireJob(error, id)) return true;
      state.resetAfterCancel = false;
      if (error.name !== 'AbortError') { state.jobError = error.message; await recoverAuth(error); }
      return false;
    }
    finally { state.cancelling = false; renderForm(); renderJob(); schedulePoll(); if (reset && !state.job) $('video-url').focus(); }
  }
  async function retryJob() {
    if (!ready() || !state.job || !['failed', 'cancelled'].includes(state.job.status) || state.retrying) return;
    const source = state.jobSource;
    if (!source) { resetNow(); toast('Enter the video URL.'); return; }
    state.retrying = true; state.platform = source.platform || 'youtube'; $('video-url').value = source.url; renderJob();
    try { await inspect(source.url, { preferredQuality: source.quality, autoStart: true }); }
    finally { state.retrying = false; renderForm(); renderJob(); }
  }
  function resetNow() {
    clearTimeout(pollTimer); pollTimer = null; invalidateMetadata();
    state.job = null; state.jobSource = null; state.jobError = ''; state.resetAfterCancel = false; state.downloadAttempted = false;
    $('video-url').value = ''; renderForm(); renderJob(); $('video-url').focus(); announce('Ready for the next video.');
  }
  async function nextVideo() {
    if (state.starting || state.cancelling || state.retrying || state.resetAfterCancel) return;
    if (isActive()) {
      if (!window.confirm('Cancel and start over?')) return;
      await cancelJob({ reset: true });
    } else resetNow();
  }
  function openLogin() {
    if (FILE_MODE || !state.session?.requires_password || state.session.authenticated) return;
    state.authDismissed = false; show($('login-error'), false);
    if (!$('login-dialog').open) $('login-dialog').showModal(); $('login-password').focus();
  }
  async function login(event) {
    event.preventDefault(); if (state.loginBusy) return;
    const password = $('login-password').value;
    if (!password) { text($('login-error'), 'Enter a password.'); show($('login-error'), true); $('login-password').focus(); return; }
    state.loginBusy = true; $('login-submit').disabled = true; $('login-submit').classList.add('is-loading'); show($('login-error'), false);
    try {
      const session = await api('/api/login', { method: 'POST', body: { password } });
      if (!session.authenticated) throw new ApiError('Check the password.');
      $('login-password').value = ''; applySession(session); if (state.job) await pollJob(); $('video-url').focus();
    } catch (error) { if (error.name !== 'AbortError') { await recoverAuth(error); text($('login-error'), error.code === 'invalid_csrf' ? 'Session refreshed. Retry.' : error.message); show($('login-error'), true); $('login-password').focus(); } }
    finally { state.loginBusy = false; $('login-submit').disabled = false; $('login-submit').classList.remove('is-loading'); }
  }
  on($('inspect-form'), 'submit', event => { event.preventDefault(); inspect($('video-url').value); });
  for (const platform of Object.keys(PLATFORMS)) on($(`platform-${platform}`), 'click', () => {
    if (state.platform === platform || state.starting || isActive() || state.cancelling || state.retrying || state.resetAfterCancel) return;
    clearTimeout(pollTimer); pollTimer = null; invalidateMetadata();
    state.platform = platform; state.job = null; state.jobSource = null; state.jobError = ''; state.downloadAttempted = false;
    renderForm(); renderJob(); announce(PLATFORMS[platform].label);
  });
  on($('video-url'), 'input', () => { if (state.metadata || state.inspecting || state.inspectError) invalidateMetadata(); state.startError = ''; if (state.job && !isActive()) { state.job = null; state.jobSource = null; state.jobError = ''; state.downloadAttempted = false; renderJob(); } renderForm(); });
  on($('paste-button'), 'click', async () => {
    try {
      if (!navigator.clipboard?.readText) throw new Error(); const value = await navigator.clipboard.readText();
      if (state.starting || isActive()) return;
      if (value.trim()) { invalidateMetadata(); state.job = null; state.jobSource = null; state.jobError = ''; $('video-url').value = value.trim(); renderForm(); renderJob(); $('video-url').focus(); }
      else toast('Clipboard is empty.');
    } catch { $('video-url').focus(); toast('Paste the URL manually.'); }
  });
  on($('quality-select'), 'change', () => { state.quality = Number($('quality-select').value) || null; renderForm(); });
  on($('save-button'), 'click', startJob);
  on($('title-toggle'), 'click', () => { state.titleExpanded = !state.titleExpanded; renderForm(); });
  on($('thumbnail-image'), 'error', () => { show($('thumbnail-image'), false); show($('thumbnail-fallback'), true); });
  on($('reconnect-button'), 'click', () => { if (state.session?.requires_password && !state.session.authenticated && state.connection === 'online') openLogin(); else refresh(); });
  on($('cancel-button'), 'click', () => cancelJob()); on($('retry-button'), 'click', retryJob); on($('next-button'), 'click', nextVideo); on($('check-job-button'), 'click', refresh);
  on(document.querySelector('.brand'), 'click', event => { event.preventDefault(); nextVideo(); });
  on($('toast-close'), 'click', () => show($('toast'), false));
  on($('login-form'), 'submit', login);
  on($('login-close'), 'click', () => { state.authDismissed = true; $('login-password').value = ''; $('login-dialog').close(); });
  on($('login-dialog'), 'cancel', () => { state.authDismissed = true; $('login-password').value = ''; });
  on(window, 'online', refresh);
  on(window, 'offline', () => { if (!FILE_MODE) { state.connection = 'offline'; state.connectionError = 'You are offline.'; renderConnection(); renderForm(); renderJob(); } });
  on(window, 'focus', () => { if (!FILE_MODE && !state.loginBusy && (state.connection !== 'online' || isActive())) refresh(); });
  on(document, 'visibilitychange', () => { if (!document.hidden && !FILE_MODE && (state.connection !== 'online' || isActive())) refresh(); });
  on(window, 'beforeunload', event => { if (isActive() || state.starting) { event.preventDefault(); event.returnValue = ''; } });
  window.ClipSaveApp = Object.freeze({ version: '2.1.0', config: Object.freeze({ pollIntervalMs: POLL_INTERVAL, apiBase: '/api', fileMode: FILE_MODE, advertisingConnected: false }),
    getState: () => ({ connection: state.connection, authenticated: !!state.session?.authenticated, requiresPassword: !!state.session?.requires_password,
      ready: state.session?.readiness?.ready === true, platform: state.platform, platformAvailable: platformAvailable(), inspecting: state.inspecting, starting: state.starting, cancelling: state.cancelling,
      metadata: state.metadata ? { id: state.metadata.id, title: state.metadata.title, qualities: [...state.metadata.qualities] } : null,
      quality: state.quality, currentJob: state.job ? { ...state.job } : null, downloadAttempted: state.downloadAttempted, jobError: state.jobError }),
    refresh, destroy: () => { state.destroyed = true; clearTimeout(pollTimer); clearTimeout(toastTimer); inspectController?.abort(); lifetime.abort(); }
  });
  renderConnection(); renderForm(); renderJob();
  if (!FILE_MODE) refresh();
})();
