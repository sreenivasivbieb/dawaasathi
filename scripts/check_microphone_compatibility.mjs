// Browser microphone compatibility tests. Uses synthetic devices; records no room audio.
const {chromium}=await import(process.env.PLAYWRIGHT_MODULE || 'playwright');
import assert from 'node:assert/strict';import{mkdir,writeFile}from'node:fs/promises';
const base=process.env.DAWA_CHECK_URL || 'http://127.0.0.1:8004';
const browser=await chromium.launch({executablePath:process.env.CHROMIUM_PATH || '/usr/bin/chromium',headless:true,args:['--no-sandbox','--disable-dev-shm-usage','--use-fake-device-for-media-stream','--use-fake-ui-for-media-stream']});
const checks=[];
try{
 for(const variant of ['missing-worklet','failed-worklet-module']){
  const context=await browser.newContext({permissions:['microphone']});const page=await context.newPage();await page.goto(base);
  const result=await page.evaluate(async variant=>{
   const audio=await import('/audio.js'),ctx=audio.audioContext();await ctx.resume();
   if(variant==='missing-worklet')window.AudioWorkletNode=undefined;
   else ctx.audioWorklet.addModule=async()=>{throw Error('test failure');};
   const mic=new audio.Mic();let batches=0;mic.onframe=()=>batches++;
   await mic.start(16000);await new Promise(resolve=>setTimeout(resolve,600));
   const mode=mic.mode;const tracks=mic.stream.getTracks();mic.stop();
   return{mode,batches,tracksStopped:tracks.every(t=>t.readyState==='ended')};
  },variant);
  assert.equal(result.mode,'compatibility');assert.ok(result.batches>3);assert.equal(result.tracksStopped,true);
  checks.push({case:variant,...result,passed:true});await context.close();
 }
 for(const [error,key]of [['NotAllowedError','mic_denied'],['NotFoundError','mic_no_device'],['NotReadableError','mic_busy']]){
  const context=await browser.newContext({permissions:['microphone']});const page=await context.newPage();
  await page.addInitScript(name=>{navigator.mediaDevices.getUserMedia=async()=>{throw Object.assign(Error('test'),{name});};},error);
  await page.goto(base);await page.locator('#mic:not([disabled])').waitFor();await page.locator('#mic').click();
  await page.locator('#mic-error:not([hidden])').waitFor();
  assert.equal(await page.locator('#mic-error-code').textContent(),error);
  const expected=await page.evaluate(async key=>(await import('/i18n.js')).t(document.documentElement.lang,key),key);
  assert.equal(await page.locator('#mic-error-message').textContent(),expected);
  assert.equal(await page.locator('#mic').isEnabled(),true);
  checks.push({case:error,localized_message:key,retry_available:true,passed:true});await context.close();
 }
 await mkdir('artifacts',{recursive:true});await writeFile('artifacts/microphone-compatibility-checks.json',JSON.stringify({synthetic_devices:true,checks},null,2)+'\n');
 console.log(JSON.stringify(checks,null,2));
}finally{await browser.close();}
