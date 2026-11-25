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
  const events={}, timers=new Map(), requests=[], nodes={};let id=0;
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
    addEventListener:(name,fn)=>{events[name]=fn;},console:{error() {}}};
  const document={getElementById:name=>nodes[name]||null,querySelector:()=>({content:'csrf'}),
    addEventListener:(name,fn)=>{events[name]=fn;}};
  vm.runInNewContext(fs.readFileSync(path.join(assets,'reader.js'),'utf8'),{window,document,XMLHttpRequest:XHR,Image:class {},isFinite,JSON,Number,Math,Error});
  function flush() {for (const [id,fn] of [...timers]) {timers.delete(id);fn();}}
  return {nodes,requests,events,flush,timers};
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
