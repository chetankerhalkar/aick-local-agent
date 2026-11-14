// SOFTWARE unit tests. Browser tracks, clocks, sockets and audio below are explicit mocks.
import test from 'node:test';
import assert from 'node:assert/strict';
import {CaptureController} from './static/capture.js';

test('an old pause timeout must not stop a newly started capture', async t=>{
  t.mock.timers.enable({apis:['setTimeout']});
  const controller=new CaptureController({token:'UNIT MOCK',getMeeting:()=>({id:'M-unit'}),onMeeting(){},onChange(){}},{});
  const old={socket:{send(){}},node:{port:{postMessage(){}}},ready:true};
  controller.run=old;
  const pending=controller.control(old,{type:'pause',position:0,reason:'user pause'}).catch(error=>error);
  const replacement={socket:{readyState:0,close(){}},node:null,source:null,context:null};
  controller.run=replacement;
  t.mock.timers.tick(5001);
  await pending;
  assert.equal(controller.run,replacement);
});

function fakeBrowser() {
  const sockets=[],nodes=[],tracks=[];
  const dependencies={
    async getDisplayMedia(){const pair=[{readyState:'live',getSettings:()=>({displaySurface:'browser'}),stop(){this.readyState='ended';},addEventListener(){}},
      {readyState:'live',stop(){this.readyState='ended';},addEventListener(){}}];tracks.push(pair);return {getTracks:()=>pair,getAudioTracks:()=>[pair[1]],getVideoTracks:()=>[pair[0]]};},
    createContext:()=>({sampleRate:48000,state:'running',resume:async()=>{},close:async()=>{},audioWorklet:{addModule:async()=>{}},createMediaStreamSource:()=>({connect(){},disconnect(){}})}),
    audioStream:()=>({}),createWorklet:()=>{const node={port:{postMessage(){}},disconnect(){}};nodes.push(node);return node;},
    createSocket:()=>{const socket={readyState:1,bufferedAmount:0,close(){},send(raw){const message=JSON.parse(raw);if(message.type==='start')queueMicrotask(()=>this.onmessage({data:JSON.stringify({type:'ready',capture_id:'C-'+sockets.length,epoch:0,meeting:{id:'M-unit',capture:{active:true,paused:false,state:'listening'}}})}));}};sockets.push(socket);queueMicrotask(()=>socket.onopen());return socket;},
    fetch:async()=>({ok:true,json:async()=>({id:'M-unit'})})
  };
  const controller=new CaptureController({token:'UNIT MOCK',getMeeting:()=>({id:'M-unit',revision:0}),onMeeting(){},onChange(){}},dependencies);
  return {controller,sockets,nodes,tracks};
}

test('late errors from closed socket and worklet must not stop a new capture',async()=>{
  const {controller,sockets,nodes,tracks}=fakeBrowser();
  try {
    await controller.start();await controller.stop();await controller.start();
    const current=controller.run;
    sockets[0].onerror(); nodes[0].onprocessorerror();
    assert.equal(controller.run,current);
    assert.equal(tracks[1][0].readyState,'live');
  } finally {await controller.stop();}
});

test('browser socket overflow drops PCM visibly without routing audio to speakers',async()=>{
  const {controller,sockets,nodes}=fakeBrowser();
  try {
    await controller.start();sockets[0].bufferedAmount=300*1024;
    nodes[0].port.onmessage({data:{position:0,frames:4800,epoch:0,rms:.2,pcm:new Float32Array(4800).buffer}});
    assert.equal(controller.last.browserDropped,.1);
    assert.match(controller.last.message,/dropped/);
    assert.equal(controller.run.position,4800);
  } finally {await controller.stop();}
});

test('ping cannot acknowledge pause or re-enable PCM while pause is pending',async()=>{
  const {controller,sockets}=fakeBrowser();
  try {
    await controller.start();
    let acknowledged=false;
    const pending=controller.pause().then(()=>{acknowledged=true;});
    const control=(kind,paused)=>sockets[0].onmessage({data:JSON.stringify({type:'control',control:kind,capture_id:controller.run.captureId,epoch:1,meeting:{id:'M-unit',capture:{active:true,paused,state:'listening'}}})});
    control('ping',false);
    await Promise.resolve();await Promise.resolve();
    assert.equal(acknowledged,false);
    assert.equal(controller.run.paused,true);
    control('pause',true);await pending;
    assert.equal(acknowledged,true);
  } finally {await controller.stop();}
});

test('Stop cancels a pending control and its timer before another session starts',async t=>{
  t.mock.timers.enable({apis:['setTimeout']});
  const {controller}=fakeBrowser();
  await controller.start();
  let outcome='pending';
  const pending=controller.pause().then(()=>{outcome='resolved';},()=>{outcome='cancelled';});
  await controller.stop();
  await Promise.resolve();
  assert.equal(outcome,'cancelled');
  await pending;
  await controller.start();
  const current=controller.run;
  t.mock.timers.tick(5001);
  assert.equal(controller.run,current);
  await controller.stop();
});

test('late Stop HTTP failure cannot overwrite a newer capture; request has a bounded signal',async()=>{
  const {controller}=fakeBrowser();
  await controller.start();
  let rejectOld;
  controller.dependencies.fetch=(_url,options)=>{
    assert.ok(options.signal instanceof AbortSignal);
    return new Promise((_resolve,reject)=>{rejectOld=reject;});
  };
  const stopping=controller.stop();
  await controller.start();
  const current=controller.run, message=controller.last.message;
  rejectOld(new Error('UNIT MOCK old HTTP failure'));
  await stopping;
  assert.equal(controller.run,current);
  assert.equal(controller.last.message,message);
  controller.dependencies.fetch=async()=>({ok:true,json:async()=>({id:'M-unit'})});
  await controller.stop();
});
