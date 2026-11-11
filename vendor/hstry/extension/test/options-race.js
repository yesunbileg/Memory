import assert from 'node:assert/strict';
import { elements, storageListeners } from './options-init.js';

const good = 'synthetic-valid-connection-token';
const bad = 'synthetic-invalid-connection-token';
let stored = {};
let firstCheck;
let firstStarted;
const started = new Promise(resolve => { firstStarted = resolve; });
let checks = 0;
chrome.storage.local.get = async key => key === 'status' ? {status:{}} : {settings:stored};
chrome.storage.local.set = async value => {
  if (value.settings) stored = structuredClone(value.settings);
};
chrome.runtime.sendMessage = async message => {
  if (message.type !== 'checkApi') return {ok:true};
  checks++;
  if (checks === 1) {
    firstStarted();
    return new Promise(resolve => { firstCheck = resolve; });
  }
  return stored.token === good ? {ok:true} : {ok:false,code:'unauthorized'};
};
elements.get('token').value = good;
elements.get('provider-chatgpt').checked = true;
const slow = elements.get('test-connection').listeners.get('click')();
await started;
const file = JSON.stringify({schema:1,application:'ai-memory',host:'127.0.0.1',port:41770,token:bad});
await elements.get('connection-file').listeners.get('change')({target:{files:[{size:file.length,text:async()=>file}],value:'fixture.json'}});
assert.equal(elements.get('connection-label').textContent,'Check connection');
firstCheck({ok:true});
await slow;
assert.equal(stored.token,bad);
assert.equal(elements.get('connection-label').textContent,'Check connection','old success must not overwrite a newer unauthorized result');
assert.equal(elements.get('sync-now').disabled,true);

// A second settings tab updates credentials: this view must reverify them.
stored = {...stored,token:good};
for (const listener of storageListeners) listener({settings:{newValue:stored}});
for (let turn=0;turn<10;turn++) await new Promise(resolve=>setImmediate(resolve));
assert.equal(elements.get('connection-label').textContent,'Connected');
for (const name of ['projects','pinned','agents','recents']) {
  elements.get(`capture-${name}`).checked=false;
  await elements.get(`capture-${name}`).listeners.get('change')();
}
assert.equal(elements.get('sync-now').disabled,true);
assert.ok(elements.get('resync-list').children.every(row=>row.querySelector('button').disabled),'no-category rescans stay disabled');
console.log('PASS stale auth replies, cross-tab credential changes, and empty checklist controls');
