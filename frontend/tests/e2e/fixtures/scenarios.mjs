/**
 * 验收用的 run 事件剧本。
 *
 * 这里的每个 payload 都必须与后端真实契约逐字段对齐：
 * `app/worker/answer_run.py::_citation_payload` 与 `app/services/runs.py::RunEvent.envelope`。
 * 剧本对不上契约，验收就只是在测自己写的假数据（docs/06 §评测可信度同理）。
 *
 * 场景由 POST /api/v1/runs 的 query 文本选中，见 mock-backend.mjs::pickScenario。
 */

/** 固定 id：断言里要直接用，随机化会让失败信息读不出所以然。 */
export const IDS = {
  conversation: "6f1d2b90-0000-4a00-8000-000000000001",
  message: "6f1d2b90-0000-4a00-8000-000000000002",
  pdfVersion: "8c2a5f31-1111-4b00-8000-0000000000a1",
  pdfDoc: "8c2a5f31-1111-4b00-8000-0000000000a2",
  mdVersion: "8c2a5f31-2222-4b00-8000-0000000000b1",
  mdDoc: "8c2a5f31-2222-4b00-8000-0000000000b2",
};

/** PDF 页面尺寸：预览面板的 aspectRatio 与高亮几何都由它推出来。 */
export const PAGE = { width: 595, height: 842 };

/**
 * 高亮几何的期望值单独导出。
 * 测试里再从 locations[i] 里取一遍，只会因为 noUncheckedIndexedAccess 多出一堆
 * 与被测行为无关的判空，读用例的人也看不清断言到底在比什么。
 */
export const S1_BBOX_PAGE3 = [0.118, 0.204, 0.882, 0.336];
export const S1_BBOX_PAGE4 = [0.118, 0.11, 0.62, 0.19];
export const S2_BBOX_PAGE5 = [0.2, 0.62, 0.8, 0.7];

/**
 * 跨页引用：locations 有两页 → 预览面板出页码 tab。
 * bbox_norm 是归一化 top_left 坐标，测试会拿它反推期望像素位置（约束 3）。
 */
export const PDF_CITATION_S1 = {
  citation_id: "S1",
  block_id: "9a3b7c10-1111-4c00-8000-0000000000c1",
  version_id: IDS.pdfVersion,
  doc_id: IDS.pdfDoc,
  title: "混合检索与 RRF 融合",
  source_uri: "data/library/papers/hybrid-retrieval.pdf",
  quote: "dense 与词法两路的失败模式并不重叠，因此 RRF 融合的增益主要来自互补而非叠加。",
  char_start: 4821,
  char_end: 4899,
  heading_path: ["4 检索", "4.2 融合策略"],
  locations: [
    {
      page_no: 3,
      bbox_norm: S1_BBOX_PAGE3,
      page_width: PAGE.width,
      page_height: PAGE.height,
      rotation: 0,
      coord_origin: "top_left",
    },
    {
      page_no: 4,
      bbox_norm: S1_BBOX_PAGE4,
      page_width: PAGE.width,
      page_height: PAGE.height,
      rotation: 0,
      coord_origin: "top_left",
    },
  ],
};

export const PDF_CITATION_S2 = {
  citation_id: "S2",
  block_id: "9a3b7c10-2222-4c00-8000-0000000000c2",
  version_id: IDS.pdfVersion,
  doc_id: IDS.pdfDoc,
  title: "混合检索与 RRF 融合",
  source_uri: "data/library/papers/hybrid-retrieval.pdf",
  quote: "table 类问题的 Recall@10 由 0.62 提升到 0.81。",
  char_start: 7310,
  char_end: 7352,
  heading_path: ["5 实验", "5.1 分类别结果"],
  locations: [
    {
      page_no: 5,
      bbox_norm: S2_BBOX_PAGE5,
      page_width: PAGE.width,
      page_height: PAGE.height,
      rotation: 0,
      coord_origin: "top_left",
    },
  ],
};

/** Markdown 引用：没有 locations 且后缀是 .md，走 HighlightedMarkdown 分支。 */
export const MD_CITATION = {
  citation_id: "S1",
  block_id: "9a3b7c10-3333-4c00-8000-0000000000c3",
  version_id: IDS.mdVersion,
  doc_id: IDS.mdDoc,
  title: "检索评测笔记",
  source_uri: "data/library/notes/检索评测笔记.md",
  quote: "词法单臂在英文集上 +0.2188，融合后 Δ 恰为 0。",
  char_start: 120,
  char_end: 158,
  heading_path: ["E3 英文标注集"],
  locations: [],
};

/** Markdown 原文：必须真的包含 quote，否则前端会走 quote-fallback 分支。 */
export const MD_FILE_CONTENT = [
  "# 检索评测笔记",
  "",
  "## E3 英文标注集",
  "",
  `补 20 条纯英文题后实测：${MD_CITATION.quote}`,
  "",
  "结论：dense 在英文上已经吃满，词法的增益被融合吸收。",
].join("\n");

const DONE_OK = {
  message_id: IDS.message,
  refused: false,
  refusal_reason: null,
  latency_ms: 1840,
  cost_usd: "0.0031",
};

/**
 * 一个剧本 = 有序事件 + 每个事件之前的等待。
 * delay_ms 要够大，测试才能稳定观察到"正文写到一半"的中间态。
 */
export const SCENARIOS = {
  pdf: {
    events: [
      { delay_ms: 120, type: "message.start", data: { message_id: IDS.message } },
      {
        delay_ms: 200,
        type: "message.delta",
        data: { text: "混合检索之所以更稳定，是因为 dense 与词法两路的失败模式并不重叠" },
      },
      {
        delay_ms: 350,
        type: "message.delta",
        data: {
          text: "：dense 擅长语义相近但用词不同的表述，词法擅长专有名词与数字的精确命中 [S1]。",
        },
      },
      {
        // 这一段刻意拖久一点：验收要能稳定观察到"正文只写了一半"的中间态，
        // 否则"流式"就退化成"一次性渲染"也照样能通过。
        delay_ms: 700,
        type: "message.delta",
        data: { text: "在 core-dev 上，RRF 融合把 table 类问题的 Recall@10 从 0.62 提到 0.81 [S2]。" },
      },
      { delay_ms: 120, type: "citation", data: PDF_CITATION_S1 },
      { delay_ms: 60, type: "citation", data: PDF_CITATION_S2 },
      { delay_ms: 80, type: "message.done", data: DONE_OK },
    ],
  },

  markdown: {
    events: [
      { delay_ms: 100, type: "message.start", data: { message_id: IDS.message } },
      {
        delay_ms: 150,
        type: "message.delta",
        data: { text: "英文集上词法单臂显著提升，但融合后增量为零 [S1]。" },
      },
      { delay_ms: 100, type: "citation", data: MD_CITATION },
      { delay_ms: 80, type: "message.done", data: DONE_OK },
    ],
  },

  /** 拒答：没有 delta、没有 citation，只有 refused=true 的终态。 */
  refusal: {
    events: [
      { delay_ms: 120, type: "message.start", data: { message_id: IDS.message } },
      {
        delay_ms: 200,
        type: "message.done",
        data: {
          message_id: IDS.message,
          refused: true,
          refusal_reason: "top_score_below_threshold",
          latency_ms: 640,
          cost_usd: "0.0004",
        },
      },
    ],
  },

  error: {
    events: [
      { delay_ms: 100, type: "message.start", data: { message_id: IDS.message } },
      { delay_ms: 150, type: "message.delta", data: { text: "正在整理证据" } },
      {
        delay_ms: 150,
        type: "error",
        data: {
          user_message: "本次回答超出了每日费用上限",
          retryable: false,
          code: "budget_exceeded",
        },
      },
    ],
  },

  /**
   * 断线续传（B2）：第一条连接在 drop_after_seq 之后被直接掐断且不发终态事件。
   * 浏览器 EventSource 自动重连并带上 Last-Event-ID，服务端必须从断点续发。
   * 断点选在正文中间——从头重放会让正文出现重复片段，这正是要验的回归。
   */
  drop: {
    drop_after_seq: 3,
    events: [
      { delay_ms: 100, type: "message.start", data: { message_id: IDS.message } },
      { delay_ms: 120, type: "message.delta", data: { text: "断线前的前半句，" } },
      { delay_ms: 120, type: "message.delta", data: { text: "断线后续上的后半句，" } },
      { delay_ms: 200, type: "message.delta", data: { text: "以及结尾 [S1]。" } },
      { delay_ms: 100, type: "citation", data: PDF_CITATION_S1 },
      { delay_ms: 80, type: "message.done", data: DONE_OK },
    ],
  },

  /**
   * 重连重放：断线后服务端**不认** Last-Event-ID，从头重发一遍。
   *
   * 真后端是认的，但重连竞态下客户端仍可能收到已经看过的事件，所以前端必须按 seq
   * 去重（run-state.ts）。只测"服务端老实续传"覆盖不到这条——把去重删掉照样绿。
   */
  replay: {
    drop_after_seq: 3,
    replay_on_reconnect: true,
    events: [
      { delay_ms: 100, type: "message.start", data: { message_id: IDS.message } },
      { delay_ms: 120, type: "message.delta", data: { text: "重放前的前半句，" } },
      { delay_ms: 120, type: "message.delta", data: { text: "重放后补上的后半句，" } },
      { delay_ms: 200, type: "message.delta", data: { text: "以及结尾 [S1]。" } },
      { delay_ms: 100, type: "citation", data: PDF_CITATION_S1 },
      { delay_ms: 80, type: "message.done", data: DONE_OK },
    ],
  },

  /**
   * 取消：正文只写一半就停住，等 POST /cancel 到来才收尾。
   * 后端取消走的是 error 事件（answer_run.py::_abort），不是 message.done。
   */
  cancel: {
    stall_after_seq: 2,
    cancel_event: {
      type: "error",
      data: { user_message: "回答已取消", retryable: true, code: "cancelled" },
    },
    events: [
      { delay_ms: 100, type: "message.start", data: { message_id: IDS.message } },
      { delay_ms: 150, type: "message.delta", data: { text: "正在写第一段，还没写完就会被取消" } },
    ],
  },
};

/** query 文本 → 剧本。测试用中文关键词选场景，读用例时一眼能看出在测什么。 */
export function pickScenario(query) {
  if (query.includes("markdown")) return "markdown";
  if (query.includes("拒答")) return "refusal";
  if (query.includes("报错")) return "error";
  if (query.includes("断线")) return "drop";
  if (query.includes("重放")) return "replay";
  if (query.includes("取消")) return "cancel";
  return "pdf";
}
