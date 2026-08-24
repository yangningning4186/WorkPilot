/**
 * pdf.js 的唯一加载点。
 *
 * 库加它的 worker 一起一兆多，所以动态 import：从来不开阅读器的用户不该为它付这份
 * 下载。promise 记忆化之后，同一份文档的每一页共用一个模块实例和一个 worker。
 *
 * worker 路径用 `new URL(..., import.meta.url)` 拼而不是写死 `/pdf.worker.mjs`：
 * 后者要靠构建步骤往 public/ 里拷一份，版本一升就悄悄指向旧文件——而 worker 与主库
 * 版本不匹配的表现是"页面一直转圈"，不报错。
 */

import type * as PdfjsModule from "pdfjs-dist";

export type Pdfjs = typeof PdfjsModule;
export type PdfDocument = Awaited<ReturnType<Pdfjs["getDocument"]>["promise"]>;

let pending: Promise<Pdfjs> | null = null;

export function loadPdfjs(): Promise<Pdfjs> {
  if (pending !== null) return pending;
  pending = (async () => {
    const pdfjs = await import("pdfjs-dist");
    if (!pdfjs.GlobalWorkerOptions.workerSrc) {
      pdfjs.GlobalWorkerOptions.workerSrc = new URL(
        "pdfjs-dist/build/pdf.worker.min.mjs",
        import.meta.url,
      ).toString();
    }
    return pdfjs;
  })().catch((error: unknown) => {
    // 失败不缓存：一次网络抖动导致的 chunk 加载失败不该让阅读器在这个会话里彻底废掉。
    pending = null;
    throw error;
  });
  return pending;
}

/**
 * 画布的设备像素倍率，带上限。
 *
 * 不设上限时，3 倍屏上一整页的画布会大到被移动端 Safari 的画布内存限制丢掉——表现是
 * 一片空白而不是一条错误。
 */
export function outputScale(): number {
  if (typeof window === "undefined") return 1;
  return Math.min(2, Math.max(1, window.devicePixelRatio || 1));
}
