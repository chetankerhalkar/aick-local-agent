// Browser-boundary unit tests. All browser media and socket objects below are mocks.
import test from 'node:test';
import assert from 'node:assert/strict';
import {CaptureController} from './static/capture.js';

test('no audio track releases all acquired tracks and gives a specific retry', async () => {
  let stopped = 0, options, view;
  const video = {stop() {stopped++; this.readyState = 'ended';}, readyState: 'live'};
  const controller = new CaptureController({token:'mock', getMeeting:()=>({id:'M-mock'}), onMeeting(){}, onChange:v=>view=v}, {
    getDisplayMedia: async value => {options=value; return {getTracks:()=>[video],getAudioTracks:()=>[]};}
  });
  await controller.start();
  assert.equal(options.video, true); assert.equal(options.audio, true);
  assert.equal(stopped, 1); assert.equal(controller.run, null);
  assert.match(view.message, /No tab audio.*Share tab audio/);
  assert.equal(view.tracksReleased, true);
});

test('Stop during picker invalidates the pending grant and ends every late track', async () => {
  let resolvePicker, stopped=0;
  const controller = new CaptureController({token:'mock',getMeeting:()=>({id:'M-mock'}),onMeeting(){},onChange(){}}, {
    getDisplayMedia:()=>new Promise(resolve=>resolvePicker=resolve)
  });
  const starting=controller.start();
  await controller.stop();
  resolvePicker({getTracks:()=>[{stop(){stopped++;}},{stop(){stopped++;}}]});
  await starting;
  assert.equal(stopped,2); assert.equal(controller.run,null);
});

test('worklet averages stereo, bounds port messages, preserves sample positions and pauses PCM', async () => {
  let Processor;
  globalThis.sampleRate=48000;
  globalThis.registerProcessor=(name, cls)=>Processor=cls;
  globalThis.AudioWorkletProcessor=class {constructor(){this.messages=[];this.port={postMessage:(message)=>this.messages.push(message)};}};
  await import('./static/pcm-worklet.js');
  const processor=new Processor();
  const left=new Float32Array(4800).fill(0.75), right=new Float32Array(4800).fill(0.25);
  for(let i=0;i<6;i++) processor.process([[left,right]]);
  assert.equal(processor.messages.length,4);
  assert.equal(new Float32Array(processor.messages[0].pcm)[0],0.5);
  assert.equal(processor.messages[0].frames,4800);
  processor.port.onmessage({data:{type:'credit'}});
  processor.process([[left,right]]);
  assert.equal(processor.messages.at(-1).position,28800);
  processor.port.onmessage({data:{type:'state',paused:true,epoch:1}});
  processor.port.onmessage({data:{type:'credit'}});
  processor.process([[left,right]]);
  assert.equal(processor.messages.at(-1).pcm,null);
  assert.equal(processor.messages.at(-1).epoch,1);
  processor.port.onmessage({data:{type:'stop'}});
  assert.equal(processor.process([[left,right]]),false);
});
