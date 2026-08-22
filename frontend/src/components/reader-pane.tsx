"use client";

import Image from "next/image";
import { useCallback, useEffect, useMemo, useRef, useState } from "react";

import {
  ApiError,
  type ReadingMaterial,
  fetchReadingMaterial,
  fetchReadingUnit,
  readingPageUrl,
} from "@/lib/api";
import type { ReadingGotoPayload, ReadingLocation } from "@/lib/run-protocol";

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
  onClose: () => void;
}

export function ReaderPane({
  conversationId,
  path,
  jump,
  requestedLocator,
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
  const viewportRef = useRef<HTMLDivElement>(null);

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
      // 手动翻页后旧高亮不再成立：它属于模型上一次引用的那一处。
      setHighlights([]);
      setQuote("");
      setShowOutline(false);
    },
    [material],
  );

  const label = material?.unit === "section" ? "节" : "页";
  const boxes = useMemo(
    () =>
      highlights
        .filter((location) => location.page_no === locator)
        .map(overlayStyle)
        .filter((style): style is React.CSSProperties => style !== null),
    [highlights, locator],
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
          <div className="reader-viewport" ref={viewportRef}>
            {material.has_page_image ? (
              <div className="reader-page">
                <Image
                  alt={`${material.filename} 第 ${locator} 页`}
                  height={1400}
                  sizes="(max-width: 1200px) 90vw, 460px"
                  src={readingPageUrl(conversationId, material.path, locator, material.material_id)}
                  unoptimized
                  width={990}
                />
                {boxes.map((style, index) => (
                  <span className="reader-highlight" key={index} style={style} />
                ))}
              </div>
            ) : (
              <pre className="reader-text">{unitText}</pre>
            )}
          </div>

          {quote !== "" && (
            <p className="reader-quote" title={quote}>
              模型引用：“{quote}”
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
