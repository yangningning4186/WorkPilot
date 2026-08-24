/**
 * 阅读器的实时状态，在发送一条消息的那一刻读一次。
 *
 * 刻意做成模块级的一个格子，而不是 React state 或 context：视口每滚一下就变，聊天区
 * 一旦订阅它，滚动一个像素就要把整条消息列表重渲染一遍。这里的值**不参与任何渲染**，
 * 只在发送时被读走一次，所以一个普通变量既更便宜也更诚实地表达了这件事。
 *
 * 由阅读器面板写入，由发送逻辑读取。
 */

import type { CoworkWorkMode } from "./api";

export interface ReadingViewport {
  locator?: number;
  selection?: string;
  unit?: "page" | "section";
}

interface Cell {
  path: string | null;
  locator: number;
  selection: string;
  unit: "page" | "section";
}

const cell: Cell = { path: null, locator: 0, selection: "", unit: "page" };

/** 阅读器打开/关闭了一份文档。 */
export function setReadingMaterial(path: string | null, unit: "page" | "section" = "page"): void {
  cell.path = path;
  cell.unit = unit;
  if (path === null) {
    // 关掉文档必须把视口一起清掉，否则下一轮会告诉模型"用户正在看某一页"——而那份
    // 文件已经不在屏幕上了。
    cell.locator = 0;
    cell.selection = "";
  }
}

export function setReadingViewport(next: { locator?: number; selection?: string }): void {
  if (typeof next.locator === "number" && Number.isFinite(next.locator)) {
    cell.locator = next.locator > 0 ? Math.floor(next.locator) : 0;
  }
  if (typeof next.selection === "string") {
    cell.selection = next.selection;
  }
}

/**
 * 这一轮要随请求带上的视口，没有就返回 undefined。
 *
 * 两个前提都是必需的：**这一轮确实是阅读档**，而且**阅读器里确实开着文档**。只判后者
 * 的话，用户切回日常办公、甚至开一段全新会话之后，那份仍然挂在客户端上的文档会继续
 * 跟着每一条消息发出去——模型于是会用一份用户早就翻过去的文件来回答。
 */
export function readingViewportFor(
  workMode: CoworkWorkMode,
  path: string | null | undefined,
): ReadingViewport | undefined {
  if (workMode !== "reading") return undefined;
  const opened = (path ?? "").trim();
  if (opened === "" || cell.path === null || cell.path !== opened) return undefined;
  const viewport: ReadingViewport = { unit: cell.unit };
  if (cell.locator > 0) viewport.locator = cell.locator;
  if (cell.selection !== "") viewport.selection = cell.selection;
  return viewport.locator === undefined && viewport.selection === undefined
    ? undefined
    : viewport;
}

/** 测试用：把格子复位。 */
export function resetReadingTurnState(): void {
  cell.path = null;
  cell.locator = 0;
  cell.selection = "";
  cell.unit = "page";
}
