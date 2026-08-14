/**
 * scenarios.mjs 的类型声明。
 *
 * 剧本本体写成 .mjs 是因为 mock 后端要用 node 直接跑，不能带编译步骤；
 * 但测试侧仍然要有完整类型（CLAUDE.md：TypeScript strict，不允许 any），
 * 所以类型在这里手写，并直接复用前端的协议定义——协议改了这里会编译失败，
 * 这正是想要的：剧本和契约必须一起改。
 */

import type { LibraryResponse } from "../../../src/lib/api";
import type {
  CitationPayload,
  ErrorPayload,
  MessageDeltaPayload,
  MessageDonePayload,
  MessageStartPayload,
  RunEventType,
} from "../../../src/lib/run-protocol";

export interface ScriptedEvent {
  /** 发送该事件之前的等待时长。 */
  delay_ms: number;
  type: RunEventType;
  data:
    | MessageStartPayload
    | MessageDeltaPayload
    | CitationPayload
    | MessageDonePayload
    | ErrorPayload;
}

export interface Scenario {
  events: ScriptedEvent[];
  /** 发到该 seq 后掐断连接（只掐第一次），用于验证 Last-Event-ID 续传。 */
  drop_after_seq?: number;
  /** 重连时无视 Last-Event-ID，从头重发一遍，用于验证前端按 seq 去重。 */
  replay_on_reconnect?: boolean;
  /** 发到该 seq 后停住，等取消请求才继续。 */
  stall_after_seq?: number;
  /** 收到取消请求后补发的终止事件。 */
  cancel_event?: { type: RunEventType; data: ErrorPayload };
}

export type ScenarioName =
  | "pdf"
  | "general"
  | "markdownRender"
  | "markdown"
  | "refusal"
  | "error"
  | "drop"
  | "replay"
  | "cancel";

export declare const IDS: {
  conversation: string;
  message: string;
  pdfVersion: string;
  pdfDoc: string;
  mdVersion: string;
  mdDoc: string;
};

export declare const PAGE: { width: number; height: number };

export type Bbox = [number, number, number, number];

export declare const S1_BBOX_PAGE3: Bbox;
export declare const S1_BBOX_PAGE4: Bbox;
export declare const S2_BBOX_PAGE5: Bbox;

export declare const PDF_CITATION_S1: CitationPayload;
export declare const PDF_CITATION_S2: CitationPayload;
export declare const MD_CITATION: CitationPayload;
export declare const MD_FILE_CONTENT: string;
export declare const SCENARIOS: Record<ScenarioName, Scenario>;
export declare function pickScenario(query: string, mode?: "grounded" | "general"): ScenarioName;

export declare const LIBRARY: LibraryResponse;
