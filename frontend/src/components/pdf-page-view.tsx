"use client";

import { useEffect, useRef, useState } from "react";

import { loadPdfjs, outputScale, type PdfDocument } from "@/lib/pdfjs-loader";

/**
 * 一页 PDF：画布 + 可选中的文本层。
 *
 * **文本层是这个组件存在的全部理由。** 在它之前阅读器贴的是后端渲染好的 PNG，用户
 * 选不中、复制不了，也没法把"这一段"交给模型——于是阅读器只能是单向的：模型推给用户
 * 看，用户无从指回来。画布负责好看，文本层负责能指。
 *
 * 画布和文本层**并发**渲染、互不等待。文本层只需要 viewport 和 textContent，和位图
 * 画没画完毫无关系；把它挂在画布的 render promise 后面，只会让"能不能选中"被那个
 * promise 绑架——某些内嵌浏览器画得出页面却永远不 settle，表现就是一份看起来完好、
 * 却一个字都选不中的文档。
 */
export interface PdfPageViewProps {
  doc: PdfDocument;
  locator: number;
  /** 页面宽度（CSS px）；高度按页面自身宽高比推出来。 */
  width: number;
  onFailed: () => void;
}

export function PdfPageView({ doc, locator, width, onFailed }: PdfPageViewProps) {
  const canvasRef = useRef<HTMLCanvasElement | null>(null);
  const textLayerRef = useRef<HTMLDivElement | null>(null);
  const [ratio, setRatio] = useState<number | null>(null);
  // WKWebView 在窗口被遮挡或最小化后可能回收 canvas 的像素缓冲，但 React 节点、
  // pdf.js document 和组件 state 都还在，因此依赖项没有变化，下面的绘制 effect 不会
  // 自己再跑。窗口重新获得焦点或文档恢复可见时递增修订号，复用现有 document 重绘
  // 当前页；无需重新下载整份 PDF，也不会丢失 locator。
  const [resumeRevision, setResumeRevision] = useState(0);

  useEffect(() => {
    const repaint = () => setResumeRevision((current) => current + 1);
    const repaintWhenVisible = () => {
      if (document.visibilityState === "visible") repaint();
    };
    window.addEventListener("focus", repaint);
    document.addEventListener("visibilitychange", repaintWhenVisible);
    return () => {
      window.removeEventListener("focus", repaint);
      document.removeEventListener("visibilitychange", repaintWhenVisible);
    };
  }, []);

  useEffect(() => {
    if (width <= 0) return;
    let cancelled = false;
    let renderTask: { cancel: () => void } | null = null;
    let textLayer: { cancel: () => void } | null = null;

    void (async () => {
      try {
        const pdfjs = await loadPdfjs();
        const page = await doc.getPage(locator);
        if (cancelled) return;

        const base = page.getViewport({ scale: 1 });
        if (base.width > 0) setRatio(base.height / base.width);
        const scale = width / base.width;
        const viewport = page.getViewport({ scale });

        const paintCanvas = async (): Promise<void> => {
          const canvas = canvasRef.current;
          if (canvas === null) return;
          canvas.dataset.renderState = "rendering";
          const dpr = outputScale();
          canvas.width = Math.floor(viewport.width * dpr);
          canvas.height = Math.floor(viewport.height * dpr);
          canvas.style.width = `${Math.floor(viewport.width)}px`;
          canvas.style.height = `${Math.floor(viewport.height)}px`;
          const task = page.render({
            canvas,
            viewport,
            transform: dpr !== 1 ? [dpr, 0, 0, dpr, 0, 0] : undefined,
          });
          renderTask = task;
          await task.promise;
          if (!cancelled) canvas.dataset.renderState = "ready";
        };

        const buildTextLayer = async (): Promise<void> => {
          const textContent = await page.getTextContent();
          // await 之后重新检查：改一次宽度会让本 effect 跑两遍，两次都会走到同一个
          // 容器上 render，后到的那次会把先到的 span 全抹掉。
          if (cancelled) return;
          const container = textLayerRef.current;
          if (container === null) return;
          container.replaceChildren();
          // pdf.js 用 `--total-scale-factor` 摆放 span；不设它，每次渲染都会缩成
          // 同一个极小尺寸，选中等于选不中。
          container.style.setProperty("--total-scale-factor", String(scale));
          const layer = new pdfjs.TextLayer({ textContentSource: textContent, container, viewport });
          textLayer = layer;
          await layer.render();
        };

        // allSettled：画布挂了不该连累文本层，反之亦然。两边都挂了才算这一页渲染失败。
        const outcomes = await Promise.allSettled([paintCanvas(), buildTextLayer()]);
        if (cancelled) return;
        const fatal = outcomes.every(
          (outcome) =>
            outcome.status === "rejected" &&
            (outcome.reason as { name?: string } | null)?.name !== "RenderingCancelledException",
        );
        if (fatal) onFailed();
      } catch (error) {
        // 取消一个正在跑的 render 会抛 RenderingCancelledException，那是翻页时的
        // 正常路径，不是失败。
        const name = (error as { name?: string } | null)?.name ?? "";
        if (!cancelled && name !== "RenderingCancelledException") onFailed();
      }
    })();

    return () => {
      cancelled = true;
      try {
        renderTask?.cancel();
      } catch {
        // 已经结束了。
      }
      try {
        textLayer?.cancel();
      } catch {
        // 已经结束了。
      }
    };
  }, [doc, locator, width, onFailed, resumeRevision]);

  return (
    <>
      <canvas className="reader-canvas" data-render-state="idle" ref={canvasRef} />
      <div
        className="textLayer"
        data-reader-locator={locator}
        ref={textLayerRef}
        style={{ height: ratio === null ? undefined : `${Math.round(width * ratio)}px` }}
      />
    </>
  );
}
