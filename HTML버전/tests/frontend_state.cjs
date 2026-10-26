const fs = require('fs');
const vm = require('vm');
const assert = require('assert/strict');
const file = require('path').resolve(__dirname, '../app.js');
const source = fs.readFileSync(file, 'utf8').replace('  renderConnection(); renderForm(); renderJob();\n  if (!FILE_MODE) refresh();', '  window.__test = { state, inspect, startJob, pollJob, cancelJob, nextVideo, resetNow, retryJob, login, recoverAuth };\n  renderConnection(); renderForm(); renderJob();\n  if (!FILE_MODE) refresh();');
function environment(fileMode = false, password = false) {
  const calls = [], clicks = [], timers = new Map(), nodes = new Map(); let inspectPause = null, jobPause = null; let timer = 0, token = 'csrf-1', jobId = 0, stage = 'queued', csrfFail = false, missing = false, confirmResult = true;
  function node() { return { hidden: false, disabled: false, textContent: '', value: '', open: false, children: [], dataset: {}, handlers: {}, classList: { toggle() {}, add() {}, remove() {} }, append(...items) { this.children.push(...items); }, replaceChildren(...items) { this.children = items; }, setAttribute(k,v) { this[k] = v; }, removeAttribute(k) { delete this[k]; }, addEventListener(k,fn) { this.handlers[k] = fn; }, removeEventListener() {}, focus() {}, remove() {}, showModal() { this.open = true; }, close() { this.open = false; }, click() { clicks.push(this.href); } }; }
  const get = id => { if (!nodes.has(id)) nodes.set(id,node()); return nodes.get(id); };
  const session = () => ({ authenticated: !password, requires_password: password, csrf_token: token, readiness: { ready: true }, platforms: ['instagram','threads','x','facebook','youtube'].map(id=>({id,label:id,available:id!=='threads'})) });
  const job = () => ({ id: `job-${jobId}`, status: stage, title: '테스트 영상', quality:720, progress: stage==='completed'?100:25, filename:'test.mp4', bytes:200, download_url:`/api/jobs/job-${jobId}/file` });
  const document = Object.assign(node(), { hidden:false, getElementById:get, querySelector:()=>get('brand'), createElement:()=>node(), body:node() });
  const window = Object.assign(node(), { confirm:()=>confirmResult });
  const ctx = { window, document, location:{ protocol:fileMode?'file:':'http:', href:fileMode?'file:///tmp/index.html':'http://127.0.0.1:8766/', origin:fileMode?'null':'http://127.0.0.1:8766' }, localStorage:{removeItem(){}}, navigator:{}, AbortController, DOMException, URL, console,
    setTimeout(fn,ms){timers.set(++timer,{fn,ms});return timer;}, clearTimeout(id){timers.delete(id);},
    async fetch(path,options) {
      calls.push({path,options}); let body, status=200;
      if(path==='/api/session') body=session();
      else if(csrfFail && options.method==='POST') {csrfFail=false; token='csrf-2'; status=403; body={error:'만료',code:'invalid_csrf'};}
      else if(path==='/api/inspect') { if (inspectPause) await inspectPause; body={platform:JSON.parse(options.body).platform,id:'video',title:'테스트 영상',channel:'채널',duration:null,thumbnail:null,qualities:[1080,720,480],metadata_token:'meta'}; }
      else if(path==='/api/jobs' && options.method==='POST') {jobId++;stage='queued';body=job();}
      else if(path.endsWith('/cancel')) {if(missing){status=404;body={error:'없음',code:'job_not_found'};}else body=job();}
      else if(path.startsWith('/api/jobs/')) {if(missing){status=404;body={error:'만료',code:'job_expired'};}else body=job(); if(jobPause) await jobPause;}
      else if(path==='/api/login') {password=false;body=session();}
      else throw Error('Unexpected API '+path);
      return {ok:status===200,status,async json(){return body;}};
    }};
  vm.runInNewContext(source,ctx,{filename:file});
  return {window,get,calls,clicks,timers,session, pauseInspect(promise){inspectPause=promise;}, pauseJob(promise){jobPause=promise;}, setStage(s){stage=s;}, failCsrf(){csrfFail=true;}, setMissing(v){missing=v;}, setConfirm(v){confirmResult=v;}};
}
const settle=()=>new Promise(resolve=>setImmediate(resolve));
(async()=>{
  const e=environment(); await settle(); const t=e.window.__test;
  assert.equal(t.state.session.csrf_token,'csrf-1'); assert.deepEqual(e.calls.map(c=>c.path),['/api/session']);
  e.get('video-url').value='https://www.youtube.com/watch?v=test'; await t.inspect(e.get('video-url').value);
  assert.equal(t.state.quality,720); assert.equal(e.get('video-duration').textContent,'—');
  await t.startJob(); assert.equal(t.state.job.status,'queued'); assert.equal(e.get('video-url').disabled,true);
  e.setStage('completed'); await t.pollJob(); await t.pollJob(); assert.equal(e.clicks.length,1); assert.match(e.get('manual-download').href,/job-1\/file$/); assert.equal(e.get('manual-download').hidden,false);
  await t.nextVideo(); assert.equal(t.state.job,null); assert.equal(e.get('video-url').value,'');
  e.get('video-url').value='https://www.youtube.com/watch?v=test'; e.failCsrf(); await t.inspect(e.get('video-url').value); assert.equal(t.state.session.csrf_token,'csrf-2'); assert.equal(t.state.metadata,null);
  await t.inspect(e.get('video-url').value); await t.startJob(); e.setMissing(true); await t.pollJob(); assert.equal(t.state.job.status,'failed'); assert.equal(e.get('retry-button').hidden,false); assert.equal([...e.timers.values()].some(t=>t.ms===1500),false);
  await t.nextVideo(); assert.equal(t.state.job,null); e.setMissing(false);
  e.get('video-url').value='https://www.youtube.com/watch?v=test'; await t.inspect(e.get('video-url').value); await t.startJob();
  e.setConfirm(false); const before=e.calls.length; await t.nextVideo(); assert.equal(e.calls.length,before); assert.equal(t.state.job.status,'queued');
  e.setConfirm(true); await t.nextVideo(); assert.equal(t.state.resetAfterCancel,true); assert.equal(t.state.job.status,'queued');
  e.setStage('cancelled'); await t.pollJob(); assert.equal(t.state.job,null); assert.equal(t.state.resetAfterCancel,false); assert.equal(e.get('video-url').disabled,false);
  e.get('video-url').value='https://www.youtube.com/watch?v=test'; await t.inspect(e.get('video-url').value); e.get('video-url').value='https://www.youtube.com/watch?v=changed'; e.get('video-url').handlers.input(); assert.equal(t.state.metadata,null); assert.equal(t.state.quality,null); assert.equal(e.get('save-button').disabled,true);
  assert.equal(e.calls.some(c=>c.path==='/api/jobs' && c.options.method==='GET'),false);
  assert.equal(e.calls.filter(c=>c.options.method==='POST').every(c=>c.options.credentials==='same-origin' && c.options.headers['X-CSRF-Token']),true);
  const local=environment(true); await settle(); assert.equal(local.calls.length,0); assert.equal(local.get('local-app-link').hidden,false);
  const priv=environment(false,true); await settle(); priv.get('login-password').value='password'; priv.failCsrf(); await priv.window.__test.login({preventDefault(){}}); assert.equal(priv.window.__test.state.session.csrf_token,'csrf-2'); assert.match(priv.get('login-error').textContent,/refreshed/);
  await priv.window.__test.login({preventDefault(){}}); assert.equal(priv.window.__test.state.session.authenticated,true);
  const multi=environment(); await settle(); const mt=multi.window.__test;
  assert.equal(mt.state.platform,'youtube');
  multi.get('platform-instagram').handlers.click();
  multi.get('video-url').value='https://www.instagram.com/reel/Chunk8-jurw/';
  await mt.inspect(multi.get('video-url').value);
  assert.equal(JSON.parse(multi.calls.find(c=>c.path==='/api/inspect').options.body).platform,'instagram');
  assert.equal(mt.state.metadata.platform,'instagram');
  multi.get('platform-x').handlers.click();
  assert.equal(mt.state.metadata,null); assert.equal(multi.get('save-button').disabled,true);
  assert.equal(multi.get('video-url').value,'https://www.instagram.com/reel/Chunk8-jurw/');
  assert.equal(multi.get('platform-x')['aria-pressed'],'true');
  assert.equal(multi.get('platform-instagram')['aria-pressed'],'false');
  assert.match(multi.get('video-url').placeholder,/x\.com/);
  multi.get('platform-threads').handlers.click();
  assert.equal(multi.get('inspect-button').disabled,true);
  assert.equal(multi.get('platform-status').hidden,false);
  const blockedCalls=multi.calls.length; await mt.inspect(multi.get('video-url').value);
  assert.equal(multi.calls.length,blockedCalls);
  multi.get('platform-facebook').handlers.click();
  let release; multi.pauseInspect(new Promise(resolve=>release=resolve));
  const slow=mt.inspect(multi.get('video-url').value);
  multi.get('platform-x').handlers.click(); release(); await slow;
  assert.equal(mt.state.metadata,null); assert.equal(mt.state.platform,'x');
  multi.pauseInspect(null);
  multi.get('video-url').value='https://x.com/test/status/12345';
  await mt.inspect(multi.get('video-url').value); await mt.startJob();
  assert.equal(mt.state.jobSource.platform,'x');
  assert.equal(multi.get('platform-instagram').disabled,true);
  multi.get('platform-instagram').handlers.click(); assert.equal(mt.state.platform,'x');
  multi.setStage('completed'); await mt.pollJob();
  multi.get('platform-facebook').handlers.click();
  assert.equal(mt.state.job,null); assert.equal(mt.state.metadata,null);
  assert.equal(multi.clicks.length,1);
  const retry=environment(); await settle(); const rt=retry.window.__test;
  retry.get('platform-x').handlers.click(); retry.get('video-url').value='https://x.com/test/status/12345';
  await rt.inspect(retry.get('video-url').value); await rt.startJob();
  retry.setStage('failed'); await rt.pollJob(); retry.failCsrf(); await rt.retryJob();
  assert.equal(rt.state.retrying,false); assert.equal(rt.state.job,null);
  for(const id of ['instagram','threads','x','facebook','youtube']) assert.equal(retry.get('platform-'+id).disabled,false);
  const race=environment(); await settle(); const ct=race.window.__test;
  race.get('video-url').value='https://www.youtube.com/watch?v=test';
  await ct.inspect(race.get('video-url').value); await ct.startJob();
  let releaseJob; race.pauseJob(new Promise(resolve=>releaseJob=resolve));
  const oldPoll=ct.pollJob(); race.setStage('cancelled'); await ct.cancelJob();
  assert.equal(ct.state.job.status,'cancelled'); releaseJob(); await oldPoll;
  assert.equal(ct.state.job.status,'cancelled'); assert.equal(race.get('platform-instagram').disabled,false);
  console.log(JSON.stringify({passed:true,checks:['initial session only','inspect and unknown duration','single active job lock','one automatic download plus manual fallback','next reset','invalid_csrf recovery','expired job terminates polling and allows retry','active next cancellation confirmation','wait for cancellation before reset','changed URL invalidates metadata','no GET jobs listing','same-origin and CSRF POSTs','file mode makes zero requests','private login CSRF recovery','platform selection sent to API','platform changes invalidate metadata','platform retains entered URL','selected platform and placeholder','unavailable platform blocks requests','stale platform response ignored','active job locks platform','completed platform switch resets current job','failed retry restores platform controls','stale poll cannot undo cancellation']},null,2));
})().catch(error=>{console.error(error);process.exitCode=1;});
