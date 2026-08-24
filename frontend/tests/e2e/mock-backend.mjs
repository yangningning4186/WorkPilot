/**
 * 前端验收用的假后端。
 *
 * 为什么不用真后端跑验收：真链路要 Postgres + 本地推理服务 + 已入库语料，
 * CI runner 连不到自建集群（docs/09 W5 里同一条硬约束），而且 LLM 输出不确定，
 * 断言只能写得很松。这里把 run 事件流按剧本回放，把"提问 → SSE → 引用 → 高亮"
 * 这条前端闭环变成确定性用例。
 *
 * SSE 帧格式与 `app/services/run_stream.py::format_sse` 完全一致：
 *     id: <run_id>:<seq>\nevent: <type>\ndata: <json>\n\n
 * Last-Event-ID 优先于 after_seq 查询参数，也与后端一致——B2 断线续传就靠这条。
 *
 * 用法：node tests/e2e/mock-backend.mjs   （端口 MOCK_BACKEND_PORT，默认 8787）
 */

import { createServer } from "node:http";
import { deflateSync } from "node:zlib";

import {
  IDS,
  LIBRARY,
  REVIEW_OUTPUT_PATH,
  REVIEW_RESUME_TOKEN,
  MD_FILE_CONTENT,
  PAGE,
  SCENARIOS,
  pickScenario,
} from "./fixtures/scenarios.mjs";

const PORT = Number(process.env.MOCK_BACKEND_PORT ?? 8787);
const READER_PAGE_PNG = Buffer.from(
  "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mNk+A8AAQUBAScY42YAAAAASUVORK5CYII=",
  "base64",
);

/**
 * 两页、每页一句英文的真 PDF。
 *
 * 必须是真能被 pdf.js 解析的字节，不能是占位符：阅读器现在的主路径是"取原始 PDF →
 * 画布 + 文本层"，喂一个假文件只会让它一路退回旧的 PNG 分支，于是这条验收永远测不到
 * 真正在跑的那条路。
 */
const READER_PDF = Buffer.from(
  "JVBERi0xLjcKJcK1wrYKJSBXcml0dGVuIGJ5IE11UERGIDEuMjguMgoKMSAwIG9iago8PC9UeXBlL0NhdGFsb2cvUGFnZXMgMiAwIFIvSW5mbzw8L1Byb2R1Y2VyKE11UERGIDEuMjguMik+Pj4+CmVuZG9iagoKMiAwIG9iago8PC9UeXBlL1BhZ2VzL0NvdW50IDIvS2lkc1s0IDAgUiA4IDAgUl0+PgplbmRvYmoKCjMgMCBvYmoKPDwvRm9udDw8L2hlbHYgNSAwIFI+Pj4+CmVuZG9iagoKNCAwIG9iago8PC9UeXBlL1BhZ2UvTWVkaWFCb3hbMCAwIDMwNiAzOTZdL1JvdGF0ZSAwL1Jlc291cmNlcyAzIDAgUi9QYXJlbnQgMiAwIFIvQ29udGVudHNbNiAwIFJdPj4KZW5kb2JqCgo1IDAgb2JqCjw8L1R5cGUvRm9udC9TdWJ0eXBlL1R5cGUxL0Jhc2VGb250L0hlbHZldGljYS9FbmNvZGluZy9XaW5BbnNpRW5jb2Rpbmc+PgplbmRvYmoKCjYgMCBvYmoKPDwvTGVuZ3RoIDk1L0ZpbHRlci9GbGF0ZURlY29kZT4+CnN0cmVhbQp42g2KsQqAMAxE93xF/sCmTS8I4iC4uAndxKm0OOjg4vcbDh73jqOXlkLCwSOcwCkql4eGq90fi/fOx6RiaoqM5hzR0WLAaMkpqKgxmK+W3Zu/MnQ+y0ZroZ1+NY8WSQplbmRzdHJlYW0KZW5kb2JqCgo3IDAgb2JqCjw8L0ZvbnQ8PC9oZWx2IDUgMCBSPj4+PgplbmRvYmoKCjggMCBvYmoKPDwvVHlwZS9QYWdlL01lZGlhQm94WzAgMCAzMDYgMzk2XS9Sb3RhdGUgMC9SZXNvdXJjZXMgNyAwIFIvUGFyZW50IDIgMCBSL0NvbnRlbnRzWzkgMCBSXT4+CmVuZG9iagoKOSAwIG9iago8PC9MZW5ndGggOTUvRmlsdGVyL0ZsYXRlRGVjb2RlPj4Kc3RyZWFtCnja4yrkcgrhMlQwAEJDBWMzBWMjE4WQXC79jNScMgVDIDtNIdrG1MAszdzYzNLcxMzSLM0s1czQLNnIwMwUyDIG8kGiqWbmRgZANaZmxjBVdrEhXlyuIVyBXACfMxeUCmVuZHN0cmVhbQplbmRvYmoKCnhyZWYKMCAxMAowMDAwMDAwMDAwIDY1NTM1IGYgCjAwMDAwMDAwNDIgMDAwMDAgbiAKMDAwMDAwMDEyMCAwMDAwMCBuIAowMDAwMDAwMTc4IDAwMDAwIG4gCjAwMDAwMDAyMTkgMDAwMDAgbiAKMDAwMDAwMDMyNiAwMDAwMCBuIAowMDAwMDAwNDE1IDAwMDAwIG4gCjAwMDAwMDA1NzggMDAwMDAgbiAKMDAwMDAwMDYxOSAwMDAwMCBuIAowMDAwMDAwNzI2IDAwMDAwIG4gCgp0cmFpbGVyCjw8L1NpemUgMTAvUm9vdCAxIDAgUi9JRFs8MTgyMkMzODRDMzgzMUE3NEMzODMwMEMyQTQ1NjQyQzI+PEFENjMwMkM3MUNCQjAzNEZBNDlGRDRDNjZENkJBNTMwPl0+PgpzdGFydHhyZWYKODg5CiUlRU9GCg==",
  "base64",
);

/** run_id → 运行态。进程内存即可，验收进程和被测进程都是一次性的。 */
const runs = new Map();
/** 匿名 session token → 会话拥有的 conversation 与已引用文档版本。 */
const sessions = new Map();
/** 供测试断言的请求流水（例如"取消接口确实被调用过"）。 */
const requestLog = [];
/** 已签发的 admin token。真后端存在 Redis，这里内存等价。 */
const adminTokens = new Set();
/** owner Cowork 会话的目录权限与交付物。 */
const coworkRoots = new Map();
const coworkArtifacts = new Map();
const coworkEventLogs = new Map();
/** conversation_id → 当前会话挂载的知识库；run 创建时会把这个值冻结进 run。 */
const conversationKnowledgeBases = new Map();
/** 后端是否配了 DEMO_ADMIN_PASSWORD_HASH；关掉用来验收 503 的提示文案。 */
let adminConfigured = true;

let memoryCounter = 10;
let memories = [];

function nextMemoryId() {
  memoryCounter += 1;
  return `6d0e4c55-0000-4d00-8000-${String(memoryCounter).padStart(12, "0")}`;
}

function memoryRecord(overrides) {
  const now = "2026-08-18T01:30:00Z";
  return {
    id: nextMemoryId(),
    category: "preference",
    fact: "偏好简洁回答",
    valid_from: now,
    invalid_at: null,
    superseded_by: null,
    source_type: "conversation",
    source_message_id: "5d0e4c55-0000-4d00-8000-000000000001",
    confidence: 0.94,
    access_count: 3,
    last_used_at: "2026-08-18T02:10:00Z",
    pinned: false,
    created_at: now,
    updated_at: now,
    ...overrides,
  };
}

function resetMemories() {
  memoryCounter = 10;
  memories = [
    memoryRecord({ fact: "回答时先给结论，再补充依据", pinned: true, access_count: 8 }),
    memoryRecord({ category: "profile", fact: "正在开发 WorkPilot", access_count: 2 }),
    memoryRecord({ category: "interest", fact: "关注 RAG 与智能体评测", access_count: 5 }),
    memoryRecord({
      category: "preference",
      fact: "偏好非常详细的回答",
      valid_from: "2026-08-01T09:00:00Z",
      invalid_at: "2026-08-12T11:20:00Z",
      superseded_by: null,
      access_count: 1,
      last_used_at: "2026-08-10T10:00:00Z",
    }),
  ];
}

resetMemories();

const ADMIN_PASSWORD = "demo-admin-pw";

let runCounter = 0;
let sessionCounter = 0;
let conversationCounter = 0;
let adminCounter = 0;

function nextRunId() {
  runCounter += 1;
  return `7d0e4c55-0000-4d00-8000-${String(runCounter).padStart(12, "0")}`;
}

function json(response, status, body, headers = {}) {
  const payload = JSON.stringify(body);
  response.writeHead(status, {
    "content-type": "application/json; charset=utf-8",
    "cache-control": "no-store",
    ...headers,
  });
  response.end(payload);
}

function cookies(request) {
  return Object.fromEntries(
    (request.headers.cookie ?? "")
      .split(";")
      .map((item) => item.trim().split("="))
      .filter(([name, value]) => name && value),
  );
}

function currentSession(request) {
  // 桌面端不用浏览器 cookie；真实 sidecar 在启动令牌通过后直接落到同一个 owner store。
  // 假后端也必须把该令牌映射到进程内的唯一会话，否则首个 POST 建出的 conversation
  // 会在紧接着的 PUT 中凭空“消失”。
  if (request.headers["x-workpilot-launch-token"] === "e2e-desktop-token") {
    return firstSession();
  }
  const token = cookies(request).workpilot_session;
  const session = token === undefined ? undefined : sessions.get(token);
  return session === undefined ? null : { token, ...session };
}

function firstSession() {
  const first = sessions.entries().next().value;
  return first === undefined ? null : { token: first[0], ...first[1] };
}

/** Web 认 httpOnly cookie；桌面端认每次启动注入的 launch token。 */
function isAdmin(request) {
  const token = cookies(request).workpilot_admin_session;
  return (token !== undefined && adminTokens.has(token))
    || request.headers["x-workpilot-launch-token"] === "e2e-desktop-token";
}

function createSession() {
  sessionCounter += 1;
  const suffix = String(sessionCounter).padStart(12, "0");
  const token = `mock-session-${suffix}`;
  const conversationId = nextConversationId();
  const session = {
    conversation_id: conversationId,
    conversations: new Map([
      [conversationId, {
        id: conversationId,
        title: null,
        provider_profile_id: "mock-provider",
        provider_name: "本地测试模型",
        provider: "openai",
        selected_model: "mock-cowork",
        unattended: false,
        approval_mode: "interactive",
        persona_name: "general",
        archived_at: null,
        created_at: new Date().toISOString(),
      }],
    ]),
    versions: new Set(),
  };
  sessions.set(token, session);
  return { token, ...session };
}

function nextConversationId() {
  conversationCounter += 1;
  return `9b0e4c55-0000-4d00-8000-${String(conversationCounter).padStart(12, "0")}`;
}

function conversationMessages(session, conversationId) {
  const items = [];
  for (const run of runs.values()) {
    if (run.session_token !== session.token || run.conversation_id !== conversationId) continue;
    const baseSeq = items.length + 1;
    items.push({
      id: `${run.id}-user`,
      seq: baseSeq,
      role: "user",
      content: run.goal,
      status: "completed",
      run_id: run.id,
      attachments: [],
      citations: [],
      answer_mode: run.answer_mode,
      created_at: "2026-08-18T01:00:00Z",
    });
    if (run.workflow_type === "cowork") {
      const events = coworkEventLogs.get(run.id) ?? [];
      const done = events.findIndex((event) => event.type === "message.done") + 1;
      if (done > 0 && run.last_sent_seq >= done) {
        const snapshots = events
          .filter((event, index) => index < done && event.type === "message.snapshot")
          .map((event) => event.data.text);
        const content = snapshots.at(-1) ?? events
          .filter((event, index) => index < done && event.type === "message.delta")
          .map((event) => event.data.text)
          .join("");
        items.push({
          id: `${run.id}-assistant`,
          seq: baseSeq + 1,
          role: "assistant",
          content,
          status: "completed",
          run_id: run.id,
          attachments: [],
          citations: [],
          answer_mode: run.answer_mode,
          created_at: "2026-08-18T01:00:01Z",
        });
      }
      continue;
    }
    const scenario = SCENARIOS[run.scenario];
    const done = scenario.events.findIndex((event) => event.type === "message.done") + 1;
    if (done > 0 && run.last_sent_seq >= done) {
      const content = scenario.events
        .filter((event, index) => index < done && event.type === "message.delta")
        .map((event) => event.data.text)
        .join("");
      const citations = scenario.events
        .filter((event, index) => index < done && event.type === "citation")
        .map((event) => event.data);
      items.push({
        id: `${run.id}-assistant`,
        seq: baseSeq + 1,
        role: "assistant",
        content,
        status: "completed",
        run_id: run.id,
        attachments: [],
        citations,
        answer_mode: run.answer_mode,
        created_at: "2026-08-18T01:00:01Z",
      });
    }
  }
  return items;
}

async function readBody(request) {
  const chunks = [];
  for await (const chunk of request) chunks.push(chunk);
  if (chunks.length === 0) return {};
  try {
    return JSON.parse(Buffer.concat(chunks).toString("utf8"));
  } catch {
    return {};
  }
}

const sleep = (ms) => new Promise((resolve) => setTimeout(resolve, ms));

// ---------------------------------------------------------------- PNG 生成
// 不往仓库里塞二进制素材：页面预览图当场画。图片本身内容不重要，
// 重要的是它能真的 onLoad——前端要等 imageReady 才渲染 bbox 高亮。

const CRC_TABLE = Array.from({ length: 256 }, (_, index) => {
  let value = index;
  for (let bit = 0; bit < 8; bit += 1) {
    value = value & 1 ? 0xedb88320 ^ (value >>> 1) : value >>> 1;
  }
  return value >>> 0;
});

function crc32(buffer) {
  let crc = 0xffffffff;
  for (const byte of buffer) crc = CRC_TABLE[(crc ^ byte) & 0xff] ^ (crc >>> 8);
  return (crc ^ 0xffffffff) >>> 0;
}

function pngChunk(type, data) {
  const length = Buffer.alloc(4);
  length.writeUInt32BE(data.length);
  const typed = Buffer.concat([Buffer.from(type, "ascii"), data]);
  const crc = Buffer.alloc(4);
  crc.writeUInt32BE(crc32(typed));
  return Buffer.concat([length, typed, crc]);
}

function renderPagePng(width, height, pageNo) {
  const raw = Buffer.alloc((width * 3 + 1) * height);
  for (let y = 0; y < height; y += 1) {
    const rowStart = y * (width * 3 + 1);
    raw[rowStart] = 0; // filter: none
    for (let x = 0; x < width; x += 1) {
      // 每页画一条不同高度的深色横带，肉眼一看截图就知道翻页生效了。
      const band = Math.abs(y - ((pageNo * 97) % height)) < height * 0.06;
      const value = band ? 0x99 : 0xf2;
      const offset = rowStart + 1 + x * 3;
      raw[offset] = value;
      raw[offset + 1] = value;
      raw[offset + 2] = value;
    }
  }
  const ihdr = Buffer.alloc(13);
  ihdr.writeUInt32BE(width, 0);
  ihdr.writeUInt32BE(height, 4);
  ihdr[8] = 8; // bit depth
  ihdr[9] = 2; // color type: truecolor
  return Buffer.concat([
    Buffer.from([0x89, 0x50, 0x4e, 0x47, 0x0d, 0x0a, 0x1a, 0x0a]),
    pngChunk("IHDR", ihdr),
    pngChunk("IDAT", deflateSync(raw)),
    pngChunk("IEND", Buffer.alloc(0)),
  ]);
}

// ---------------------------------------------------------------- SSE 回放

function sseFrame(runId, seq, type, data) {
  const envelope = {
    id: `${runId}:${seq}`,
    run_id: runId,
    // seq 是字符串：后端 BIGINT，前端必须走 BigInt（docs/08 §3.2）。
    seq: String(seq),
    type,
    data,
  };
  return `id: ${runId}:${seq}\nevent: ${type}\ndata: ${JSON.stringify(envelope)}\n\n`;
}

function parseLastEventId(header) {
  if (typeof header !== "string" || !header.includes(":")) return null;
  const seq = Number.parseInt(header.slice(header.lastIndexOf(":") + 1), 10);
  return Number.isInteger(seq) && seq >= 0 ? seq : null;
}

async function streamEvents(request, response, run) {
  const url = new URL(request.url, "http://mock");
  const fromQuery = Number.parseInt(url.searchParams.get("after_seq") ?? "0", 10);
  const scenario = SCENARIOS[run.scenario];
  // replay 场景刻意无视续传游标，从头重发：用来验证前端自己的 seq 去重。
  const cursor = scenario.replay_on_reconnect
    ? 0
    : (parseLastEventId(request.headers["last-event-id"]) ?? (fromQuery || 0));

  response.writeHead(200, {
    "content-type": "text/event-stream; charset=utf-8",
    "cache-control": "no-cache, no-transform",
    connection: "keep-alive",
    "x-accel-buffering": "no", // 别让中间层缓冲，缓冲了 delta 就不是流式的了
  });
  // 仅 mock 有：把客户端重连间隔压到 250ms，断线用例才不用干等。
  response.write("retry: 250\n\n");

  let closed = false;
  response.on("close", () => {
    closed = true;
  });

  for (const [index, event] of scenario.events.entries()) {
    const seq = index + 1;
    if (closed) return;
    if (seq <= cursor) continue; // 续传：已经看过的事件不重发

    // 已经产生过的事件是"历史"，立刻补发不再等 delay——与后端"先补历史再续实时流"
    // 的顺序一致（run_stream.py），刷新恢复也因此不用把整段生成重演一遍。
    if (seq > run.last_sent_seq) await sleep(event.delay_ms);
    if (closed) return;
    response.write(sseFrame(run.id, seq, event.type, event.data));
    run.last_sent_seq = Math.max(run.last_sent_seq, seq);
    if (event.type === "citation" && typeof event.data.version_id === "string") {
      sessions.get(run.session_token)?.versions.add(event.data.version_id);
    }

    if (scenario.drop_after_seq === seq && !run.dropped) {
      // 掐断且不发终态：模拟网络断开，客户端会带 after_seq 游标重连。
      run.dropped = true;
      response.destroy();
      return;
    }

    if (scenario.stall_after_seq === seq) {
      while (!run.cancelled && !closed) {
        await sleep(50);
        response.write(": keepalive\n\n"); // 与后端保活注释帧一致
      }
      if (closed) return;
      const cancelEvent = scenario.cancel_event;
      response.write(sseFrame(run.id, seq + 1, cancelEvent.type, cancelEvent.data));
      run.status = "cancelled";
      response.end();
      return;
    }
  }

  // 固定综述停在人工确认点时连接不能断：真后端此时 run 仍是 waiting_human，
  // 客户端要挂在同一条流上等批准/拒绝的结果。断开再重连也能续，但那会让
  // "确认后立刻看到写回结果"变成一次竞态。
  if (run.workflow_type === "literature_review") {
    let seq = scenario.events.length;
    while (!closed) {
      const pending = run.pending_events ?? [];
      if (pending.length > 0) {
        run.pending_events = [];
        for (const event of pending) {
          if (closed) return;
          await sleep(event.delay_ms);
          seq += 1;
          response.write(sseFrame(run.id, seq, event.type, event.data));
          run.last_sent_seq = Math.max(run.last_sent_seq, seq);
        }
        run.status = "done";
        response.end();
        return;
      }
      await sleep(50);
      response.write(": keepalive\n\n");
    }
    return;
  }

  run.status = "done";
  response.end();
}

async function streamCoworkEvents(request, response, run) {
  const url = new URL(request.url, "http://mock");
  let cursor = run.cowork_replay_on_reconnect
    ? 0
    : Number.parseInt(url.searchParams.get("after_seq") ?? "0", 10) || 0;
  response.writeHead(200, {
    "content-type": "text/event-stream; charset=utf-8",
    "cache-control": "no-cache, no-transform",
    connection: "keep-alive",
    "x-accel-buffering": "no",
  });
  response.write("retry: 250\n\n");
  let closed = false;
  response.on("close", () => { closed = true; });
  while (!closed) {
    const events = coworkEventLogs.get(run.id) ?? [];
    while (!closed && cursor < events.length) {
      const event = events[cursor];
      // 已经发过的是历史回放，应立即补齐；只有新产生的事件保留剧本时序。
      if (cursor >= run.last_sent_seq) await sleep(event.delay_ms ?? 12);
      if (closed) return;
      cursor += 1;
      response.write(sseFrame(run.id, cursor, event.type, event.data));
      run.last_sent_seq = Math.max(run.last_sent_seq, cursor);
      if (event.type === "citation" && typeof event.data.version_id === "string") {
        sessions.get(run.session_token)?.versions.add(event.data.version_id);
      }
      if (run.cowork_drop_after_seq === cursor && !run.dropped) {
        run.dropped = true;
        response.destroy();
        return;
      }
      if (event.type === "run.done") {
        run.status = event.data.status ?? "done";
        response.end();
        return;
      }
      if (event.type === "error") {
        run.status = event.data.code === "cancelled" ? "cancelled" : "failed";
        response.end();
        return;
      }
    }
    await sleep(40);
    if (!closed) response.write(": keepalive\n\n");
  }
}

/** 已下线的独立 RAG 页面用例迁到 Cowork 后，仍复用这些确定性事件剧本。 */
function coworkFixtureScenario(goal) {
  if (goal.includes("这段回答的排版")) return "markdownRender";
  if (goal.includes("思维链不进入正文")) return "reasoningLeak";
  if (goal.includes("会在中途断线")) return "drop";
  if (goal.includes("重连后会重放")) return "replay";
  if (goal.includes("会报错")) return "error";
  if (goal.includes("markdown 笔记")) return "markdown";
  if (goal.includes("混合检索为什么比单路召回")) return "pdf";
  return null;
}

// ---------------------------------------------------------------- 路由

const server = createServer((request, response) => {
  const url = new URL(request.url ?? "/", "http://mock");
  const path = url.pathname;
  requestLog.push({ method: request.method ?? "GET", path, search: url.search });

  if (path === "/__health") {
    json(response, 200, { status: "ok" });
    return;
  }

  if (path === "/__requests") {
    json(response, 200, { requests: requestLog });
    return;
  }

  if (path === "/__runs") {
    json(response, 200, {
      runs: [...runs.values()].map((run) => ({
        id: run.id,
        conversation_id: run.conversation_id,
        status: run.status,
        kb_slug: run.kb_slug ?? null,
        workspace_path: run.workspace_path ?? null,
        workspace_files: run.workspace_files ?? null,
      })),
    });
    return;
  }

  if (path === "/__reset" && request.method === "POST") {
    runs.clear();
    sessions.clear();
    conversationCounter = 0;
    adminTokens.clear();
    coworkRoots.clear();
    coworkArtifacts.clear();
    coworkEventLogs.clear();
    conversationKnowledgeBases.clear();
    adminConfigured = true;
    resetMemories();
    requestLog.length = 0;
    json(response, 200, { status: "reset" });
    return;
  }

  // 单独开关，不连带清空 runs：用例改完要能原样改回去，免得污染后续用例。
  if (path === "/__admin_configured" && request.method === "POST") {
    adminConfigured = url.searchParams.get("value") !== "false";
    json(response, 200, { admin_configured: adminConfigured });
    return;
  }

  // ------------------------------------------------------------ admin 会话
  // 写操作（创建综述、批准写回、触发同步）在真后端都挂着 require_admin_session，
  // 假后端必须照抄这个门，否则验收测的是一个不存在的宽松后端。

  if (path === "/api/v1/auth/admin/session" && request.method === "GET") {
    if (isAdmin(request)) {
      json(response, 200, { authenticated: true });
    } else {
      json(response, 401, { detail: "需要 admin 登录" });
    }
    return;
  }

  if (path === "/api/v1/auth/admin/login" && request.method === "POST") {
    void readBody(request).then((body) => {
      if (!adminConfigured) {
        json(response, 503, { detail: "demo admin 尚未配置" });
        return;
      }
      if (body.password !== ADMIN_PASSWORD) {
        json(response, 401, { detail: "密码错误" });
        return;
      }
      adminCounter += 1;
      const token = `mock-admin-${String(adminCounter).padStart(12, "0")}`;
      adminTokens.add(token);
      json(
        response,
        200,
        { authenticated: true },
        {
          "set-cookie": `workpilot_admin_session=${token}; Max-Age=28800; Path=/; HttpOnly; SameSite=Lax`,
        },
      );
    });
    return;
  }

  if (path === "/api/v1/auth/admin/logout" && request.method === "POST") {
    const token = cookies(request).workpilot_admin_session;
    if (token !== undefined) adminTokens.delete(token);
    response.writeHead(204, {
      "set-cookie": "workpilot_admin_session=; Max-Age=0; Path=/; HttpOnly; SameSite=Lax",
    });
    response.end();
    return;
  }

  // ------------------------------------------------------------ 多轮会话

  if (path === "/api/v1/providers" && request.method === "GET") {
    const now = "2026-08-18T03:00:00Z";
    json(response, 200, {
      items: [
        {
          id: "mock-provider",
          name: "本地测试模型",
          provider: "openai",
          base_url: "http://127.0.0.1:9999/v1",
          default_model: "mock-cowork",
          context_window_tokens: 128000,
          enabled: true,
          has_api_key: true,
          metadata: {},
          created_at: now,
          updated_at: now,
        },
      ],
    });
    return;
  }

  if (path === "/api/v1/conversations" && request.method === "GET") {
    const existing = currentSession(request);
    // 页面会并行请求活跃与归档列表。首屏还没有 cookie 时，两条请求必须共用同一个
    // 匿名 session；各建一份会让后返回的 Set-Cookie 指向另一份会话，后续加载即 404。
    const session = existing ?? firstSession() ?? createSession();
    const items = [...session.conversations.values()].map((conversation) => {
      const messages = conversationMessages(session, conversation.id);
      return {
        ...conversation,
        active_run_id: ([...runs.values()].find((run) =>
          run.conversation_id === conversation.id
          && ["queued", "executing", "waiting_human", "sleeping"].includes(run.status)
        )?.id ?? null),
        message_count: messages.length,
        latest_message: messages.at(-1)?.content ?? null,
        last_message_at: messages.at(-1)?.created_at ?? null,
        updated_at: messages.at(-1)?.created_at ?? conversation.created_at,
      };
    });
    json(
      response,
      200,
      { items: items.reverse(), total: items.length },
      existing === null
        ? {
            "set-cookie": `workpilot_session=${session.token}; Max-Age=1800; Path=/; HttpOnly; SameSite=Lax`,
          }
        : {},
    );
    return;
  }

  if (path === "/api/v1/conversations" && request.method === "POST") {
    void readBody(request).then((body) => {
      const existing = currentSession(request);
      const session = existing ?? createSession();
      const id = nextConversationId();
      const createdAt = new Date().toISOString();
      const created = {
        id,
        title: typeof body.title === "string" ? body.title : "新会话",
        active_run_id: null,
        message_count: 0,
        latest_message: null,
        last_message_at: null,
        provider_profile_id: null,
        provider_name: null,
        provider: null,
        selected_model: null,
        unattended: false,
        approval_mode: "interactive",
        persona_name: "general",
        archived_at: null,
        created_at: createdAt,
        updated_at: createdAt,
      };
      session.conversations.set(id, created);
      // sessions 里存的是同一个 Map；更新默认 id 只为兼容旧用例未显式传 conversation。
      sessions.get(session.token).conversation_id = id;
      json(
        response,
        201,
        created,
        existing === null
          ? {
              "set-cookie": `workpilot_session=${session.token}; Max-Age=1800; Path=/; HttpOnly; SameSite=Lax`,
            }
          : {},
      );
    });
    return;
  }

  const conversationRuntimeMatch = path.match(/^\/api\/v1\/conversations\/([^/]+)\/runtime$/);
  if (conversationRuntimeMatch && request.method === "PUT") {
    void readBody(request).then((body) => {
      const session = currentSession(request);
      const conversationId = conversationRuntimeMatch[1];
      const current = session?.conversations.get(conversationId);
      if (session === null || current === undefined) {
        json(response, 404, { detail: "会话不存在" });
        return;
      }
      const usesMockProvider = body.provider_profile_id === "mock-provider";
      const updated = {
        ...current,
        provider_profile_id: usesMockProvider ? "mock-provider" : null,
        provider_name: usesMockProvider ? "本地测试模型" : null,
        provider: usesMockProvider ? "openai" : null,
        selected_model: usesMockProvider ? (body.model_override ?? "mock-cowork") : null,
        unattended: body.unattended === true,
        approval_mode: body.approval_mode ?? "interactive",
        persona_name: body.persona_name ?? "general",
        updated_at: new Date().toISOString(),
      };
      session.conversations.set(conversationId, updated);
      json(response, 200, updated);
    });
    return;
  }

  const conversationMatch = path.match(/^\/api\/v1\/conversations\/([^/]+)$/);
  if (conversationMatch && request.method === "DELETE") {
    const session = currentSession(request);
    const conversationId = conversationMatch[1];
    if (session === null || !session.conversations.has(conversationId)) {
      json(response, 404, { detail: "会话不存在" });
      return;
    }
    const active = [...runs.values()].some(
      (run) =>
        run.session_token === session.token &&
        run.conversation_id === conversationId &&
        !["done", "failed", "cancelled", "budget_exceeded"].includes(run.status),
    );
    if (active) {
      json(response, 409, { detail: "会话仍有任务在运行" });
      return;
    }
    session.conversations.delete(conversationId);
    conversationKnowledgeBases.delete(conversationId);
    for (const [runId, run] of runs) {
      if (run.session_token === session.token && run.conversation_id === conversationId) {
        runs.delete(runId);
      }
    }
    session.conversation_id = session.conversations.keys().next().value ?? null;
    response.writeHead(204);
    response.end();
    return;
  }

  const conversationMessagesMatch = path.match(
    /^\/api\/v1\/conversations\/([^/]+)\/messages$/,
  );
  if (conversationMessagesMatch && request.method === "GET") {
    const session = currentSession(request);
    const conversationId = conversationMessagesMatch[1];
    if (session === null || !session.conversations.has(conversationId)) {
      json(response, 404, { detail: "会话不存在" });
      return;
    }
    const items = conversationMessages(session, conversationId);
    json(response, 200, { items, total: items.length });
    return;
  }

  const conversationContextMatch = path.match(
    /^\/api\/v1\/conversations\/([^/]+)\/context-usage$/,
  );
  if (conversationContextMatch && request.method === "GET") {
    const session = currentSession(request);
    const conversationId = conversationContextMatch[1];
    if (session === null || !session.conversations.has(conversationId)) {
      json(response, 404, { detail: "会话不存在" });
      return;
    }
    const hasRun = [...runs.values()].some((run) => run.conversation_id === conversationId);
    json(response, 200, {
      used_tokens: 37400,
      context_window_tokens: 102400,
      max_input_tokens: 93208,
      trigger_tokens: 79226,
      trigger_ratio: 0.85,
      auto_compaction: true,
      compaction_revision: 0,
      compaction_mode: "none",
      model: "mock-cowork",
      run_status: hasRun ? "done" : null,
      estimated: true,
      breakdown: {
        system: 4096,
        tool_manifest: 1536,
        tools: 31768,
        loaded_tools: 0,
        messages: 0,
        tool_activity: 0,
      },
    });
    return;
  }

  // ------------------------------------------------------------ 本地知识库挂载

  if (path === "/api/v1/cowork/knowledge-bases" && request.method === "GET") {
    if (!isAdmin(request)) {
      json(response, 401, { detail: "需要先登录 owner" });
      return;
    }
    json(response, 200, {
      items: [
        {
          slug: "papers",
          name: "论文资料库",
          description: "前端挂载时序测试",
          document_count: 3,
          is_indexed: true,
          embedding: "bge-m3:latest",
          active_version: "v1",
          versions: [],
          needs_migration: false,
          documents: [],
        },
        {
          slug: "agent-research",
          name: "Agent 研究库",
          description: "切换挂载测试",
          document_count: 2,
          is_indexed: true,
          embedding: "bge-m3:latest",
          active_version: "v1",
          versions: [],
          needs_migration: false,
          documents: [],
        },
      ],
    });
    return;
  }

  const conversationKnowledgeBaseMatch = path.match(
    /^\/api\/v1\/cowork\/sessions\/([^/]+)\/knowledge-base$/,
  );
  if (conversationKnowledgeBaseMatch && request.method === "GET") {
    const session = currentSession(request);
    const conversationId = conversationKnowledgeBaseMatch[1];
    if (session === null || !session.conversations.has(conversationId)) {
      json(response, 404, { detail: "会话不存在" });
      return;
    }
    json(response, 200, { slug: conversationKnowledgeBases.get(conversationId) ?? null });
    return;
  }
  if (conversationKnowledgeBaseMatch && request.method === "PUT") {
    if (!isAdmin(request)) {
      json(response, 401, { detail: "需要先登录 owner" });
      return;
    }
    void readBody(request).then((body) => {
      const session = currentSession(request);
      const conversationId = conversationKnowledgeBaseMatch[1];
      if (session === null || !session.conversations.has(conversationId)) {
        json(response, 404, { detail: "会话不存在" });
        return;
      }
      const slug = body.slug === null ? null : String(body.slug ?? "");
      if (slug !== null && !["papers", "agent-research"].includes(slug)) {
        json(response, 404, { detail: `知识库 ${slug} 不存在` });
        return;
      }
      conversationKnowledgeBases.set(conversationId, slug);
      json(response, 200, { slug });
    });
    return;
  }

  // ------------------------------------------------------------ owner 私有记忆

  const coworkMemoriesMatch = path.match(/^\/api\/v1\/cowork\/sessions\/([^/]+)\/memories$/);
  if (coworkMemoriesMatch && request.method === "GET") {
    if (!isAdmin(request)) {
      json(response, 401, { detail: "需要先登录 owner" });
      return;
    }
    json(response, 200, { items: [] });
    return;
  }

  const coworkRootsMatch = path.match(/^\/api\/v1\/cowork\/sessions\/([^/]+)\/roots$/);
  if (coworkRootsMatch && request.method === "GET") {
    if (!isAdmin(request)) {
      json(response, 401, { detail: "需要先登录 owner" });
      return;
    }
    const conversationId = coworkRootsMatch[1];
    if (!coworkRoots.has(conversationId)) {
      coworkRoots.set(conversationId, [
        {
          id: "8a0e4c55-0000-4d00-8000-000000000001",
          conversation_id: conversationId,
          requested_path: "/Users/demo/Documents/Quarterly",
          canonical_path: "/Users/demo/Documents/Quarterly",
          label: "Quarterly",
          access_mode: "read_write",
          enabled: true,
          created_at: "2026-08-18T03:00:00Z",
          updated_at: "2026-08-18T03:00:00Z",
        },
      ]);
    }
    json(response, 200, { items: coworkRoots.get(conversationId) });
    return;
  }

  if (coworkRootsMatch && request.method === "POST") {
    if (!isAdmin(request)) {
      json(response, 401, { detail: "需要先登录 owner" });
      return;
    }
    void readBody(request).then((body) => {
      const conversationId = coworkRootsMatch[1];
      const item = {
        id: `8a0e4c55-0000-4d00-8000-${String((coworkRoots.get(conversationId) ?? []).length + 2).padStart(12, "0")}`,
        conversation_id: conversationId,
        requested_path: body.path,
        canonical_path: body.path,
        label: String(body.path).split("/").at(-1) || "授权目录",
        access_mode: body.access_mode,
        enabled: true,
        created_at: "2026-08-18T03:00:00Z",
        updated_at: "2026-08-18T03:00:00Z",
      };
      const items = coworkRoots.get(conversationId) ?? [];
      items.push(item);
      coworkRoots.set(conversationId, items);
      json(response, 201, item);
    });
    return;
  }

  const coworkRootDeleteMatch = path.match(
    /^\/api\/v1\/cowork\/sessions\/([^/]+)\/roots\/([^/]+)$/,
  );
  if (coworkRootDeleteMatch && request.method === "DELETE") {
    const items = coworkRoots.get(coworkRootDeleteMatch[1]) ?? [];
    coworkRoots.set(coworkRootDeleteMatch[1], items.filter((item) => item.id !== coworkRootDeleteMatch[2]));
    response.writeHead(204);
    response.end();
    return;
  }

  const coworkGrantsMatch = path.match(/^\/api\/v1\/cowork\/sessions\/([^/]+)\/grants$/);
  if (coworkGrantsMatch && request.method === "GET") {
    const roots = coworkRoots.get(coworkGrantsMatch[1]) ?? [];
    const capabilities = ["filesystem.read", "filesystem.write"];
    json(response, 200, {
      items: roots.flatMap((root, rootIndex) => capabilities.map((capability, index) => ({
        id: `8b0e4c55-0000-4d00-8000-${String(rootIndex * 10 + index + 1).padStart(12, "0")}`,
        conversation_id: coworkGrantsMatch[1],
        session_root_id: root.id,
        capability,
        grant_source: "root_access_mode",
        expires_at: null,
        revoked_at: null,
        active: true,
        created_at: "2026-08-18T03:00:00Z",
        updated_at: "2026-08-18T03:00:00Z",
      }))),
    });
    return;
  }

  const readingMaterialMatch = path.match(
    /^\/api\/v1\/cowork\/sessions\/([^/]+)\/reading\/material$/,
  );
  if (readingMaterialMatch && request.method === "GET") {
    const materialPath = url.searchParams.get("path") ?? "/Users/demo/Documents/Quarterly/paper.pdf";
    json(response, 200, {
      path: materialPath,
      material_id: "mock-reader-material-v1",
      filename: materialPath.split("/").at(-1) ?? "paper.pdf",
      title: "Mock Paper",
      unit: "page",
      unit_count: 2,
      parser: "pymupdf",
      has_page_image: true,
      outline: [{ locator: 1, title: "Introduction", level: 1, synthesised: false }],
    });
    return;
  }

  const readingAnnotationsMatch = path.match(
    /^\/api\/v1\/cowork\/sessions\/([^/]+)\/reading\/annotations$/,
  );
  if (readingAnnotationsMatch && request.method === "GET") {
    json(response, 200, {
      material_id: "mock-reader-material-v1",
      items: [],
      stale_count: 0,
    });
    return;
  }

  const readingFileMatch = path.match(
    /^\/api\/v1\/cowork\/sessions\/([^/]+)\/reading\/file$/,
  );
  if (readingFileMatch && request.method === "GET") {
    response.writeHead(200, {
      "cache-control": "private, max-age=3600",
      "content-length": String(READER_PDF.length),
      "content-type": "application/pdf",
    });
    response.end(READER_PDF);
    return;
  }

  const readingAnnotationCreateMatch = path.match(
    /^\/api\/v1\/cowork\/sessions\/([^/]+)\/reading\/annotations$/,
  );
  if (readingAnnotationCreateMatch && request.method === "POST") {
    void readBody(request).then((body) => {
      json(response, 201, {
        annotation: {
          id: "mock-annotation-1",
          locator: body.locator ?? 1,
          quote: body.quote ?? "",
          note: body.note ?? "",
          color: body.color ?? "yellow",
          locations: [],
          created_at: new Date().toISOString(),
        },
        verified: true,
      });
    });
    return;
  }

  const readingPageMatch = path.match(
    /^\/api\/v1\/cowork\/sessions\/([^/]+)\/reading\/pages\/(\d+)\.png$/,
  );
  if (readingPageMatch && request.method === "GET") {
    response.writeHead(200, {
      "cache-control": "private, max-age=3600",
      "content-length": String(READER_PAGE_PNG.length),
      "content-type": "image/png",
    });
    response.end(READER_PAGE_PNG);
    return;
  }

  const coworkArtifactsMatch = path.match(/^\/api\/v1\/cowork\/sessions\/([^/]+)\/artifacts$/);
  if (coworkArtifactsMatch && request.method === "GET") {
    json(response, 200, { items: coworkArtifacts.get(coworkArtifactsMatch[1]) ?? [] });
    return;
  }

  const artifactPreviewMatch = path.match(/^\/api\/v1\/cowork\/artifacts\/([^/]+)\/preview$/);
  if (artifactPreviewMatch && request.method === "GET") {
    response.writeHead(200, {
      "cache-control": "no-store",
      "content-type": "text/html; charset=utf-8",
      "x-workpilot-preview-mode": "structure",
    });
    response.end("<!doctype html><meta charset='utf-8'><h1>季度汇报</h1><p>管理层摘要</p>");
    return;
  }

  const artifactDiffMatch = path.match(/^\/api\/v1\/cowork\/artifacts\/([^/]+)\/diff$/);
  if (artifactDiffMatch && request.method === "GET") {
    json(response, 200, {
      schema_version: 1,
      available: true,
      format: "unified",
      view: "semantic",
      created: false,
      before_sha256: "a".repeat(64),
      after_sha256: "b".repeat(64),
      added_lines: 2,
      removed_lines: 1,
      truncated: false,
      text: "--- 修改前\n+++ 修改后\n@@ -1 +1,2 @@\n-季度总结\n+管理层季度总结\n+关键结论",
      reason: null,
    });
    return;
  }

  if (path === "/api/v1/memories" && request.method === "GET") {
    if (!isAdmin(request)) {
      json(response, 401, { detail: "需要先登录 owner" });
      return;
    }
    const view = url.searchParams.get("view") ?? "current";
    const items = memories.filter((item) =>
      view === "history" ? item.invalid_at !== null : item.invalid_at === null,
    );
    json(response, 200, { items, total: items.length });
    return;
  }

  if (path === "/api/v1/memories" && request.method === "POST") {
    if (!isAdmin(request)) {
      json(response, 401, { detail: "需要先登录 owner" });
      return;
    }
    void readBody(request).then((body) => {
      const created = memoryRecord({
        category: body.category,
        fact: body.fact,
        pinned: body.pinned === true,
        source_type: "manual",
        source_message_id: null,
        confidence: 1,
        access_count: 0,
        last_used_at: null,
      });
      memories.unshift(created);
      json(response, 201, created);
    });
    return;
  }

  const restoreMemoryMatch = path.match(/^\/api\/v1\/memories\/([^/]+)\/restore$/);
  if (restoreMemoryMatch && request.method === "POST") {
    if (!isAdmin(request)) {
      json(response, 401, { detail: "需要先登录 owner" });
      return;
    }
    const historical = memories.find((item) => item.id === restoreMemoryMatch[1]);
    if (historical === undefined || historical.invalid_at === null) {
      json(response, historical === undefined ? 404 : 409, { detail: "记忆无法恢复" });
      return;
    }
    const restored = memoryRecord({
      category: historical.category,
      fact: historical.fact,
      pinned: historical.pinned,
      source_type: "manual",
      source_message_id: null,
      confidence: 1,
      access_count: 0,
      last_used_at: null,
    });
    memories.unshift(restored);
    json(response, 200, restored);
    return;
  }

  const memoryMatch = path.match(/^\/api\/v1\/memories\/([^/]+)$/);
  if (memoryMatch && request.method === "PATCH") {
    if (!isAdmin(request)) {
      json(response, 401, { detail: "需要先登录 owner" });
      return;
    }
    void readBody(request).then((body) => {
      const index = memories.findIndex(
        (item) => item.id === memoryMatch[1] && item.invalid_at === null,
      );
      if (index < 0) {
        json(response, 404, { detail: "当前记忆不存在" });
        return;
      }
      const current = memories[index];
      if (body.fact === undefined && body.category === undefined) {
        memories[index] = { ...current, pinned: body.pinned === true };
        json(response, 200, memories[index]);
        return;
      }
      const replacement = memoryRecord({
        category: body.category ?? current.category,
        fact: body.fact ?? current.fact,
        pinned: body.pinned ?? current.pinned,
        source_type: "manual",
        source_message_id: null,
        confidence: 1,
        access_count: 0,
        last_used_at: null,
      });
      memories[index] = {
        ...current,
        invalid_at: "2026-08-18T02:30:00Z",
        superseded_by: replacement.id,
      };
      memories.unshift(replacement);
      json(response, 200, replacement);
    });
    return;
  }

  if (memoryMatch && request.method === "DELETE") {
    if (!isAdmin(request)) {
      json(response, 401, { detail: "需要先登录 owner" });
      return;
    }
    const index = memories.findIndex(
      (item) => item.id === memoryMatch[1] && item.invalid_at === null,
    );
    if (index < 0) {
      json(response, 404, { detail: "当前记忆不存在" });
      return;
    }
    memories[index] = { ...memories[index], invalid_at: "2026-08-18T02:30:00Z" };
    response.writeHead(204);
    response.end();
    return;
  }

  if (path === "/api/v1/runs" && request.method === "POST") {
    void readBody(request).then((body) => {
      const existingSession = currentSession(request);
      const session = existingSession ?? createSession();
      if (
        body.conversation_id !== undefined &&
        !session.conversations.has(body.conversation_id)
      ) {
        json(response, 404, { detail: "对话不存在" });
        return;
      }
      if (
        body.conversation_id === undefined &&
        (session.conversation_id === null || !session.conversations.has(session.conversation_id))
      ) {
        const id = nextConversationId();
        session.conversation_id = id;
        session.conversations.set(id, {
          id,
          title: null,
          created_at: new Date().toISOString(),
        });
      }
      const query = typeof body.query === "string" ? body.query : "";
      // 模式跟着 run 走, 与后端一致(agent_runs.answer_mode), 不靠猜问题内容。
      const answerMode = body.mode === "general" ? "general" : "grounded";
      const run = {
        id: nextRunId(),
        conversation_id: body.conversation_id ?? session.conversation_id,
        session_token: session.token,
        goal: query,
        answer_mode: answerMode,
        scenario: pickScenario(query, answerMode),
        status: "queued",
        cancelled: false,
        dropped: false,
        last_sent_seq: 0,
      };
      runs.set(run.id, run);
      json(
        response,
        202,
        {
          run_id: run.id,
          conversation_id: run.conversation_id,
          status: "queued",
        },
        existingSession === null
          ? {
              "set-cookie": `workpilot_session=${session.token}; Max-Age=1800; Path=/; HttpOnly; SameSite=Lax`,
            }
          : {},
      );
    });
    return;
  }

  if (path === "/api/v1/runs/cowork" && request.method === "POST") {
    if (!isAdmin(request)) {
      json(response, 401, { detail: "需要先登录 owner" });
      return;
    }
    void readBody(request).then((body) => {
      const runId = nextRunId();
      const conversationId = body.conversation_id;
      const artifactId = "8c0e4c55-0000-4d00-8000-000000000001";
      const finalAnswer = "已将季度汇报改为管理层语气，并保留原有数据。";
      const waitsForCancel = body.goal.includes("保持运行直到我停止");
      const waitsForDirectoryApproval = body.goal.includes("请求目录授权");
      const fixtureScenarioName = coworkFixtureScenario(body.goal);
      const conversationTitle = fixtureScenarioName === null
        ? "优化季度汇报管理层表达"
        : body.goal.slice(0, 36);
      const initialEvents = [
        { type: "plan", data: { workflow_type: "cowork", mode: "dynamic_tool_loop", tools: [] } },
        { type: "step.update", data: { step_id: `${runId}-step-0`, step_idx: 0, tool: "list_files", status: "pending", activity: { title: "列出文件", summary: "查看 *.docx", target: "/Users/demo/Documents/Quarterly", target_kind: "path" } } },
      ];
      const standardEvents = waitsForCancel
        ? initialEvents
        : waitsForDirectoryApproval
          ? [
              ...initialEvents,
              {
                type: "interrupt",
                data: {
                  kind: "directory_request",
                  resume_token: "directory-approval-token",
                  payload: {
                    access_mode: "read_write",
                    reason: "需要读取并更新项目工作目录中的文件。",
                  },
                },
              },
            ]
          : [
        ...initialEvents,
        { type: "tool.start", data: { step_id: `${runId}-step-0`, step_idx: 0, tool: "list_files", activity: { title: "列出文件", summary: "查看 *.docx", target: "/Users/demo/Documents/Quarterly", target_kind: "path" } } },
        { type: "tool.result", data: { step_id: `${runId}-step-0`, step_idx: 0, tool: "list_files", reused: false, effect_ref: null } },
        { type: "step.update", data: { step_id: `${runId}-step-1`, step_idx: 1, tool: "load_skill", status: "pending", activity: { title: "加载格式 Skill", summary: "读取这项 Skill 的执行规范", target: "office-deliverable", target_kind: "text" } } },
        { type: "tool.result", data: { step_id: `${runId}-step-1`, step_idx: 1, tool: "load_skill", reused: false, effect_ref: null } },
        { type: "step.update", data: { step_id: `${runId}-step-2`, step_idx: 2, tool: "write_text_file", status: "pending", activity: { title: "写入文本", summary: "创建或更新文本文件", target: "/Users/demo/Documents/Quarterly/brief.md", target_kind: "path" } } },
        { type: "tool.result", data: { step_id: `${runId}-step-2`, step_idx: 2, tool: "write_text_file", reused: false, effect_ref: null } },
        { type: "step.update", data: { step_id: `${runId}-step-3`, step_idx: 3, tool: "run_shell", status: "pending", activity: { title: "执行 Shell 命令", summary: "渲染并验证 Word 交付物", target: "python render_docx.py 季度汇报.docx", target_kind: "code" } } },
        { type: "tool.start", data: { step_id: `${runId}-step-3`, step_idx: 3, tool: "run_shell", activity: { title: "执行 Shell 命令", summary: "渲染并验证 Word 交付物", target: "python render_docx.py 季度汇报.docx", target_kind: "code" } } },
        { type: "tool.result", data: { step_id: `${runId}-step-3`, step_idx: 3, tool: "run_shell", reused: false, effect_ref: "file:/Users/demo/Documents/Quarterly/季度汇报.docx#sha256=abc" } },
        { type: "artifact", data: { kind: "file", title: "季度汇报.docx", artifact_id: artifactId, effect_ref: "file:/Users/demo/Documents/Quarterly/季度汇报.docx#sha256=abc" } },
        { type: "step.update", data: { status: "done", summary: "" } },
        { type: "message.snapshot", data: { text: finalAnswer } },
        { type: "message.done", data: { message_id: `${runId}-message`, status: "completed" } },
        { type: "conversation.title", data: { conversation_id: conversationId, title: conversationTitle } },
        { type: "run.done", data: { workflow_type: "cowork", status: "done" } },
            ];
      const fixtureEvents = fixtureScenarioName === null
        ? null
        : SCENARIOS[fixtureScenarioName].events;
      const events = fixtureEvents === null
        ? standardEvents
        : [
            { type: "plan", data: { workflow_type: "cowork", mode: "dynamic_tool_loop", tools: [] } },
            ...fixtureEvents,
            { type: "conversation.title", data: { conversation_id: conversationId, title: conversationTitle } },
            ...(fixtureEvents.some((event) => event.type === "error")
              ? []
              : [{ type: "run.done", data: { workflow_type: "cowork", status: "done" } }]),
          ];
      coworkEventLogs.set(runId, events);
      const session = currentSession(request);
      const conversation = session?.conversations.get(conversationId);
      if (conversation !== undefined) {
        session.conversations.set(conversationId, {
          ...conversation,
          title: conversationTitle,
          updated_at: new Date().toISOString(),
        });
      }
      runs.set(runId, {
        id: runId,
        conversation_id: conversationId,
        session_token: session?.token ?? null,
        goal: body.goal,
        // 与真后端一致：创建 run 的这一刻读取一次会话挂载，之后会话切换不回写旧 run。
        kb_slug: conversationKnowledgeBases.get(conversationId) ?? null,
        workspace_path: (coworkRoots.get(conversationId) ?? [])[0]?.canonical_path ?? null,
        workspace_files: body.workspace_files ?? null,
        answer_mode: "grounded",
        workflow_type: "cowork",
        status: waitsForCancel || waitsForDirectoryApproval || fixtureScenarioName !== null ? "executing" : "done",
        started_at: Date.now(),
        cancelled: false,
        cowork_drop_after_seq: fixtureScenarioName === null
          ? (body.goal.includes("断线续传") ? 4 : null)
          : (SCENARIOS[fixtureScenarioName].drop_after_seq ?? null),
        cowork_replay_on_reconnect: fixtureScenarioName !== null
          && SCENARIOS[fixtureScenarioName].replay_on_reconnect === true,
        dropped: false,
        last_sent_seq: 0,
      });
      coworkArtifacts.set(conversationId, [
        {
          id: artifactId,
          conversation_id: conversationId,
          run_id: runId,
          session_root_id: "8a0e4c55-0000-4d00-8000-000000000001",
          kind: "file",
          title: "季度汇报.docx",
          uri: "/Users/demo/Documents/Quarterly/季度汇报.docx",
          mime_type: "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
          meta: { change_count: 2, summary: "已更新标题与结论段" },
          created_at: "2026-08-18T03:10:00Z",
          updated_at: "2026-08-18T03:10:00Z",
        },
      ]);
      json(response, 202, {
        run_id: runId,
        conversation_id: conversationId,
        conversation_title: conversationTitle,
        status: "queued",
        workflow_type: "cowork",
      });
    });
    return;
  }

  const coworkEventLogMatch = path.match(/^\/api\/v1\/runs\/([^/]+)\/event-log$/);
  if (coworkEventLogMatch && request.method === "GET") {
    const runId = coworkEventLogMatch[1];
    const run = runs.get(runId);
    const startedAt = run?.started_at ?? Date.now();
    const afterSeq = Number.parseInt(url.searchParams.get("after_seq") ?? "0", 10);
    const items = (coworkEventLogs.get(runId) ?? [])
      // 断线用例模拟真实生产时序：断开时库里只有已经发送的前缀，
      // event-log 先补这部分，客户端再带游标重连 SSE 继续收。
      .slice(0, run?.cowork_drop_after_seq ? run.last_sent_seq : undefined)
      .map((event, index) => ({
        id: `${runId}:${index + 1}`,
        run_id: runId,
        seq: String(index + 1),
        type: event.type,
        data: event.data,
        created_at: new Date(startedAt + index * 3000).toISOString(),
      }))
      .filter((event) => Number(event.seq) > afterSeq);
    json(response, 200, { items });
    return;
  }

  if (path === "/api/v1/runs/reviews" && request.method === "POST") {
    if (!isAdmin(request)) {
      json(response, 401, { detail: "需要 admin 登录" });
      return;
    }
    void readBody(request).then((body) => {
      const existingSession = currentSession(request);
      const session = existingSession ?? createSession();
      const documentIds = Array.isArray(body.document_ids) ? body.document_ids : [];
      // 与后端 create_review_run 的前置校验对齐：少于两篇、路径非法都不该进队列。
      if (documentIds.length < 2) {
        json(response, 422, { detail: "固定综述至少需要两篇不同文档" });
        return;
      }
      const outputPath = typeof body.output_path === "string" ? body.output_path : "";
      if (!outputPath.endsWith(".md") || outputPath.startsWith("/") || outputPath.includes("..")) {
        json(response, 422, { detail: "output_path 必须是输出目录内的相对 .md 路径" });
        return;
      }
      const goal = typeof body.goal === "string" ? body.goal : "";
      const run = {
        id: nextRunId(),
        conversation_id: session.conversation_id,
        session_token: session.token,
        goal,
        answer_mode: "grounded",
        workflow_type: "literature_review",
        // 目标里带"恢复"就回放 watchdog 自动恢复那条剧本。
        scenario: goal.includes("恢复") ? "reviewRecovered" : "review",
        status: "queued",
        cancelled: false,
        dropped: false,
        last_sent_seq: 0,
        output_path: outputPath,
      };
      runs.set(run.id, run);
      json(
        response,
        202,
        {
          run_id: run.id,
          conversation_id: run.conversation_id,
          status: "queued",
          workflow_type: "literature_review",
        },
        existingSession === null
          ? {
              "set-cookie": `workpilot_session=${session.token}; Max-Age=1800; Path=/; HttpOnly; SameSite=Lax`,
            }
          : {},
      );
    });
    return;
  }

  const resumeMatch = path.match(/^\/api\/v1\/runs\/([^/]+)\/resume$/);
  if (resumeMatch && request.method === "POST") {
    // 批准写回是整条工作流唯一有副作用的一步，admin 门必须在所有权检查之前。
    if (!isAdmin(request)) {
      json(response, 401, { detail: "需要 admin 登录" });
      return;
    }
    const run = runs.get(resumeMatch[1]);
    if (run === undefined || currentSession(request)?.token !== run.session_token) {
      json(response, 404, { detail: "run 不存在" });
      return;
    }
    void readBody(request).then((body) => {
      if (body.resume_token !== REVIEW_RESUME_TOKEN) {
        json(response, 409, { detail: "resume_token 不匹配" });
        return;
      }
      // 批准才产出 written_note；拒绝只把步骤标 skipped，磁盘零写入。
      run.pending_events = body.approved === true
        ? [
            {
              delay_ms: 60,
              type: "artifact",
              data: {
                kind: "written_note",
                title: "已写入笔记",
                path: REVIEW_OUTPUT_PATH,
                effect_ref: "note:1",
                content_sha256: "a".repeat(64),
                reused: run.resumed === true,
              },
            },
            { delay_ms: 40, type: "step.update", data: { step_id: "s5", step_idx: 5, status: "done" } },
            { delay_ms: 40, type: "run.done", data: { workflow_type: "literature_review", effect_ref: "note:1" } },
          ]
        : [
            { delay_ms: 40, type: "step.update", data: { step_id: "s5", step_idx: 5, status: "skipped", summary: "用户拒绝写回" } },
            { delay_ms: 40, type: "run.done", data: { workflow_type: "literature_review", effect_ref: null } },
          ];
      run.resumed = true;
      json(response, 200, {
        run_id: run.id,
        conversation_id: run.conversation_id,
        goal: run.goal,
        answer_mode: run.answer_mode,
        workflow_type: "literature_review",
        status: body.approved === true ? "done" : "done",
        cancel_requested: false,
        used_tokens: 0,
        used_calls: 0,
        next_seq: 1,
        error: null,
      });
    });
    return;
  }

  const eventsMatch = path.match(/^\/api\/v1\/runs\/([^/]+)\/events$/);
  if (eventsMatch && request.method === "GET") {
    const run = runs.get(eventsMatch[1]);
    const coworkAuthorized = run?.workflow_type === "cowork" && isAdmin(request);
    if (run === undefined || (!coworkAuthorized && currentSession(request)?.token !== run.session_token)) {
      json(response, 404, { detail: "run 不存在" });
      return;
    }
    if (run.workflow_type === "cowork") void streamCoworkEvents(request, response, run);
    else void streamEvents(request, response, run);
    return;
  }

  const runMatch = path.match(/^\/api\/v1\/runs\/([^/]+)$/);
  if (runMatch && request.method === "GET") {
    const run = runs.get(runMatch[1]);
    if (run === undefined || currentSession(request)?.token !== run.session_token) {
      json(response, 404, { detail: "run 不存在" });
      return;
    }
    json(response, 200, {
      run_id: run.id,
      conversation_id: run.conversation_id,
      goal: run.goal,
      answer_mode: run.answer_mode,
      status: run.status,
      cancel_requested: run.cancelled,
      used_tokens: 128,
      used_calls: 1,
      next_seq: run.last_sent_seq + 1,
      error: null,
    });
    return;
  }

  if (path === "/api/v1/library" && request.method === "GET") {
    const keyword = (url.searchParams.get("query") ?? "").trim();
    const documents =
      keyword === ""
        ? LIBRARY.documents
        : LIBRARY.documents.filter(
            (item) => item.title.includes(keyword) || item.source_uri.includes(keyword),
          );
    json(response, 200, { ...LIBRARY, documents });
    return;
  }

  const cancelMatch = path.match(/^\/api\/v1\/runs\/([^/]+)\/cancel$/);
  if (cancelMatch && request.method === "POST") {
    const run = runs.get(cancelMatch[1]);
    const authorized = run?.workflow_type === "cowork"
      ? isAdmin(request)
      : currentSession(request)?.token === run?.session_token;
    if (run === undefined || !authorized) {
      json(response, 404, { detail: "run 不存在" });
      return;
    }
    if (!run.cancelled) {
      run.cancelled = true;
      if (run.workflow_type === "cowork") {
        run.status = "cancelled";
        const events = coworkEventLogs.get(run.id) ?? [];
        events.push(
          { type: "step.update", data: { step_id: `${run.id}-step-0`, step_idx: 0, tool: "list_files", status: "skipped", summary: "用户停止，未执行此步骤" } },
          { type: "error", data: { code: "cancelled", retryable: true, user_message: "Cowork 任务已停止。已完成的文件修改会保留。" } },
          { type: "run.done", data: { workflow_type: "cowork", status: "cancelled" } },
        );
        coworkEventLogs.set(run.id, events);
      }
    }
    json(response, 200, {
      run_id: run.id,
      conversation_id: run.conversation_id,
      goal: run.goal,
      answer_mode: run.answer_mode,
      workflow_type: run.workflow_type ?? "answer",
      status: run.status,
      cancel_requested: true,
      used_tokens: 128,
      used_calls: 1,
      next_seq: run.last_sent_seq + 1,
      error: null,
    });
    return;
  }

  const pageMatch = path.match(/^\/api\/v1\/documents\/([^/]+)\/pages\/(\d+)\.png$/);
  if (pageMatch && request.method === "GET") {
    if (!currentSession(request)?.versions.has(pageMatch[1])) {
      json(response, 404, { detail: "文档版本不存在" });
      return;
    }
    const png = renderPagePng(PAGE.width, PAGE.height, Number(pageMatch[2]));
    response.writeHead(200, {
      "content-type": "image/png",
      "content-length": String(png.length),
      "cache-control": "private, max-age=3600",
    });
    response.end(png);
    return;
  }

  const fileMatch = path.match(/^\/api\/v1\/documents\/([^/]+)\/file$/);
  if (fileMatch && request.method === "GET") {
    if (!currentSession(request)?.versions.has(fileMatch[1])) {
      json(response, 404, { detail: "文档版本不存在" });
      return;
    }
    if (fileMatch[1] === IDS.mdVersion) {
      response.writeHead(200, { "content-type": "text/markdown; charset=utf-8" });
      response.end(MD_FILE_CONTENT);
      return;
    }
    response.writeHead(200, { "content-type": "application/pdf" });
    response.end("%PDF-1.4\n% mock file for acceptance tests\n%%EOF\n");
    return;
  }

  json(response, 404, { detail: `mock 后端没有实现 ${request.method} ${path}` });
});

server.listen(PORT, "127.0.0.1", () => {
  process.stdout.write(`mock backend listening on http://127.0.0.1:${PORT}\n`);
});
