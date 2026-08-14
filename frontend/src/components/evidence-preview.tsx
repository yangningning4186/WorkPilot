"use client";

import Image from "next/image";
import { useEffect, useMemo, useState } from "react";

import { sourceFileUrl, sourcePageUrl } from "@/lib/api";
import type { CitationLocation, CitationPayload } from "@/lib/run-protocol";

function ExternalLinkIcon() {
  return (
    <svg aria-hidden="true" viewBox="0 0 20 20">
      <path d="M11.5 3.5h5v5M16.2 3.8l-7.4 7.4M8 5H4.8A1.8 1.8 0 0 0 3 6.8v8.4A1.8 1.8 0 0 0 4.8 17h8.4a1.8 1.8 0 0 0 1.8-1.8V12" />
    </svg>
  );
}

function CloseIcon() {
  return (
    <svg aria-hidden="true" viewBox="0 0 20 20">
      <path d="m5 5 10 10M15 5 5 15" />
    </svg>
  );
}

function safeBox(location: CitationLocation): [number, number, number, number] | null {
  const [x0, y0, x1, y1] = location.bbox_norm;
  if (
    location.coord_origin !== "top_left" ||
    ![x0, y0, x1, y1].every((value) => Number.isFinite(value) && value >= 0 && value <= 1) ||
    x1 <= x0 ||
    y1 <= y0
  ) {
    return null;
  }
  return [x0, y0, x1, y1];
}

function PdfPage({ citation }: { citation: CitationPayload }) {
  const pages = useMemo(
    () => [...new Set(citation.locations.map((location) => location.page_no))].sort((a, b) => a - b),
    [citation.locations],
  );
  const [pageNo, setPageNo] = useState(pages[0] ?? 1);
  const [imageReady, setImageReady] = useState(false);
  const [imageFailed, setImageFailed] = useState(false);
  const locations = citation.locations.filter((location) => location.page_no === pageNo);
  const pageMeta = locations[0] ?? citation.locations[0];
  const aspectRatio =
    pageMeta !== undefined && pageMeta.page_width > 0 && pageMeta.page_height > 0
      ? `${pageMeta.page_width} / ${pageMeta.page_height}`
      : "210 / 297";

  return (
    <div className="pdf-preview">
      {pages.length > 1 && (
        <div className="page-tabs" aria-label="引用所在页面">
          {pages.map((page) => (
            <button
              key={page}
              className={page === pageNo ? "active" : undefined}
              onClick={() => {
                setPageNo(page);
                setImageReady(false);
                setImageFailed(false);
              }}
              type="button"
            >
              第 {page} 页
            </button>
          ))}
        </div>
      )}

      <div className="pdf-canvas" style={{ aspectRatio }}>
        {!imageReady && !imageFailed && <div className="preview-loading">正在渲染原文页…</div>}
        {imageFailed ? (
          <div className="preview-error">原文页暂时无法渲染，请打开完整文件查看。</div>
        ) : (
          <Image
            alt={`${citation.title} 第 ${pageNo} 页`}
            fill
            onError={() => setImageFailed(true)}
            onLoad={() => setImageReady(true)}
            priority
            sizes="(max-width: 900px) 100vw, 42vw"
            src={sourcePageUrl(citation.version_id, pageNo)}
            unoptimized
          />
        )}
        {imageReady &&
          locations.map((location, index) => {
            const box = safeBox(location);
            if (box === null) return null;
            const [x0, y0, x1, y1] = box;
            return (
              <span
                key={`${pageNo}-${index}`}
                aria-label="引用原文高亮"
                className="bbox-highlight"
                style={{
                  left: `${x0 * 100}%`,
                  top: `${y0 * 100}%`,
                  width: `${(x1 - x0) * 100}%`,
                  height: `${(y1 - y0) * 100}%`,
                }}
              />
            );
          })}
      </div>
    </div>
  );
}

function HighlightedMarkdown({ citation }: { citation: CitationPayload }) {
  const [content, setContent] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    const controller = new AbortController();
    void fetch(sourceFileUrl(citation.version_id), {
      credentials: "include",
      signal: controller.signal,
    })
      .then(async (response) => {
        if (!response.ok) throw new Error(`HTTP ${response.status}`);
        return response.text();
      })
      .then(setContent)
      .catch((reason: unknown) => {
        if (reason instanceof DOMException && reason.name === "AbortError") return;
        setError("原文暂时无法载入，请打开完整文件查看。");
      });
    return () => controller.abort();
  }, [citation.version_id]);

  if (error !== null) return <div className="preview-error">{error}</div>;
  if (content === null) return <div className="preview-loading">正在载入原文…</div>;

  const quoteIndex = content.indexOf(citation.quote);
  return (
    <div className="markdown-preview" tabIndex={0}>
      {quoteIndex >= 0 ? (
        <pre>
          {content.slice(0, quoteIndex)}
          <mark>{citation.quote}</mark>
          {content.slice(quoteIndex + citation.quote.length)}
        </pre>
      ) : (
        <>
          <div className="quote-fallback">
            <span>解析后的引用片段</span>
            <p>{citation.quote}</p>
          </div>
          <pre>{content}</pre>
        </>
      )}
    </div>
  );
}

export function EvidencePreview({
  citation,
  onClose,
}: {
  citation: CitationPayload;
  onClose: () => void;
}) {
  const isPdf = citation.source_uri.toLowerCase().endsWith(".pdf") || citation.locations.length > 0;
  const firstPage = citation.locations[0]?.page_no;
  const originalUrl = `${sourceFileUrl(citation.version_id)}${firstPage === undefined ? "" : `#page=${firstPage}`}`;

  return (
    <aside className="evidence-panel" aria-label="引用原文预览">
      <header className="evidence-header">
        <div>
          <div className="eyebrow">原文证据 · {citation.citation_id}</div>
          <h2>{citation.title}</h2>
          <p title={citation.source_uri}>{citation.source_uri}</p>
        </div>
        <button className="icon-button evidence-close" onClick={onClose} type="button">
          <CloseIcon />
          <span className="sr-only">关闭原文预览</span>
        </button>
      </header>

      {citation.heading_path.length > 0 && (
        <div className="breadcrumb">{citation.heading_path.join(" / ")}</div>
      )}

      <div className="evidence-document">
        {isPdf ? <PdfPage citation={citation} /> : <HighlightedMarkdown citation={citation} />}
      </div>

      <footer className="evidence-footer">
        <p>高亮区域对应回答所引用的原文位置。</p>
        <a href={originalUrl} rel="noreferrer" target="_blank">
          打开完整原文
          <ExternalLinkIcon />
        </a>
      </footer>
    </aside>
  );
}
