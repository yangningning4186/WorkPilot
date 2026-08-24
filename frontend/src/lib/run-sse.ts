/** SSE 拆帧与可中断重连等待。Cowork 与通用 run 共用同一套边界语义。 */

export interface ParsedSseFrame {
  data: string | null;
  retryMs: number | null;
}

export function parseSseFrame(frame: string): ParsedSseFrame {
  const data: string[] = [];
  let retryMs: number | null = null;
  for (const line of frame.split(/\r\n|\r|\n/)) {
    if (line === "" || line.startsWith(":")) continue;
    const separator = line.indexOf(":");
    const field = separator < 0 ? line : line.slice(0, separator);
    const value = separator < 0 ? "" : line.slice(separator + 1).replace(/^ /, "");
    if (field === "data") data.push(value);
    if (field === "retry" && /^\d+$/.test(value)) retryMs = Number(value);
  }
  return { data: data.length === 0 ? null : data.join("\n"), retryMs };
}

export function takeSseFrame(buffer: string): [string, string] | null {
  const boundary = /(?:\r\n|\r|\n){2}/.exec(buffer);
  if (boundary?.index === undefined) return null;
  const end = boundary.index + boundary[0].length;
  return [buffer.slice(0, boundary.index), buffer.slice(end)];
}

export function waitForStreamRetry(delayMs: number, signal: AbortSignal): Promise<void> {
  return new Promise((resolve) => {
    const timeout = window.setTimeout(done, delayMs);
    signal.addEventListener("abort", done, { once: true });
    function done() {
      window.clearTimeout(timeout);
      signal.removeEventListener("abort", done);
      resolve();
    }
  });
}
