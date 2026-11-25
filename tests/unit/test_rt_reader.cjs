/* SPDX-License-Identifier: GPL-3.0-or-later */
const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');
const assets = path.resolve(__dirname, '..', '..', 'cps', 'rt', 'static');

function contentsHarness(patched) {
  const frames = new Map(), timers = new Map(), listeners = new Set();
  let id = 0, detached = false, reads = 0;
  const document = {visibilityState: 'visible', addEventListener: (name, fn) => listeners.add(fn),
    removeEventListener: (name, fn) => listeners.delete(fn)};
  const context = {document, requestAnimationFrame: fn => {frames.set(++id, fn);return id;},
    cancelAnimationFrame: id => frames.delete(id), setTimeout: fn => {timers.set(++id, fn);return id;},
    clearTimeout: id => timers.delete(id)};
  context.window = {...context};
  vm.createContext(context);
  vm.runInContext(fs.readFileSync(path.join(assets, 'vendor', 'epub.legacy.js'), 'utf8'), context);
  if (patched) vm.runInContext(fs.readFileSync(path.join(assets, 'epub-lifecycle.js'), 'utf8'), context);
  const contents = Object.create(context.window.ePub.Contents.prototype);
  Object.assign(contents, {active: true, _size: {width: 0, height: 0}, removeEventListeners() {},
    removeSelectionListeners() {}, textWidth() {if (detached) throw new Error('Permission denied');reads++;return 0;},
    textHeight() {if (detached) throw new Error('Permission denied');return 0;}});
  return {contents, frames, timers, listeners, detach: () => {detached = true;}, reads: () => reads,
    visibility: state => {document.visibilityState = state;for (const fn of [...listeners]) fn();}};
}

test('vendor regression: an outstanding resize accesses a destroyed document without the shim', () => {
  const h=contentsHarness(false);
  h.contents.resizeListeners();h.contents.visibilityListeners();h.contents.destroy();h.detach();
  assert.throws([...h.frames.values()][0], /Permission denied/);
  h.visibility('hidden');h.visibility('visible');
  assert.equal(h.listeners.size, 1);
  assert.equal(h.timers.size, 1);
});

test('destroy removes visibility handler, timers, frames and guards an already-dispatched callback', () => {
  const h=contentsHarness(true);
  h.contents.resizeListeners();h.contents.visibilityListeners();
  const queued=[...h.frames.values()][0];
  h.contents.destroy();h.detach();queued();h.contents.resizeCheck();
  h.contents.resizeListeners();h.contents.visibilityListeners();
  h.visibility('hidden');h.visibility('visible');
  assert.equal(h.frames.size, 0);assert.equal(h.timers.size, 0);assert.equal(h.listeners.size, 0);
  assert.equal(h.reads(), 0);
});

test('live contents can suspend and resume without accumulating visibility listeners', () => {
  const h=contentsHarness(true);
  h.contents.resizeListeners();h.contents.visibilityListeners();h.contents.visibilityListeners();
  h.visibility('hidden');assert.equal(h.frames.size, 0);assert.equal(h.timers.size, 0);
  h.visibility('visible');assert.equal(h.frames.size, 1);assert.equal(h.timers.size, 1);
  [...h.frames.values()][0]();assert.equal(h.reads(), 1);
  assert.equal(h.listeners.size, 1);h.contents.destroy();assert.equal(h.listeners.size, 0);
});

function readerHarness(overrides={}) {
  const events={}, timers=new Map(), requests=[], nodes={}, redirects=[];let id=0;
  const config={kind:'pages', bookmark:'1', pageUrl:'/rt/page/1/cbz/0',listUrl:'/comic/1/cbz/pages',
    saveUrl:'/ajax/bookmark/1/CBZ',labels:{loading:'loading',ready:'ready',failed:'failed',closed:'closed',saved:'saved',saveFailed:'save failed'}};
  Object.assign(config,overrides);
  for (const name of ['rt-config','reader-status','save-status','previous','next','page-number','resolution','page-image','page-count','go','close-reader','audio']) {
    nodes[name]={textContent:'',value:name==='resolution'?'1600':'',disabled:false,listeners:{},
      addEventListener(name,fn) {this.listeners[name]=fn;},removeAttribute(name) {delete this[name];},pause() {},load() {}};
  }
  nodes['rt-config'].textContent=JSON.stringify(config);
  class XHR {
    open(method,url) {this.method=method;this.url=url;}
    setRequestHeader() {}
    send(body) {this.body=body;requests.push(this);}
    complete(status,text='') {this.status=status;this.responseText=text;this.onload();}
  }
  const window={setTimeout:fn=>{timers.set(++id,fn);return id;},clearTimeout:id=>timers.delete(id),
    addEventListener:(name,fn)=>{events[name]=fn;},console:{error() {}},
    location:{assign:url=>redirects.push(url)},
    RTImmersive:()=>({open() {},positionError() {},destroy() {},resetZoom() {},progress() {},imageLoaded() {},isOpen:()=>false})};
  const document={getElementById:name=>nodes[name]||null,querySelector:()=>({content:'csrf'}),
    addEventListener:(name,fn)=>{events[name]=fn;}};
  vm.runInNewContext(fs.readFileSync(path.join(assets,'reader.js'),'utf8'),{window,document,XMLHttpRequest:XHR,Image:class {},isFinite,JSON,Number,Math,Error});
  function flush() {for (const [id,fn] of [...timers]) {timers.delete(id);fn();}}
  return {nodes,requests,events,flush,timers,redirects};
}

test('page reader clamps navigation, serializes bookmark writes and clears on close', () => {
  const h=readerHarness();h.requests[0].complete(200,JSON.stringify({pages:[{},{}]}));
  assert.equal(h.nodes['page-number'].value,2);
  assert.equal(h.nodes.next.disabled,true);
  h.nodes['page-image'].onload();h.flush();
  assert.equal(h.requests[1].body,'bookmark=1');
  h.nodes.previous.onclick();h.nodes['page-image'].onload();h.flush();
  assert.equal(h.requests.length,2,'never overlap writes');
  h.requests[1].complete(201);
  assert.equal(h.requests[2].body,'bookmark=0');
  h.requests[2].complete(500);assert.equal(h.nodes['save-status'].textContent,'save failed');
  h.nodes['close-reader'].onclick();
  assert.equal(h.nodes['page-image'].src,undefined);
  assert.equal(h.nodes['reader-status'].textContent,'closed');
  assert.equal(h.nodes.next.disabled,true);
  assert.equal(h.timers.size,0);
  h.nodes.next.onclick();assert.equal(h.nodes['page-image'].src,undefined);
});

test('a late page-list response cannot recreate a closed reader', () => {
  const h=readerHarness();h.nodes['close-reader'].onclick();
  h.requests[0].complete(200,JSON.stringify({pages:[{}]}));
  assert.equal(h.nodes['page-image'].src,undefined);
  assert.equal(h.nodes['reader-status'].textContent,'closed');
});

test('Close book waits for the last bookmark write before returning', () => {
  const h=readerHarness({returnUrl:'/rt/book/1'});
  h.requests[0].complete(200,JSON.stringify({pages:[{},{}]}));h.nodes['page-image'].onload();h.flush();
  h.nodes['close-reader'].onclick();assert.deepEqual(h.redirects,[]);
  h.requests[1].complete(201);assert.deepEqual(h.redirects,['/rt/book/1']);
});

test('audio reuses millisecond bookmarks and continuous updates do not postpone saving', () => {
  const h=readerHarness({kind:'audio',bookmark:'12500'}), audio=h.nodes.audio;
  audio.duration=100;audio.listeners.loadedmetadata();assert.equal(audio.currentTime,12.5);
  audio.currentTime=13.25;audio.listeners.timeupdate();
  audio.currentTime=13.75;audio.listeners.timeupdate();
  assert.equal(h.timers.size,1);h.flush();
  assert.equal(h.requests[0].body,'bookmark=13750');
  h.requests[0].complete(200,'<html>login required</html>');
  assert.equal(h.nodes['save-status'].textContent,'save failed');
  audio.currentTime=14;audio.listeners.pause();
  assert.equal(h.requests[1].body,'bookmark=14000');
});

function immersiveHarness(kind='pages') {
  const nodes={}, calls=[], timers=new Map();let id=0;
  function node(name,tag='DIV') {
    const listeners={};
    return {nodeType:1,tagName:tag,style:{},hidden:name==='reader-modal',disabled:false,clientWidth:1000,clientHeight:800,
      offsetWidth:100,offsetHeight:50,naturalWidth:1200,naturalHeight:1600,value:1,
      addEventListener(name,fn) {(listeners[name]??=new Set()).add(fn);},
      removeEventListener(name,fn) {listeners[name]?.delete(fn);},
      emit(name,event={}) {for(const fn of [...listeners[name]||[]]) fn({target:this,preventDefault(){},...event});},
      listenerCount:()=>Object.values(listeners).reduce((sum,items)=>sum+items.size,0),
      getBoundingClientRect:()=>({left:0,top:0,width:1000,height:800}),setAttribute(){},removeAttribute(){},
      focus(){document.activeElement=this;}};
  }
  for(const name of ['reader-stage','reader-modal','reader-dialog','reading-progress','progress-label','page-image','reader-menu','dialog-close','fullscreen','reset-zoom','reader-return']) nodes[name]=node(name);
  if(kind!=='pages') delete nodes['page-image'];
  const document=node('document');document.getElementById=name=>nodes[name];document.documentElement=node('root');
  const window=node('window');window.setTimeout=fn=>{timers.set(++id,fn);return id;};window.clearTimeout=id=>timers.delete(id);
  nodes['reader-dialog'].querySelectorAll=()=>[nodes['dialog-close'],nodes['reading-progress'],nodes.fullscreen];
  vm.runInNewContext(fs.readFileSync(path.join(assets,'immersive.js'),'utf8'),{window,document,Math,Number,Object,Array});
  const ui=window.RTImmersive({kind,labels:{page:'Page',position:'Position',fullscreenFailed:'fullscreen failed'}},
    {navigate:delta=>calls.push(['navigate',delta]),seek:page=>calls.push(['seek',page]),resize:(w,h)=>calls.push(['resize',w,h]),error:msg=>calls.push(['error',msg])});
  function pointer(type,id,x,y) {nodes['reader-stage'].emit('pointer'+type,{pointerId:id,clientX:x,clientY:y,button:0});}
  function tap(x,y) {pointer('down',1,x,y);pointer('up',1,x,y);}
  function flush() {for(const [id,fn] of [...timers]) {timers.delete(id);fn();}}
  return {ui,nodes,calls,window,document,pointer,tap,flush,timers,node};
}

test('immersive tap zones, slider and modal X/Escape/focus trap are isolated from navigation', () => {
  const h=immersiveHarness();h.ui.progress(2,10);
  h.tap(199,700);h.tap(800,700);h.tap(500,400);
  assert.deepEqual(h.calls,[['navigate',-1],['navigate',1]]);
  h.tap(500,159);assert.equal(h.nodes['reader-modal'].hidden,false);
  assert.equal(h.document.activeElement,h.nodes['dialog-close']);
  h.tap(50,50);assert.equal(h.calls.length,2,'modal blocks page navigation');
  h.nodes['reading-progress'].value='7';h.nodes['reading-progress'].emit('change');
  assert.deepEqual(h.calls[2],['seek',6]);
  h.nodes.fullscreen.focus();h.document.emit('keydown',{keyCode:9});
  assert.equal(h.document.activeElement,h.nodes['dialog-close']);
  h.nodes['dialog-close'].onclick();assert.equal(h.nodes['reader-modal'].hidden,true);
  h.nodes['reader-menu'].onclick();h.document.emit('keydown',{keyCode:27});assert.equal(h.nodes['reader-modal'].hidden,true);
});

test('pinch zoom and panning suppress page taps, rotation refits, and close cancels work', () => {
  const h=immersiveHarness();h.ui.imageLoaded();
  assert.match(h.nodes['page-image'].style.transform,/scale\(1\)/);
  h.pointer('down',1,300,400);h.pointer('down',2,700,400);
  h.pointer('move',1,100,400);h.pointer('move',2,900,400);
  assert.match(h.nodes['page-image'].style.transform,/scale\(2\)/);
  h.pointer('up',1,100,400);h.pointer('up',2,900,400);assert.equal(h.calls.length,0);
  h.pointer('down',1,500,400);h.pointer('move',1,550,450);h.pointer('up',1,550,450);
  assert.equal(h.calls.length,0);assert.match(h.nodes['page-image'].style.transform,/scale\(2\)/);
  h.window.emit('orientationchange');h.flush();assert.deepEqual(h.calls[0],['resize',1000,800]);
  h.ui.resetZoom();assert.match(h.nodes['page-image'].style.transform,/scale\(1\)/);
  h.window.emit('resize');h.ui.destroy();h.flush();
  assert.equal(h.timers.size,0);assert.equal(h.nodes['reader-stage'].listenerCount(),0);
  assert.equal(h.window.listenerCount(),0);assert.equal(h.nodes['reader-return'].hidden,false);
  h.nodes['dialog-close'].onclick();assert.equal(h.nodes['reader-modal'].hidden,true,'X works even after closing book');
});

test('EPUB frame gestures are detached with Contents without retaining stale documents', () => {
  const h=immersiveHarness('epub'), doc=h.node('frame-document');
  const contents={document:doc,window:{frameElement:{getBoundingClientRect:()=>({left:0,top:0})}},
    removeListeners(){this.rtDisposed=true;}};
  h.ui.bindContents(contents);
  doc.emit('pointerdown',{pointerId:1,clientX:900,clientY:500,button:0});
  doc.emit('pointerup',{pointerId:1,clientX:900,clientY:500});
  assert.deepEqual(h.calls,[['navigate',1]]);
  contents.removeListeners();assert.equal(doc.listenerCount(),0);
  doc.emit('pointerup',{pointerId:1,clientX:900,clientY:500});assert.equal(h.calls.length,1);
});

test('unzoomed comic swipes are preserved without interpreting pinches as swipes', () => {
  const h=immersiveHarness();h.ui.imageLoaded();
  h.pointer('down',1,700,400);h.pointer('move',1,400,400);h.pointer('up',1,400,400);
  assert.deepEqual(h.calls,[['navigate',1]]);
  h.pointer('down',1,300,400);h.pointer('down',2,700,400);
  h.pointer('move',1,400,400);h.pointer('move',2,600,400);
  h.pointer('up',1,400,400);h.pointer('up',2,600,400);
  assert.equal(h.calls.length,1);
});

test('audio progress starts at zero seconds and seeks the displayed elapsed time', () => {
  const h=immersiveHarness('audio');h.ui.progress(0,60);
  assert.equal(h.nodes['reading-progress'].min,0);
  assert.equal(h.nodes['reading-progress'].value,0);
  h.nodes['reading-progress'].value='20';h.nodes['reading-progress'].emit('change');
  assert.deepEqual(h.calls,[['seek',20]]);
});
