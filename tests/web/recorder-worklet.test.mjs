import test from 'node:test';
import assert from 'node:assert/strict';
import vm from 'node:vm';
import {readFile} from 'node:fs/promises';

test('microphone worklet continues after transferred buffers are detached', async () => {
  const batches=[];
  let Recorder;
  const sandbox={Float32Array, AudioWorkletProcessor:class {
    constructor(){this.port={postMessage(data, transfer){batches.push(structuredClone(data,{transfer}));}};}
  },registerProcessor(name, constructor){assert.equal(name,'dawa-recorder');Recorder=constructor;}};
  vm.runInNewContext(await readFile(new URL('../../web/recorder-worklet.js',import.meta.url),'utf8'),sandbox);
  const recorder=new Recorder({processorOptions:{batch:1024}});
  for(let i=0;i<160;i++)assert.equal(recorder.process([[new Float32Array(128).fill(.25)]]),true);
  assert.equal(batches.length,20);
  assert.ok(batches.every(batch=>batch.length===1024 && batch[0]===.25 && batch[1023]===.25));
  assert.equal(recorder.buf.length,1024);
  recorder.port.onmessage({data:'stop'});
  assert.equal(recorder.process([]),false);
});
