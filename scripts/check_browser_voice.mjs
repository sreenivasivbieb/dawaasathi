// Real Chromium getUserMedia/AudioWorklet capture using an injected synthetic WAV.
// The server must have live Gnani speech configured. No physical microphone is recorded.
const {chromium}=await import(process.env.PLAYWRIGHT_MODULE || 'playwright');
import assert from 'node:assert/strict';
import {writeFile,mkdir} from 'node:fs/promises';
const base=process.env.DAWA_CHECK_URL || 'http://127.0.0.1:8004';
const sample=process.env.DAWA_MIC_SAMPLE;
const reportPath=process.env.DAWA_VOICE_REPORT || 'artifacts/browser-voice-check.json';
const screenshotPath=process.env.DAWA_VOICE_SCREENSHOT || 'artifacts/browser-voice-check.png';
if(!sample)throw Error('Set DAWA_MIC_SAMPLE to an absolute synthetic WAV path.');
const browser=await chromium.launch({executablePath:process.env.CHROMIUM_PATH || '/usr/bin/chromium',headless:true,args:['--no-sandbox','--disable-dev-shm-usage','--use-fake-device-for-media-stream','--use-fake-ui-for-media-stream',`--use-file-for-fake-audio-capture=${sample}%noloop`]});
const context=await browser.newContext({permissions:['microphone'],viewport:{width:1440,height:1050},reducedMotion:'reduce'});
const page=await context.newPage();
const report={synthetic_microphone:true,physical_microphone_verified:false,live_provider:true,page_errors:[],requests:[],pcm_bytes:0};
const audioReads=[];
let resolveQuery;
const queried=new Promise(resolve=>{resolveQuery=resolve;});
page.on('pageerror',error=>report.page_errors.push(error.message));
page.on('request',request=>{if(request.url().includes('/v1/'))report.requests.push({path:new URL(request.url()).pathname,method:request.method(),body:request.url().endsWith('/v1/interrupt')?request.postDataJSON():undefined});});
page.on('response',response=>{
 if(response.url().endsWith('/v1/query'))response.json().then(data=>{report.result={status:data.status,transcript:data.transcript,language:data.lang,detected_language:data.detected_language,tts:data.tts,audio_id:data.audio_id};resolveQuery(data);});
 if(/\/v1\/audio\/.*\.(?:pcm|wav)$/.test(response.url()))audioReads.push(response.body().then(bytes=>{report.pcm_bytes+=bytes.length;}));
});
try{
 await page.goto(base,{waitUntil:'networkidle'});
 await page.locator('#mic:not([disabled])').waitFor();
 await page.evaluate(async()=>{
  const {Mic}=await import('/audio.js'); window.captureFrameCount=0;
  const batch=Mic.prototype._batch;
  Mic.prototype._batch=function(...args){window.captureFrameCount++;return batch.apply(this,args);};
 });
 await page.locator('#mic').click();
 await page.waitForFunction(()=>document.querySelector('#mic').dataset.state==='recording');
 await page.waitForFunction(()=>window.captureFrameCount>10);
 const result=await Promise.race([queried,new Promise((_,reject)=>setTimeout(()=>reject(Error('No recognized result after 45 seconds')),45000))]);
 assert.ok(result.transcript);assert.equal(result.tts,'timbre');
 await page.waitForFunction(()=>document.querySelector('#mic').dataset.state==='speaking',{timeout:30000});
 await page.waitForFunction(()=>{const p=document.querySelector('#reply-player');return p&&!p.paused&&p.currentTime>0;});
 report.playback_started=true;
 report.audio_context_state=await page.evaluate(async()=>(await import('/audio.js')).audioContext().state);
 assert.equal(report.audio_context_state,'running');
 await Promise.all(audioReads);
 assert.ok(report.pcm_bytes>0);
 assert.equal(report.requests.filter(r=>r.path==='/v1/interrupt').length,0,'Fresh answer must not cancel its own audio.');
 report.capture_batches=await page.evaluate(()=>window.captureFrameCount);
 assert.ok(report.capture_batches>10);
 await mkdir('artifacts',{recursive:true});
 await page.screenshot({path:screenshotPath,fullPage:true});
 await page.locator('#voice-stop').click();
 await page.waitForTimeout(300);
 assert.ok(report.requests.some(r=>r.path==='/v1/interrupt'&&r.body?.audio_id===result.audio_id));
 report.stop_targeted_correct_audio=true;
 await page.request.delete(base+'/v1/threads/'+result.session_id);
 assert.deepEqual(report.page_errors,[]);
 report.passed=true;
 await writeFile(reportPath,JSON.stringify(report,null,2)+'\n');
 console.log(JSON.stringify(report,null,2));
}finally{await browser.close();}
