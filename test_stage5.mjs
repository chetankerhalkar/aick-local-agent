// Explicit browser audio mocks: these are lifecycle checks, not meeting acceptance.
import test from 'node:test';
import assert from 'node:assert/strict';
import {SpeechController} from './static/speech.js';

function setup() {
  const calls = [], urls = [], listeners = {};
  let report = {id:'T-test', active:true, phase:'ready', state:'ready', message:'Ready'};
  const audio = {paused:true, currentTime:0, addEventListener:(event,fn)=>listeners[event]=fn,
    pause(){this.paused=true;}, removeAttribute(){delete this.src;}, load(){}, async play(){this.paused=false;}};
  const controller = new SpeechController({token:'mock-token', audio, onMeeting(){}, onChange(view){controller.view=view;},
    async api(path, body){calls.push({path,body}); if(path.endsWith('/finish')) report={...report,active:false,phase:body.reason}; return {speech:report};}}, {
    delay:async()=>{}, fetch:async()=>({ok:true,blob:async()=>new Blob(['mock wave'])}),
    urls:{createObjectURL(){urls.push('create');return 'blob:mock';},revokeObjectURL(){urls.push('revoke');}}});
  return {controller,audio,calls,urls,listeners};
}
test('autoplay refusal exposes Play and never releases the ASR hold until Stop', async()=>{
  const {controller,audio,calls,urls}=setup();
  audio.play=async()=>{throw new DOMException('blocked','NotAllowedError');};
  await controller.start({approved:true,answer_id:'C1'});
  assert.equal(controller.view.playable,true);
  assert.match(controller.view.message,/Click Play/);
  assert.equal(calls.filter(c=>c.path.endsWith('/finish')).length,0);
  await controller.stop();
  assert.equal(calls.at(-1).body.reason,'stopped');
  assert.equal(controller.run,null); assert.equal(audio.src,undefined);
  assert.deepEqual(urls,['create','revoke']);
});
test('ended clears the audio source, revokes the blob, and acknowledges end', async()=>{
  const {controller,audio,calls,urls,listeners}=setup();
  await controller.start({approved:true,answer_id:'C1'});
  assert.equal(audio.paused,false);
  await listeners.ended();
  assert.equal(audio.paused,true);
  assert.equal(calls.at(-1).body.reason,'ended');
  assert.equal(controller.run,null); assert.deepEqual(urls,['create','revoke']);
});
test('Stop during a pending start cancels late preparation without playback', async()=>{
  const {controller,calls,audio}=setup();
  let resolveStart;
  const normal=controller.api;
  controller.api=(path,body)=>path==='/api/meeting/speech'?new Promise(resolve=>resolveStart=resolve):normal(path,body);
  const starting=controller.start({approved:true});
  await controller.stop();
  resolveStart({speech:{id:'T-test',active:true,phase:'ready'}});
  await starting;
  assert.equal(audio.src,undefined); assert.equal(controller.run,null);
  assert.equal(calls.at(-1).body.reason,'stopped');
});
test('a rejected start releases UI controls and cannot play any audio', async()=>{
  const {controller,audio}=setup(); controller.api=async()=>{throw Error('Stale answer');};
  await controller.start({approved:true});
  assert.equal(controller.run,null); assert.equal(audio.src,undefined);
  assert.match(controller.view.message,/Stale answer/);
});
