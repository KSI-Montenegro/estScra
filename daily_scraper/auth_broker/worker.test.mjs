import {test} from 'node:test';
import assert from 'node:assert/strict';
import worker, {PatuljakAuth, verifyTextbee, patuljakCode, validCookie} from './worker.mjs';

const SECRET = 'test-only-webhook-secret-at-least-20';
const DEVICE = '0123456789abcdef01234567';
const HASH = 'a'.repeat(64);
const COOKIE = 'PHPSESSID=test-session; id=123; 5tg=KzM4MjY5MTIzNDU2';
function setup() {
  const data = new Map(); let tail = Promise.resolve();
  const storage = {
    async get(key) { return structuredClone(data.get(key)); },
    async put(key, value) { data.set(key, structuredClone(value)); },
    async setAlarm(time) { data.set('alarm', time); },
  };
  const ctx = {storage, blockConcurrencyWhile(fn) { const result = tail.then(fn); tail = result.catch(() => {}); return result; }};
  const env = {TEXTBEE_DEVICE_ID: DEVICE, TEXTBEE_WEBHOOK_SECRET: SECRET, AUTH_CLIENT_KEY: 'k'.repeat(40)};
  const obj = new PatuljakAuth(ctx, env); let now = Date.now(); obj.now = () => now;
  env.AUTH_STATE = {idFromName: () => 'one-account', get: () => obj};
  const request = (path, method='GET', body) => obj.fetch(new Request('https://private.test'+path,
    {method, body: body === undefined ? undefined : JSON.stringify(body)}));
  const message = (overrides={}) => ({smsId:'sms-1',deviceId:DEVICE,webhookEvent:'MESSAGE_RECEIVED',
    sender:'PATULJAK',message:'Vaš Patuljak kod je 123456',receivedAt:new Date(now).toISOString(),...overrides});
  const pair = () => request('/internal/textbee','POST',message({smsId:'pair',sender:'Other',message:'Unrelated SMS'}));
  const begin = async () => { const r=await request('/v1/login/begin','POST',{phone_hash:HASH}); return {status:r.status,...await r.json()}; };
  return {data,env,obj,request,message,pair,begin,advance:ms=>{now+=ms;},now:()=>now};
}
async function signature(body, timestamp) {
  const key=await crypto.subtle.importKey('raw',new TextEncoder().encode(SECRET),{name:'HMAC',hash:'SHA-256'},false,['sign']);
  const signed=await crypto.subtle.sign('HMAC',key,new TextEncoder().encode(timestamp+'.'+body));
  return 't='+timestamp+',v1='+Array.from(new Uint8Array(signed),b=>b.toString(16).padStart(2,'0')).join('');
}

test('TextBee timestamped signature binds exact bytes and expires',async()=>{
  const raw='{"message":"test"}';const t=Math.floor(Date.now()/1000);const signed=await signature(raw,t);
  assert.equal(await verifyTextbee(SECRET,signed,raw),true);
  assert.equal(await verifyTextbee(SECRET,signed,raw+' '),false);
  assert.equal(await verifyTextbee(SECRET,signed,raw,(t+301)*1000),false);
  assert.equal(await verifyTextbee(SECRET,'legacy-signature',raw),false);
});

test('public routes never return sessions or accept an unsigned SMS',async()=>{
  const s=setup();
  await s.request('/v1/session','PUT',{phone_hash:HASH,cookie:COOKIE});
  let r=await worker.fetch(new Request('https://broker.test/v1/session'),s.env);
  assert.equal(r.status,401);assert.equal((await r.text()).includes(COOKIE),false);
  r=await worker.fetch(new Request('https://broker.test/webhooks/textbee',{method:'POST',body:JSON.stringify(s.message())}),s.env);
  assert.equal(r.status,401);
});

test('signed webhook acknowledges unrelated SMS without retaining its text',async()=>{
  const s=setup();const payload=s.message({message:'A private unrelated text',sender:'Unrelated'});const raw=JSON.stringify(payload);
  const signed=await signature(raw,Math.floor(Date.now()/1000));
  const r=await worker.fetch(new Request('https://broker.test/webhooks/textbee',{method:'POST',body:raw,headers:{'X-Textbee-Signature':signed}}),s.env);
  assert.equal(r.status,200);
  assert.equal(JSON.stringify([...s.data]).includes(payload.message),false);
});

test('SMS requests require a paired device and are throttled after failure',async()=>{
  const s=setup();assert.equal((await s.begin()).status,409);
  await s.pair();const start=await s.begin();assert.equal(start.status,201);
  assert.equal((await s.begin()).status,409);
  await s.request('/v1/login/'+start.request_id+'/fail','POST',{phone_hash:HASH});
  assert.equal((await s.begin()).status,429);
});

test('only fresh Patuljak codes for the selected device are accepted',()=>{
  const s=setup();const now=s.now();
  assert.equal(patuljakCode(s.message(),DEVICE,now,now).code,'123456');
  assert.equal(patuljakCode(s.message({deviceId:'another'}),DEVICE,now,now),null);
  assert.equal(patuljakCode(s.message({sender:'Bank',message:'Your bank code 123456'}),DEVICE,now,now),null);
  assert.equal(patuljakCode(s.message({receivedAt:new Date(now-120000).toISOString()}),DEVICE,now,now),null);
  assert.equal(patuljakCode(s.message({message:'Patuljak code 123456 or code 987654'}),DEVICE,now,now),null);
});

test('a code can be claimed once, then a verified session can be cached privately',async()=>{
  const s=setup();await s.pair();const start=await s.begin();
  const path='/v1/login/'+start.request_id;
  assert.equal((await s.request(path+'/code','POST',{phone_hash:HASH})).status,202);
  await s.request('/internal/textbee','POST',s.message());
  const replies=await Promise.all([s.request(path+'/code','POST',{phone_hash:HASH}),s.request(path+'/code','POST',{phone_hash:HASH})]);
  assert.deepEqual(replies.map(r=>r.status).sort(),[200,409]);
  const good=replies.find(r=>r.status===200);assert.equal((await good.json()).code,'123456');
  assert.equal(s.data.get('state').pending.otp,undefined);
  assert.equal(JSON.stringify(s.data.get('state')).includes('Vaš Patuljak kod'),false);
  assert.equal((await s.request(path+'/complete','POST',{phone_hash:HASH,cookie:COOKIE})).status,200);
  assert.equal((await (await s.request('/v1/session')).json()).cookie,COOKIE);
});

test('stale SMS retries cannot populate a new login attempt',async()=>{
  const s=setup();await s.pair();const start=await s.begin();const old=s.message();
  await s.request('/internal/textbee','POST',old);
  await s.request('/v1/login/'+start.request_id+'/fail','POST',{phone_hash:HASH});
  s.advance(16*60000);const next=await s.begin();assert.equal(next.status,201);
  await s.request('/internal/textbee','POST',old);
  assert.equal((await s.request('/v1/login/'+next.request_id+'/code','POST',{phone_hash:HASH})).status,202);
});

test('pending codes are deleted at TTL and never survive as session data',async()=>{
  const s=setup();await s.pair();const start=await s.begin();await s.request('/internal/textbee','POST',s.message());
  s.advance(301000);await s.obj.alarm();
  assert.equal(s.data.get('state').pending,undefined);
  assert.equal((await s.request('/v1/login/'+start.request_id+'/code','POST',{phone_hash:HASH})).status,410);
});

test('changing device requires new signed pairing and daily SMS attempts are capped',async()=>{
  const s=setup();await s.pair();s.env.TEXTBEE_DEVICE_ID='another';assert.equal((await s.begin()).status,409);
  s.env.TEXTBEE_DEVICE_ID=DEVICE;
  for(let i=0;i<3;i++){
    const start=await s.begin();assert.equal(start.status,201);
    await s.request('/v1/login/'+start.request_id+'/fail','POST',{phone_hash:HASH});s.advance(16*60000);
  }
  assert.equal((await s.begin()).status,429);
});

test('account binding and cookie validation reject mixing accounts or headers',async()=>{
  const s=setup();await s.request('/v1/session','PUT',{phone_hash:HASH,cookie:COOKIE});
  assert.equal((await s.request('/v1/session','PUT',{phone_hash:'b'.repeat(64),cookie:COOKIE})).status,409);
  assert.equal(validCookie(COOKIE),true);
  assert.equal(validCookie('PHPSESSID=ok\r\nX-Extra: injected'),false);
  assert.equal(validCookie('PHPSESSID=ok; third_party=secret'),false);
});
