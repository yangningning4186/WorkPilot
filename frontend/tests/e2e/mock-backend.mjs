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

/** run_id → 运行态。进程内存即可，验收进程和被测进程都是一次性的。 */
const runs = new Map();
/** 匿名 session token → 会话拥有的 conversation 与已引用文档版本。 */
const sessions = new Map();
/** 供测试断言的请求流水（例如"取消接口确实被调用过"）。 */
const requestLog = [];
/** 已签发的 admin token。真后端存在 Redis，这里内存等价。 */
const adminTokens = new Set();
/** 后端是否配了 DEMO_ADMIN_PASSWORD_HASH；关掉用来验收 503 的提示文案。 */
let adminConfigured = true;

const ADMIN_PASSWORD = "demo-admin-pw";

let runCounter = 0;
let sessionCounter = 0;
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
  const token = cookies(request).workpilot_session;
  const session = token === undefined ? undefined : sessions.get(token);
  return session === undefined ? null : { token, ...session };
}

/** 与真后端一致：admin 凭据只认 httpOnly cookie，前端 JS 读不到。 */
function isAdmin(request) {
  const token = cookies(request).workpilot_admin_session;
  return token !== undefined && adminTokens.has(token);
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
    adminTokens.clear();
    adminConfigured = true;
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
      // 模式跟着 run 走, 与后端一致(agent_runs.answer_mode), 不靠猜问题内容。
      const answerMode = body.mode === "general" ? "general" : "grounded";
      const run = {
        id: nextRunId(),
        conversation_id: session.conversation_id,
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
    if (run === undefined || currentSession(request)?.token !== run.session_token) {
      json(response, 404, { detail: "run 不存在" });
      return;
    }
    void streamEvents(request, response, run);
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
