#!/usr/bin/env node
// Typecast connector for the 낭독 부스 artifact.
// A dependency-free MCP server (stdio, newline-delimited JSON-RPC) with two tools:
//   list_voices – the Typecast voices this API key can use
//   speak       – one sentence of Typecast speech as base64 MP3
// The API key is read from TYPECAST_API_KEY or from ../.env (TYPECAST_API_KEY=...).
// Generated audio is cached in ../.typecast-cache so replaying a sentence costs no credits.

import { createHash } from 'node:crypto';
import { existsSync, mkdirSync, readFileSync, writeFileSync } from 'node:fs';
import { dirname, join } from 'node:path';
import { fileURLToPath } from 'node:url';

const ROOT = join(dirname(fileURLToPath(import.meta.url)), '..');
const ENV_FILE = join(ROOT, '.env');
const CACHE_DIR = join(ROOT, '.typecast-cache');
const API = 'https://api.typecast.ai';
const MODELS = ['ssfm-v31', 'ssfm-v30', 'ssfm-v21'];
const PRESETS = ['normal', 'happy', 'sad', 'angry', 'whisper', 'toneup', 'tonedown'];

const log = (...a) => process.stderr.write(a.join(' ') + '\n');

function apiKey() {
  if (process.env.TYPECAST_API_KEY && process.env.TYPECAST_API_KEY.trim()) return process.env.TYPECAST_API_KEY.trim();
  if (!existsSync(ENV_FILE)) return '';
  for (const line of readFileSync(ENV_FILE, 'utf8').split(/\r?\n/)) {
    const m = line.match(/^\s*(?:export\s+)?TYPECAST_API_KEY\s*=\s*(.*)$/);
    if (m) return m[1].trim().replace(/^(['"])(.*)\1$/, '$2').trim();
  }
  return '';
}

class ToolError extends Error {
  constructor(code, message) { super(message); this.code = code; }
}

const STATUS = {
  400: ['bad_request', '요청 값이 올바르지 않아요.'],
  401: ['auth', 'Typecast API 키가 올바르지 않아요. .env 파일의 TYPECAST_API_KEY를 확인해 주세요.'],
  402: ['credits', 'Typecast 크레딧이 부족해요.'],
  403: ['forbidden', '이 API 키로는 사용할 수 없어요. 예전 Starter 키이거나 휴면 계정, 또는 키 앞뒤에 공백이 들어갔을 수 있어요.'],
  404: ['not_found', '목소리나 모델을 찾지 못했어요.'],
  422: ['invalid', '요청 값이 올바르지 않아요.'],
  429: ['rate_limited', '요청이 너무 많아요. 잠시 후 다시 시도해 주세요.'],
};

async function typecast(path, init = {}) {
  const key = apiKey();
  if (!key) throw new ToolError('no_key', `Typecast API 키가 없어요. ${ENV_FILE} 파일에 TYPECAST_API_KEY=키 를 한 줄로 적어 주세요.`);
  let res;
  try {
    res = await fetch(API + path, {
      ...init,
      headers: { 'X-API-KEY': key, ...(init.body ? { 'Content-Type': 'application/json' } : {}) },
      signal: AbortSignal.timeout(90_000),
    });
  } catch (e) {
    throw new ToolError('network', `Typecast 서버에 연결하지 못했어요 (${e.name === 'TimeoutError' ? '시간 초과' : e.message}).`);
  }
  if (!res.ok) {
    const body = (await res.text().catch(() => '')).slice(0, 400);
    const [code, msg] = STATUS[res.status] || ['server', `Typecast 서버 오류 (${res.status})`];
    throw new ToolError(code, body && (code === 'bad_request' || code === 'invalid') ? `${msg} ${body}` : msg);
  }
  return res;
}

async function listVoices({ model, gender } = {}) {
  const q = new URLSearchParams();
  if (MODELS.includes(model)) q.set('model', model);
  if (gender === 'male' || gender === 'female') q.set('gender', gender);
  const res = await typecast('/v3/voices' + (q.size ? '?' + q : ''));
  const body = await res.json();
  // Documented as a bare array; accept a wrapped list too.
  const list = Array.isArray(body) ? body : (body && (body.voices || body.items || body.data)) || [];
  return {
    voices: list.map((v) => ({
      id: v.voice_id,
      name: (v.voice_name && (v.voice_name.kor || v.voice_name.eng)) || v.voice_id,
      name_eng: (v.voice_name && v.voice_name.eng) || '',
      gender: v.gender || null,
      age: v.age || null,
      use_cases: v.use_cases || [],
      custom: v.voice_type === 'custom',
      models: (v.models || []).map((m) => ({ version: m.version, emotions: m.emotions || [] })),
    })),
  };
}

const clamp = (v, lo, hi) => Math.min(hi, Math.max(lo, v));

async function speak(input) {
  const text = String(input.text || '').trim();
  if (!text) throw new ToolError('bad_request', '읽을 문장이 비어 있어요.');
  if (text.length > 2000) throw new ToolError('bad_request', '한 번에 2000자까지만 보낼 수 있어요.');
  if (!/^(tc|uc)_/.test(String(input.voice_id || ''))) throw new ToolError('bad_request', 'Typecast 목소리를 먼저 골라 주세요.');
  const model = MODELS.includes(input.model) ? input.model : 'ssfm-v31';

  const body = { voice_id: input.voice_id, text, model, language: input.language || 'kor' };
  const emotion = String(input.emotion || 'smart');
  if (emotion === 'smart' && model !== 'ssfm-v21') {
    body.prompt = { emotion_type: 'smart' };
    if (input.previous_text) body.prompt.previous_text = String(input.previous_text).slice(0, 2000);
    if (input.next_text) body.prompt.next_text = String(input.next_text).slice(0, 2000);
  } else if (PRESETS.includes(emotion)) {
    const intensity = clamp(Number(input.emotion_intensity ?? 1), 0, 2);
    body.prompt = model === 'ssfm-v21'
      ? { emotion_preset: emotion, emotion_intensity: intensity }
      : { emotion_type: 'preset', emotion_preset: emotion, emotion_intensity: intensity };
  }
  body.output = {
    audio_format: 'mp3',
    audio_tempo: clamp(Number(input.audio_tempo ?? 1), 0.5, 2),
    audio_pitch: Math.round(clamp(Number(input.audio_pitch ?? 0), -12, 12)),
  };

  const hash = createHash('sha256').update(JSON.stringify(body)).digest('hex').slice(0, 32);
  const file = join(CACHE_DIR, hash + '.mp3');
  if (existsSync(file)) {
    return { mime: 'audio/mpeg', audio_base64: readFileSync(file).toString('base64'), chars: text.length, cached: true };
  }
  const res = await typecast('/v1/text-to-speech', { method: 'POST', body: JSON.stringify(body) });
  const buf = Buffer.from(await res.arrayBuffer());
  try { mkdirSync(CACHE_DIR, { recursive: true }); writeFileSync(file, buf); } catch (e) { log('cache write failed:', e.message); }
  return { mime: res.headers.get('content-type') || 'audio/mpeg', audio_base64: buf.toString('base64'), chars: text.length, cached: false };
}

const TOOLS = [
  {
    name: 'list_voices',
    description: 'List the Typecast voices available to this API key (custom voices first).',
    inputSchema: {
      type: 'object',
      properties: {
        model: { type: 'string', enum: MODELS, description: 'Only voices that support this model.' },
        gender: { type: 'string', enum: ['male', 'female'] },
      },
    },
    annotations: { title: 'Typecast 목소리 목록', readOnlyHint: true, openWorldHint: true },
  },
  {
    name: 'speak',
    description: 'Generate Typecast speech for one sentence and return it as base64 MP3. New text uses Typecast credits (1 per character); repeated requests are served from the local cache.',
    inputSchema: {
      type: 'object',
      required: ['text', 'voice_id'],
      properties: {
        text: { type: 'string', maxLength: 2000 },
        voice_id: { type: 'string', description: 'tc_… or uc_… id from list_voices' },
        model: { type: 'string', enum: MODELS },
        language: { type: 'string', description: 'ISO 639-3, default kor' },
        emotion: { type: 'string', enum: ['smart', ...PRESETS], description: 'smart = infer from previous_text/next_text' },
        emotion_intensity: { type: 'number', minimum: 0, maximum: 2 },
        previous_text: { type: 'string' },
        next_text: { type: 'string' },
        audio_tempo: { type: 'number', minimum: 0.5, maximum: 2 },
        audio_pitch: { type: 'integer', minimum: -12, maximum: 12 },
      },
    },
    // Generates audio only; it changes no data, so the app need not confirm every sentence.
    annotations: { title: 'Typecast로 읽기', readOnlyHint: true, destructiveHint: false, openWorldHint: true },
  },
];

const HANDLERS = { list_voices: listVoices, speak };

async function handle(msg) {
  const { id, method, params } = msg;
  switch (method) {
    case 'initialize':
      return {
        protocolVersion: (params && params.protocolVersion) || '2025-06-18',
        capabilities: { tools: {} },
        serverInfo: { name: 'typecast', version: '1.0.0' },
      };
    case 'ping':
      return {};
    case 'tools/list':
      return { tools: TOOLS };
    case 'tools/call': {
      const fn = HANDLERS[params && params.name];
      if (!fn) throw Object.assign(new Error(`Unknown tool: ${params && params.name}`), { rpc: -32602 });
      try {
        const out = await fn((params && params.arguments) || {});
        return { content: [{ type: 'text', text: JSON.stringify(out) }] };
      } catch (e) {
        const code = e instanceof ToolError ? e.code : 'internal';
        if (!(e instanceof ToolError)) log('tool failed:', e.stack || e.message);
        return { content: [{ type: 'text', text: JSON.stringify({ error: code, message: e.message }) }], isError: true };
      }
    }
    default:
      if (id === undefined) return undefined; // notification
      throw Object.assign(new Error(`Method not found: ${method}`), { rpc: -32601 });
  }
}

function send(obj) { process.stdout.write(JSON.stringify(obj) + '\n'); }

let buffer = '';
process.stdin.setEncoding('utf8');
process.stdin.on('data', (chunk) => {
  buffer += chunk;
  let nl;
  while ((nl = buffer.indexOf('\n')) >= 0) {
    const line = buffer.slice(0, nl).trim();
    buffer = buffer.slice(nl + 1);
    if (!line) continue;
    let msg;
    try { msg = JSON.parse(line); } catch { send({ jsonrpc: '2.0', id: null, error: { code: -32700, message: 'Parse error' } }); continue; }
    Promise.resolve(handle(msg)).then(
      (result) => { if (msg.id !== undefined && result !== undefined) send({ jsonrpc: '2.0', id: msg.id, result }); },
      (err) => { if (msg.id !== undefined) send({ jsonrpc: '2.0', id: msg.id, error: { code: err.rpc || -32603, message: err.message } }); },
    );
  }
});
process.stdin.on('end', () => process.exit(0));
