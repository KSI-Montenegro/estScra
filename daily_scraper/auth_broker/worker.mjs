const ENC = new TextEncoder();
const TTL = 5 * 60_000;
const COOLDOWN = 15 * 60_000;
const SESSION_TTL = 7 * 86400_000;
const MAX_BODY = 16_384;

function json(value, status = 200) {
  return new Response(JSON.stringify(value), {status, headers: {
    'Content-Type': 'application/json', 'Cache-Control': 'no-store',
    'X-Content-Type-Options': 'nosniff',
  }});
}

async function bodyText(request) {
  if (Number(request.headers.get('content-length') || 0) > MAX_BODY) throw new Error('body_limit');
  if (!request.body) return '';
  const reader = request.body.getReader(); const chunks = []; let size = 0;
  try {
    while (true) {
      const {done, value} = await reader.read();
      if (done) break;
      size += value.byteLength;
      if (size > MAX_BODY) { await reader.cancel(); throw new Error('body_limit'); }
      chunks.push(value);
    }
  } finally { reader.releaseLock(); }
  const bytes = new Uint8Array(size); let offset = 0;
  for (const chunk of chunks) { bytes.set(chunk, offset); offset += chunk.byteLength; }
  return new TextDecoder('utf-8', {fatal: true}).decode(bytes);
}

export async function verifyTextbee(secret, header, raw, now = Date.now()) {
  if (typeof secret !== 'string' || secret.length < 20 || typeof header !== 'string') return false;
  const match = /^t=(\d{10}),v1=([a-fA-F0-9]{64})$/.exec(header.replace(/\s/g, ''));
  if (!match || Math.abs(now - Number(match[1]) * 1000) > TTL) return false;
  const digest = Uint8Array.from(match[2].match(/../g), part => parseInt(part, 16));
  const key = await crypto.subtle.importKey('raw', ENC.encode(secret), {name: 'HMAC', hash: 'SHA-256'}, false, ['verify']);
  return crypto.subtle.verify('HMAC', key, digest, ENC.encode(match[1] + '.' + raw));
}

export function patuljakCode(payload, expectedDevice, now, started) {
  if (!expectedDevice || payload?.webhookEvent !== 'MESSAGE_RECEIVED' || payload.deviceId !== expectedDevice) return null;
  if (typeof payload.smsId !== 'string' || !/^[A-Za-z0-9_-]{1,128}$/.test(payload.smsId)) return null;
  if (typeof payload.message !== 'string' || payload.message.length > 2000 || typeof payload.sender !== 'string') return null;
  const received = Date.parse(payload.receivedAt);
  if (!Number.isFinite(received) || received < started - 30_000 || received > now + 30_000 || now - received > TTL) return null;
  if (!/^patuljak(?:\.me)?$/i.test(payload.sender.trim()) && !/\bpatuljak(?:\.me)?\b/i.test(payload.message)) return null;
  const labelled = [...payload.message.matchAll(/(?:kod|code|pin|otp|šifra|sifra)\s*(?:je|is|:|-)?\s*[:\-]?\s*(\d{4,8})(?!\d)/giu)].map(m => m[1]);
  const candidates = labelled.length ? labelled : [...payload.message.matchAll(/(?<!\d)\d{4,8}(?!\d)/g)].map(m => m[0]);
  const codes = [...new Set(candidates)];
  return codes.length === 1 ? {code: codes[0], smsId: payload.smsId, receivedAt: received} : null;
}

export function validCookie(value) {
  if (typeof value !== 'string' || value.length > 4096 || /[\r\n]/.test(value)) return false;
  const pairs = value.split(';').map(v => v.trim());
  return pairs.some(v => v.startsWith('PHPSESSID=')) && pairs.every(v =>
    /^(?:PHPSESSID|5tg|id)=[A-Za-z0-9_.=+/-]+$/.test(v));
}

export class PatuljakAuth {
  constructor(ctx, env) { this.ctx = ctx; this.env = env; this.now = () => Date.now(); }
  async fetch(request) {
    return this.ctx.blockConcurrencyWhile(async () => {
      try { return await this.handle(request); }
      catch (error) { return json({error: 'broker_request_failed'}, error instanceof SyntaxError ? 400 : 503); }
    });
  }
  async handle(request) {
    const url = new URL(request.url); const now = this.now();
    const state = await this.ctx.storage.get('state') || {attempts: [], seen: []};
    state.attempts = state.attempts.filter(t => t > now - 86400_000);
    state.seen = state.seen.filter(s => s.at > now - 86400_000).slice(-128);
    if (state.pending?.expiresAt <= now) delete state.pending;
    if (state.session?.expiresAt <= now) delete state.session;
    const save = async () => {
      await this.ctx.storage.put('state', state);
      await this.ctx.storage.setAlarm(Math.min(now + TTL, state.pending?.expiresAt || Infinity, state.session?.expiresAt || Infinity));
    };
    if (url.pathname === '/internal/textbee' && request.method === 'POST') {
      const payload = await request.json();
      if (payload?.webhookEvent !== 'MESSAGE_RECEIVED' || !this.env.TEXTBEE_DEVICE_ID || payload.deviceId !== this.env.TEXTBEE_DEVICE_ID) {
        return json({received: true, matched: false});
      }
      const received = Date.parse(payload.receivedAt);
      if (!Number.isFinite(received) || Math.abs(now - received) > TTL) return json({received: true, matched: false});
      state.lastDeviceWebhookAt = now;
      state.lastDeviceWebhookDevice = this.env.TEXTBEE_DEVICE_ID;
      const pending = state.pending;
      const found = pending && !pending.claimed ? patuljakCode(payload, this.env.TEXTBEE_DEVICE_ID, now, pending.createdAt) : null;
      if (!found || pending.otp || state.seen.some(s => s.id === found.smsId)) {
        await save(); return json({received: true, matched: false});
      }
      pending.otp = found.code;
      state.seen.push({id: found.smsId, at: now});
      await save(); return json({received: true, matched: true});
    }
    if (url.pathname === '/v1/status' && request.method === 'GET') {
      await save();
      return json({configured: Boolean(this.env.TEXTBEE_DEVICE_ID),
        webhook_verified: Boolean(state.lastDeviceWebhookAt && state.lastDeviceWebhookDevice === this.env.TEXTBEE_DEVICE_ID),
        has_session: Boolean(state.session), pending: Boolean(state.pending),
        next_login_at: state.attempts.length ? state.attempts.at(-1) + COOLDOWN : 0});
    }
    if (url.pathname === '/v1/session' && request.method === 'GET') {
      await save();
      return json(state.session ? {cookie: state.session.cookie, phone_hash: state.phoneHash, expires_at: state.session.expiresAt} : {cookie: null});
    }
    const data = await request.json();
    if (!/^[a-f0-9]{64}$/.test(data.phone_hash || '')) return json({error: 'account_required'}, 400);
    if (state.phoneHash && state.phoneHash !== data.phone_hash) return json({error: 'account_mismatch'}, 409);
    if (url.pathname === '/v1/session' && request.method === 'PUT') {
      if (!validCookie(data.cookie)) return json({error: 'invalid_cookie'}, 400);
      state.phoneHash = data.phone_hash;
      state.session = {cookie: data.cookie, expiresAt: now + SESSION_TTL};
      await save(); return json({stored: true});
    }
    if (url.pathname === '/v1/login/begin' && request.method === 'POST') {
      if (!this.env.TEXTBEE_DEVICE_ID || !state.lastDeviceWebhookAt || state.lastDeviceWebhookDevice !== this.env.TEXTBEE_DEVICE_ID) {
        return json({error: 'textbee_pairing_required'}, 409);
      }
      if (state.pending) return json({error: 'login_already_pending', expires_at: state.pending.expiresAt}, 409);
      if (state.attempts.length >= 3 || (state.attempts.length && now-state.attempts.at(-1) < COOLDOWN)) {
        return json({error: 'login_cooldown'}, 429);
      }
      state.phoneHash = data.phone_hash;
      state.attempts.push(now);
      state.pending = {id: crypto.randomUUID(), createdAt: now, expiresAt: now + TTL, claimed: false};
      await save(); return json({request_id: state.pending.id, expires_at: state.pending.expiresAt}, 201);
    }
    const route = /^\/v1\/login\/([0-9a-f-]{36})\/(code|complete|fail)$/.exec(url.pathname);
    if (!route || request.method !== 'POST') return json({error: 'not_found'}, 404);
    if (!state.pending || state.pending.id !== route[1]) return json({error: 'login_expired_or_missing'}, 410);
    if (route[2] === 'code') {
      if (state.pending.claimed) return json({error: 'code_already_claimed'}, 409);
      if (!state.pending.otp) return json({waiting: true}, 202);
      const code = state.pending.otp; delete state.pending.otp; state.pending.claimed = true;
      await save(); return json({code});
    }
    if (route[2] === 'complete') {
      if (!state.pending.claimed || !validCookie(data.cookie)) return json({error: 'invalid_completion'}, 400);
      state.session = {cookie: data.cookie, expiresAt: now + SESSION_TTL};
    }
    delete state.pending;
    await save(); return json({completed: route[2] === 'complete'});
  }
  async alarm() {
    const state = await this.ctx.storage.get('state'); if (!state) return;
    const now = this.now();
    if (state.pending?.expiresAt <= now) delete state.pending;
    if (state.session?.expiresAt <= now) delete state.session;
    state.attempts = (state.attempts || []).filter(t => t > now - 86400_000);
    state.seen = (state.seen || []).filter(t => t.at > now - 86400_000);
    await this.ctx.storage.put('state', state);
    if (state.pending || state.session || state.attempts.length || state.seen.length) await this.ctx.storage.setAlarm(now + TTL);
  }
}

export default {
  async fetch(request, env) {
    const url = new URL(request.url);
    if (url.pathname === '/health' && request.method === 'GET') return json({service: 'graden-patuljak-auth', status: 'online'});
    try {
      if (url.pathname === '/webhooks/textbee' && request.method === 'POST') {
        const raw = await bodyText(request);
        if (!await verifyTextbee(env.TEXTBEE_WEBHOOK_SECRET, request.headers.get('X-Textbee-Signature'), raw)) return json({error: 'invalid_signature'}, 401);
        return env.AUTH_STATE.get(env.AUTH_STATE.idFromName('patuljak-primary')).fetch(new Request(
          'https://auth.internal/internal/textbee', {method: 'POST', body: raw, headers: {'Content-Type': 'application/json'}}));
      }
      if (!url.pathname.startsWith('/v1/') || !env.AUTH_CLIENT_KEY || request.headers.get('Authorization') !== 'Bearer ' + env.AUTH_CLIENT_KEY) {
        return json({error: 'unauthorized'}, 401);
      }
      if (!['GET', 'POST', 'PUT'].includes(request.method)) return json({error: 'method'}, 405);
      const raw = request.method === 'GET' ? undefined : await bodyText(request);
      return env.AUTH_STATE.get(env.AUTH_STATE.idFromName('patuljak-primary')).fetch(new Request(
        'https://auth.internal' + url.pathname, {method: request.method, body: raw, headers: {'Content-Type': 'application/json'}}));
    } catch { return json({error: 'invalid_request'}, 400); }
  },
};
