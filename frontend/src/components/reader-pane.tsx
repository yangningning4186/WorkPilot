"use client";

import Image from "next/image";
import { useCallback, useEffect, useMemo, useRef, useState } from "react";

import {
  ApiError,
  type ReadingAnnotation,
  type ReadingMaterial,
  createReadingAnnotation,
  deleteReadingAnnotation,
  fetchReadingAnnotations,
  fetchReadingFile,
  fetchReadingMaterial,
  fetchReadingPage,
  fetchReadingUnit,
} from "@/lib/api";
import { loadPdfjs, type PdfDocument } from "@/lib/pdfjs-loader";
import {
  type NormalisedRect,
  cleanQuote,
  normaliseRects,
  unionRect,
} from "@/lib/reading-selection";
import { setReadingMaterial, setReadingViewport } from "@/lib/reading-turn-state";
import type {
  ReadingAnnotatedPayload,
  ReadingGotoPayload,
  ReadingLocation,
} from "@/lib/run-protocol";

import { PdfPageView } from "./pdf-page-view";

/**
 * 阅读器面板：对话旁边的那份文档。
 *
 * 两件事是刻意的产品决定，不是默认行为：
 *
 * * **自动跟随是用户可关的开关，不是限流。** 模型每讨论到一处就该调一次 `reader_goto`，
 *   开关打开时视口跟着走，用户等于在看模型读；关掉之后跳转被忽略，但回答里的 `[p.12]`
 *   仍然可点，滚动位置归用户自己控制。
 * * **换文档靠 `key` 重挂载，不靠在 effect 里逐个 setState 复位。** 复位漏一个字段，
 *   就会出现"新文档、旧高亮"这种没人会去测的组合。父组件用 `key={path}` 挂载本组件。
 * * **批注是持久的，跳转不是。** `reader_goto` 的高亮跟着模型讲到哪里走，翻一页就没了；
 *   批注留在磁盘上，换个会话打开同一份文件还在。所以两者在面板里是两层：临时高亮画
 *   实心块，批注画描边并可点开看备注。**批注不移动视口**——用户可能正在读别的地方。
 * * **批注列表的真相在后端，不在事件里。** 事件只用来触发一次重新拉取；把事件里那条
 *   直接 push 进列表，刷新一次页面就会和后端对不上，用户在别处删掉的那条也不会消失。
 * * **PDF 走 pdf.js 的画布 + 文本层，不再贴后端渲染的 PNG。** 图片没有文本层：用户
 *   选不中、复制不了，也没法把"这一段"交给模型——阅读器因此只能是单向的。文本层一上，
 *   `reading_viewport` 才有东西可报，"这段是什么意思"才谈得上能被解析。pdf.js 加载
 *   失败时**回落到原来的 PNG**：少了选中总好过一份打不开的文档。
 * * **用户划出来的批注，几何仍然由后端从解析结果里取。** 前端量的是浏览器坐标，
 *   ParsedBlock 量的是 PDF 坐标，两套混用会让模型留的高亮和用户划的高亮各偏各的。
 *   代价是引文得能在解析文本里对上；对不上时后端不拒绝、只是不给几何（`verified`
 *   为假），因为那说明是我们的归一化没跟上 PDF 的硬换行，不是用户划错了。
 * * **高亮几何来自后端解析结果，不在前端猜。** `locations` 里带着页面宽高、旋转和坐标
 *   原点（后端约束 3），所以画框只是一次坐标换算；没有这些就得去文本层里模糊匹配引文，
 *   那是一整类会悄悄错位的 bug。`locations` 为空表示"翻页但不高亮"——引文没能逐字对上
 *   时的正确表现，在错误的位置涂一块颜色比不涂更糟。
 */

/** 归一化矩形转成百分比样式；不合法的框直接丢掉而不是画一个歪的。 */
function overlayStyle(location: ReadingLocation): React.CSSProperties | null {
  const [x0, y0, x1, y1] = location.bbox_norm;
  const sane = [x0, y0, x1, y1].every((value) => Number.isFinite(value) && value >= 0 && value <= 1);
  if (!sane || x1 <= x0 || y1 <= y0 || location.coord_origin !== "top_left") return null;
  return {
    left: `${x0 * 100}%`,
    top: `${y0 * 100}%`,
    width: `${(x1 - x0) * 100}%`,
    height: `${(y1 - y0) * 100}%`,
  };
}

function readableError(reason: unknown): string {
  if (reason instanceof ApiError) {
    if (reason.status === 403) return "这个目录还没有授权，先在会话里添加它。";
    if (reason.status === 422) return "这份文件读不了：可能是没有文本层的扫描件。";
    if (reason.status === 404) return "文件不存在。";
  }
  return reason instanceof Error ? reason.message : "打开文档失败。";
}

export interface ReaderPaneProps {
  conversationId: string;
  path: string;
  /** 模型最近一次跳转。`seq` 变化即表示"又跳了一次"，即使落点相同。 */
  jump: (ReadingGotoPayload & { seq: number }) | null;
  /**
   * 用户点回答里 `[p.12]` 发来的请求。
   *
   * 和 `jump` 分成两个 prop 而不是在父组件里合并成一个：合并就要判"模型跳转和用户点击
   * 哪个更新"，而那需要一个跨两条来源的单调计数器。分开之后各自比对各自的序号，谁变
   * React 就为谁重渲染一次，顺序天然正确。
   */
  requestedLocator: { locator: number; nonce: number } | null;
  /**
   * 模型最近一次留下的批注。面板只看 `seq`：变了就重新拉一次列表。
   * 传整个 payload 而不是一个计数器，是为了能比对 `path`——模型可能在标注另一份文档。
   */
  annotated: (ReadingAnnotatedPayload & { seq: number }) | null;
  /**
   * 用户划了一段并点了"问这一段"。只把文字送进输入框，**不替他发送**——他多半还要
   * 在后面接一句自己的问题。真正让模型知道"这段"指哪里的是随请求一起走的
   * `reading_viewport`，这里的文字只是让对话读起来有上下文。
   */
  onAskSelection: (quote: string, locator: number) => void;
  onClose: () => void;
}

export function ReaderPane({
  conversationId,
  path,
  jump,
  requestedLocator,
  annotated,
  onAskSelection,
  onClose,
}: ReaderPaneProps) {
  const [material, setMaterial] = useState<ReadingMaterial | null>(null);
  const [locator, setLocator] = useState(1);
  const [unitText, setUnitText] = useState("");
  const [highlights, setHighlights] = useState<ReadingLocation[]>([]);
  const [quote, setQuote] = useState("");
  const [error, setError] = useState<string | null>(null);
  // 初值就是 true，而不是在 effect 里同步置位：同步 setState 会多触发一轮渲染，
  // 而"有路径就是在加载"这件事在首次渲染时已经知道了。
  const [loading, setLoading] = useState(path.trim() !== "");
  // 已经应用过的跳转序号。用来在渲染期识别"来了一次新跳转"，见下方注释。
  const [seenJumpSeq, setSeenJumpSeq] = useState(0);
  const [seenLocatorNonce, setSeenLocatorNonce] = useState(0);
  const [showOutline, setShowOutline] = useState(false);
  const [autoFollow, setAutoFollow] = useState(true);
  const [annotations, setAnnotations] = useState<ReadingAnnotation[]>([]);
  const [staleAnnotations, setStaleAnnotations] = useState(0);
  const [openNote, setOpenNote] = useState<string | null>(null);
  // 重新拉取批注的触发器。事件来了就 +1；列表本身永远来自后端。
  const [annotationRevision, setAnnotationRevision] = useState(0);
  const [seenAnnotationSeq, setSeenAnnotationSeq] = useState(0);
  const [pageImage, setPageImage] = useState<{ locator: number; url: string } | null>(null);
  const [pageImageError, setPageImageError] = useState<{
    locator: number;
    message: string;
  } | null>(null);
  /**
   * pdf.js 打开的那份文档，连同它属于哪个 material。
   *
   * 把 material 记在同一格里，是为了不在 effect 开头同步 `setPdf(null)` 复位——那样
   * 会多跑一轮渲染，而且"复位"和"加载"是两条时序，中间那一帧显示的是上一份文档的页。
   * 用一格加一次比对，"这份文档还没打开"就是一个可以直接读出来的事实。
   */
  const [pdfState, setPdfState] = useState<{
    materialId: string;
    doc: PdfDocument | null;
    failed: boolean;
  } | null>(null);
  const [pageWidth, setPageWidth] = useState(0);
  /**
   * 当前选区。带着它属于哪一 locator，翻页时不清空、直接判定失效——同一件事用比对
   * 表达比用一个复位 effect 表达更难写错：复位漏一个字段就会出现"新页面、旧选区"。
   */
  const [rawSelection, setSelection] = useState<{
    quote: string;
    locator: number;
    rects: NormalisedRect[];
  } | null>(null);
  const [savingAnnotation, setSavingAnnotation] = useState(false);
  const [annotationNotice, setAnnotationNotice] = useState<string | null>(null);
  const selection = rawSelection !== null && rawSelection.locator === locator ? rawSelection : null;

  const forThisMaterial =
    material !== null && pdfState !== null && pdfState.materialId === material.material_id;
  const pdf = forThisMaterial ? pdfState.doc : null;
  // pdf.js 用不了就退回后端渲染的 PNG。是一个显式状态而不是异常路径：它决定每一页怎么画。
  const pdfUnavailable = forThisMaterial && pdfState.failed;

  const viewportRef = useRef<HTMLDivElement>(null);
  // 选区的度量基准。PDF 是 .reader-page，文本视图是 <pre>，两者都当 HTMLElement 用。
  const pageRef = useRef<HTMLElement>(null);

  useEffect(() => {
    let cancelled = false;
    if (path.trim() === "") return;
    fetchReadingMaterial(conversationId, path)
      .then((loaded) => {
        if (!cancelled) setMaterial(loaded);
      })
      .catch((reason: unknown) => {
        if (!cancelled) setError(readableError(reason));
      })
      .finally(() => {
        if (!cancelled) setLoading(false);
      });
    return () => {
      cancelled = true;
    };
  }, [conversationId, path]);

  // 文本视图才需要拉正文；PDF 直接渲染原页，取文本只会白跑一趟。
  useEffect(() => {
    let cancelled = false;
    if (material === null || material.has_page_image) return;
    fetchReadingUnit(conversationId, material.path, locator)
      .then((unit) => {
        if (!cancelled) setUnitText(unit.text);
      })
      .catch(() => {
        if (!cancelled) setUnitText("");
      });
    return () => {
      cancelled = true;
    };
  }, [conversationId, material, locator]);

  // 打开 PDF 本体交给 pdf.js。整份字节一次取回而不是按页取：文本层要的是同一个
  // document 实例，逐页取回等于每翻一页重新解析一次文件头。
  useEffect(() => {
    let cancelled = false;
    let loaded: PdfDocument | null = null;
    if (material === null || !material.has_page_image) return;
    const materialId = material.material_id;
    void (async () => {
      try {
        const [pdfjs, bytes] = await Promise.all([
          loadPdfjs(),
          fetchReadingFile(conversationId, material.path, material.material_id),
        ]);
        if (cancelled) return;
        loaded = await pdfjs.getDocument({ data: new Uint8Array(bytes) }).promise;
        if (cancelled) {
          void loaded.destroy();
          return;
        }
        setPdfState({ materialId, doc: loaded, failed: false });
      } catch {
        // 打不开就退回后端渲染的 PNG：少了选中，总好过一份打不开的文档。
        if (!cancelled) setPdfState({ materialId, doc: null, failed: true });
      }
    })();
    return () => {
      cancelled = true;
      void loaded?.destroy();
    };
  }, [conversationId, material]);

  // 页面宽度跟着面板走。pdf.js 是按固定 scale 画位图的，不测宽度就只能写死一个值，
  // 面板一拖宽画布要么糊要么留白。
  useEffect(() => {
    const node = viewportRef.current;
    if (node === null) return;
    const measure = () => {
      // 减去 .reader-viewport 的左右内边距，否则画布会把自己撑出滚动条。
      setPageWidth(Math.max(0, node.clientWidth - 28));
    };
    measure();
    const observer = new ResizeObserver(measure);
    observer.observe(node);
    return () => observer.disconnect();
  }, [material]);

  // 阅读器 → 模型的反向通道。写进模块级的格子而不是 state：视口每滚一下就变，
  // 聊天区一旦订阅它，滚动一个像素就要把整条消息列表重渲染一遍。
  useEffect(() => {
    if (material === null) return;
    setReadingMaterial(material.path, material.unit);
    return () => setReadingMaterial(null);
  }, [material]);

  useEffect(() => {
    setReadingViewport({ locator });
  }, [locator]);

  useEffect(() => {
    setReadingViewport({ selection: selection?.quote ?? "" });
  }, [selection]);

  // PDF 原页必须先经 apiFetch 取得 Blob。桌面 sidecar 使用随机端口和 launch-token header，
  // 直接把后端 URL 塞给 img 会绕过这两项，表现就是材料信息正常、页面中间一个破图标。
  useEffect(() => {
    let cancelled = false;
    let objectUrl: string | null = null;
    if (material === null || !material.has_page_image || !pdfUnavailable) return;
    fetchReadingPage(
      conversationId,
      material.path,
      locator,
      material.material_id,
    )
      .then((blob) => {
        if (cancelled) return;
        objectUrl = URL.createObjectURL(blob);
        setPageImageError(null);
        setPageImage({ locator, url: objectUrl });
      })
      .catch((reason: unknown) => {
        if (!cancelled) setPageImageError({ locator, message: readableError(reason) });
      });
    return () => {
      cancelled = true;
      if (objectUrl !== null) URL.revokeObjectURL(objectUrl);
    };
  }, [conversationId, material, locator, pdfUnavailable]);

  useEffect(() => {
    let cancelled = false;
    if (material === null) return;
    fetchReadingAnnotations(conversationId, material.path)
      .then((loaded) => {
        if (cancelled) return;
        setAnnotations(loaded.items);
        setStaleAnnotations(loaded.stale_count);
      })
      // 批注拉不到不该盖掉正文：文档本身仍然可读，少的只是那几块高亮。
      .catch(() => undefined);
    return () => {
      cancelled = true;
    };
  }, [conversationId, material, annotationRevision]);

  // 模型刚标了一条：只重新拉一次列表，**不动视口**。
  if (annotated !== null && material !== null && annotated.seq !== seenAnnotationSeq) {
    setSeenAnnotationSeq(annotated.seq);
    if (annotated.path === material.path) setAnnotationRevision((current) => current + 1);
  }

  // 模型的跳转，在渲染期消化而不是放进 effect。
  //
  // 这是 React 文档里"prop 变化时调整 state"的那个模式：effect 里同步 setState 会多跑
  // 一轮渲染，用户先看到旧页再看到新页闪一下。序号比对是必需的——模型连着两次跳到同一页
  // 时，payload 完全相同，只有 seq 能区分"又跳了一次"和"什么都没发生"。
  //
  // 路径不一致的跳转吞掉但照样记账：用户可能已经在面板里换了另一份文档，此时把视口拽到
  // 上一份文档的第 12 页比不动更让人困惑；不记账则会在下次渲染时反复重试同一条。
  if (jump !== null && material !== null && jump.seq !== seenJumpSeq) {
    setSeenJumpSeq(jump.seq);
    if (jump.path === material.path) {
      setHighlights(jump.locations);
      setQuote(jump.quote);
      if (autoFollow) setLocator(Math.min(Math.max(1, jump.locator), material.unit_count));
    }
  }

  // 用户点了回答里的 `[p.12]`。和模型跳转同一个模式，但不画高亮——那一处引用的几何
  // 只有模型知道，用户点的只是"带我去这一页"。
  if (requestedLocator !== null && material !== null && requestedLocator.nonce !== seenLocatorNonce) {
    setSeenLocatorNonce(requestedLocator.nonce);
    setLocator(Math.min(Math.max(1, requestedLocator.locator), material.unit_count));
    setHighlights([]);
    setQuote("");
  }

  useEffect(() => {
    viewportRef.current?.scrollTo({ top: 0, behavior: "smooth" });
  }, [locator]);

  const goto = useCallback(
    (target: number) => {
      if (material === null) return;
      setLocator(Math.min(Math.max(1, target), material.unit_count));
      // 手动翻页后旧高亮不再成立：它属于模型上一次引用的那一处。选区不必在这里清，
      // 它带着自己的 locator，翻页之后自然判定失效。
      setHighlights([]);
      setQuote("");
      setShowOutline(false);
      setAnnotationNotice(null);
    },
    [material],
  );

  /**
   * 把浏览器的选区收成"引文 + locator + 一组框"。
   *
   * 引文要折成单行才提交：pdf.js 的文本层在 PDF 原本换行的地方就换行，原始选区里
   * 全是硬换行，直接送后端会被判成"文档里没有这句话"——而用户明明是照着屏幕划的。
   *
   * 框只用来在本地画一层选中提示，**不送后端**：批注的几何来自解析结果（约束 3）。
   */
  const captureSelection = useCallback(() => {
    if (material === null) return;
    const active = window.getSelection();
    const container = pageRef.current;
    if (active === null || active.isCollapsed || container === null) {
      setSelection(null);
      return;
    }
    if (!container.contains(active.anchorNode)) return;
    const quote = cleanQuote(active.toString());
    if (quote === "") {
      setSelection(null);
      return;
    }
    const range = active.getRangeAt(0);
    const box = container.getBoundingClientRect();
    setSelection({
      quote,
      locator,
      rects: normaliseRects(Array.from(range.getClientRects()), box),
    });
  }, [material, locator]);

  const askAboutSelection = useCallback(() => {
    if (selection === null) return;
    // 只把选区交给输入框，不替用户按发送：他多半还要在这段文字后面接一句自己的问题。
    onAskSelection(selection.quote, selection.locator);
  }, [selection, onAskSelection]);

  const annotateSelection = useCallback(async () => {
    if (selection === null || material === null) return;
    setSavingAnnotation(true);
    try {
      const created = await createReadingAnnotation(conversationId, {
        path: material.path,
        locator: selection.locator,
        quote: selection.quote,
      });
      setAnnotationNotice(
        created.verified
          ? null
          : "记下了，但这段文字在解析出来的原文里没能逐字对上，所以画不出高亮框。",
      );
      setSelection(null);
      window.getSelection()?.removeAllRanges();
      setAnnotationRevision((current) => current + 1);
    } catch (reason: unknown) {
      setAnnotationNotice(readableError(reason));
    } finally {
      setSavingAnnotation(false);
    }
  }, [conversationId, material, selection]);

  const removeAnnotation = useCallback(
    async (annotationId: string) => {
      if (material === null) return;
      setOpenNote(null);
      try {
        await deleteReadingAnnotation(conversationId, material.path, annotationId);
      } catch {
        // 删失败就当没删：下面这次重拉会把它原样带回来，用户看到的仍是真相。
      }
      setAnnotationRevision((current) => current + 1);
    },
    [conversationId, material],
  );

  const handlePdfFailure = useCallback(() => {
    setPdfState((current) =>
      current === null ? current : { ...current, doc: null, failed: true },
    );
  }, []);

  /**
   * 把操作条摆在选区下方。用外接框的下沿而不是鼠标位置：拖选可以从下往上拉，
   * 按鼠标落点摆会让条子压在被选中的文字上。
   */
  const selectionAnchor = useMemo((): React.CSSProperties | null => {
    if (selection === null) return null;
    const box = unionRect(selection.rects);
    if (box === null) return null;
    return { left: `${box[0] * 100}%`, top: `${box[3] * 100}%` };
  }, [selection]);

  const label = material?.unit === "section" ? "节" : "页";
  const currentPageImageUrl = pageImage?.locator === locator ? pageImage.url : null;
  const currentPageImageError =
    pageImageError?.locator === locator ? pageImageError.message : null;
  const boxes = useMemo(
    () =>
      highlights
        .filter((location) => location.page_no === locator)
        .map(overlayStyle)
        .filter((style): style is React.CSSProperties => style !== null),
    [highlights, locator],
  );
  /** 当前这一 locator 上的批注。有几何的画框，没几何的（非 PDF）只进下方列表。 */
  const pageAnnotations = useMemo(
    () => annotations.filter((item) => item.locator === locator),
    [annotations, locator],
  );
  const annotationBoxes = useMemo(
    () =>
      pageAnnotations.flatMap((item) =>
        item.locations
          .filter((location) => location.page_no === locator)
          .map((location) => ({ annotation: item, style: overlayStyle(location) }))
          .filter(
            (entry): entry is { annotation: ReadingAnnotation; style: React.CSSProperties } =>
              entry.style !== null,
          ),
      ),
    [pageAnnotations, locator],
  );

  return (
    <aside aria-label="阅读器" className="reader-pane">
      <header className="reader-head">
        <div className="reader-title">
          <strong>{material?.filename ?? "阅读器"}</strong>
          {material !== null && (
            <small>
              {material.unit_count} {label} · {material.parser}
            </small>
          )}
        </div>
        <div className="reader-head-actions">
          <button
            aria-pressed={showOutline}
            disabled={material === null || material.outline.length === 0}
            onClick={() => setShowOutline((current) => !current)}
            title="大纲"
            type="button"
          >
            大纲
          </button>
          <button
            aria-checked={autoFollow}
            className={autoFollow ? "is-on" : undefined}
            onClick={() => setAutoFollow((current) => !current)}
            role="switch"
            title="打开后视口跟随模型的每一次跳转；关掉后引用仍然可点，滚动由你控制"
            type="button"
          >
            自动跟随
          </button>
          <button aria-label="关闭阅读器" onClick={onClose} type="button">
            ✕
          </button>
        </div>
      </header>

      {error !== null && <p className="reader-error">{error}</p>}
      {loading && <p className="reader-hint">正在解析文档…</p>}
      {!loading && error === null && path.trim() === "" && (
        <p className="reader-hint">还没有选文档。在输入框里给出工作区内的路径即可打开。</p>
      )}

      {showOutline && material !== null && (
        <nav aria-label="文档大纲" className="reader-outline">
          {material.outline[0]?.synthesised === true && (
            <p className="reader-hint">这份大纲是用每{label}首行凑的，只能当线索。</p>
          )}
          {material.outline.map((entry, index) => (
            <button
              key={`${entry.locator}-${index}`}
              onClick={() => goto(entry.locator)}
              style={{ paddingLeft: `${8 + (entry.level - 1) * 12}px` }}
              type="button"
            >
              <span>{entry.title || "（无标题）"}</span>
              <small>
                {entry.locator} {label}
              </small>
            </button>
          ))}
        </nav>
      )}

      {material !== null && (
        <>
          <div
            className="reader-viewport"
            onMouseUp={captureSelection}
            onTouchEnd={captureSelection}
            ref={viewportRef}
          >
            {material.has_page_image ? (
              <div className="reader-page" ref={pageRef as React.RefObject<HTMLDivElement>}>
                {pdf !== null && pageWidth > 0 ? (
                  <PdfPageView
                    doc={pdf}
                    locator={locator}
                    onFailed={handlePdfFailure}
                    width={pageWidth}
                  />
                ) : pdfUnavailable ? (
                  currentPageImageError !== null ? (
                    <p className="reader-error">{currentPageImageError}</p>
                  ) : currentPageImageUrl !== null ? (
                    <Image
                      alt={`${material.filename} 第 ${locator} 页`}
                      height={1400}
                      onError={() =>
                        setPageImageError({
                          locator,
                          message: "页面图片解码失败，请重新打开阅读器。",
                        })
                      }
                      sizes="(max-width: 1200px) 90vw, 460px"
                      src={currentPageImageUrl}
                      unoptimized
                      width={990}
                    />
                  ) : (
                    <p className="reader-hint">正在渲染第 {locator} 页…</p>
                  )
                ) : (
                  <p className="reader-hint">正在打开文档…</p>
                )}
                {boxes.map((style, index) => (
                  <span className="reader-highlight" key={index} style={style} />
                ))}
                {annotationBoxes.map(({ annotation, style }, index) => (
                  <button
                    className={`reader-annotation ${annotation.color}`}
                    key={`${annotation.id}-${index}`}
                    onClick={() =>
                      setOpenNote((current) => (current === annotation.id ? null : annotation.id))
                    }
                    style={style}
                    title={annotation.note}
                    type="button"
                  />
                ))}
                {selectionAnchor !== null && (
                  <div className="reader-selection-actions" style={selectionAnchor}>
                    <button disabled={savingAnnotation} onClick={askAboutSelection} type="button">
                      问这一段
                    </button>
                    <button
                      disabled={savingAnnotation}
                      onClick={() => void annotateSelection()}
                      type="button"
                    >
                      {savingAnnotation ? "保存中…" : "高亮"}
                    </button>
                  </div>
                )}
              </div>
            ) : (
              <pre className="reader-text" ref={pageRef as React.RefObject<HTMLPreElement>}>
                {unitText}
              </pre>
            )}
          </div>

          {annotationNotice !== null && <p className="reader-hint">{annotationNotice}</p>}

          {quote !== "" && (
            <p className="reader-quote" title={quote}>
              模型引用：“{quote}”
            </p>
          )}

          {pageAnnotations.length > 0 && (
            <ul aria-label={`第 ${locator} ${label}的批注`} className="reader-annotation-list">
              {pageAnnotations.map((item) => (
                <li className={openNote === item.id ? "is-open" : undefined} key={item.id}>
                  <button
                    className={`swatch ${item.color}`}
                    onClick={() =>
                      setOpenNote((current) => (current === item.id ? null : item.id))
                    }
                    type="button"
                  >
                    <span>{item.note}</span>
                  </button>
                  {openNote === item.id && (
                    <div className="reader-annotation-detail">
                      <blockquote>{item.quote}</blockquote>
                      <button onClick={() => void removeAnnotation(item.id)} type="button">
                        删除批注
                      </button>
                    </div>
                  )}
                </li>
              ))}
            </ul>
          )}

          {staleAnnotations > 0 && (
            <p className="reader-hint">
              另有 {staleAnnotations} 条批注属于这份文件的旧版本，不再显示——文件内容变了之后，
              原来的位置可能已经指向别的文字。
            </p>
          )}

          <footer className="reader-foot">
            <button disabled={locator <= 1} onClick={() => goto(locator - 1)} type="button">
              上一{label}
            </button>
            <span>
              第 {locator} / {material.unit_count} {label}
            </span>
            <button
              disabled={locator >= material.unit_count}
              onClick={() => goto(locator + 1)}
              type="button"
            >
              下一{label}
            </button>
          </footer>
        </>
      )}
    </aside>
  );
}
