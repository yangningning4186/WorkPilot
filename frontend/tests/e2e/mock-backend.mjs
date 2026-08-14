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
  MD_FILE_CONTENT,
  PAGE,
  SCENARIOS,
  pickScenario,
} from "./fixtures/scenarios.mjs";

const PORT = Number(process.env.MOCK_BACKEND_PORT ?? 8787);

/** run_id → 运行态。进程内存即可，验收进程和被测进程都是一次性的。 */
const runs = new Map();
/** 匿名 session token → 会话拥有的 conversation 与已引用文档版本。 */
const sessions = new Map();
/** 供测试断言的请求流水（例如"取消接口确实被调用过"）。 */
const requestLog = [];

let runCounter = 0;
let sessionCounter = 0;

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
  const token = cookies(request).workpilot_session;
  const session = token === undefined ? undefined : sessions.get(token);
  return session === undefined ? null : { token, ...session };
}

function createSession() {
  sessionCounter += 1;
  const suffix = String(sessionCounter).padStart(12, "0");
  const token = `mock-session-${suffix}`;
  const session = {
    conversation_id: `9b0e4c55-0000-4d00-8000-${suffix}`,
    versions: new Set(),
  };
  sessions.set(token, session);
  return { token, ...session };
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
  // 仅 mock 有：把 EventSource 默认 3s 重连压到 250ms，断线用例才不用干等。
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
      // 掐断且不发终态：模拟网络断开，浏览器会自己带 Last-Event-ID 重连。
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

  run.status = "done";
  response.end();
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

  if (path === "/__reset" && request.method === "POST") {
    runs.clear();
    sessions.clear();
    requestLog.length = 0;
    json(response, 200, { status: "reset" });
    return;
  }

  if (path === "/api/v1/runs" && request.method === "POST") {
    void readBody(request).then((body) => {
      const existingSession = currentSession(request);
      const session = existingSession ?? createSession();
      if (
        body.conversation_id !== undefined &&
        body.conversation_id !== session.conversation_id
      ) {
        json(response, 404, { detail: "对话不存在" });
        return;
      }
      const query = typeof body.query === "string" ? body.query : "";
      const run = {
        id: nextRunId(),
        conversation_id: session.conversation_id,
        session_token: session.token,
        goal: query,
        scenario: pickScenario(query),
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

  const eventsMatch = path.match(/^\/api\/v1\/runs\/([^/]+)\/events$/);
  if (eventsMatch && request.method === "GET") {
    const run = runs.get(eventsMatch[1]);
    if (run === undefined || currentSession(request)?.token !== run.session_token) {
      json(response, 404, { detail: "run 不存在" });
      return;
    }
    void streamEvents(request, response, run);
    return;
  }

  const cancelMatch = path.match(/^\/api\/v1\/runs\/([^/]+)\/cancel$/);
  if (cancelMatch && request.method === "POST") {
    const run = runs.get(cancelMatch[1]);
    if (run === undefined || currentSession(request)?.token !== run.session_token) {
      json(response, 404, { detail: "run 不存在" });
      return;
    }
    run.cancelled = true;
    json(response, 200, {
      run_id: run.id,
      conversation_id: run.conversation_id,
      goal: run.goal,
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
