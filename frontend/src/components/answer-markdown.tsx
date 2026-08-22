"use client";

import { memo, useMemo } from "react";
import Markdown from "react-markdown";
import remarkGfm from "remark-gfm";
import type { Element, Root, Text } from "hast";
import { visit } from "unist-util-visit";

/**
 * 回答正文的 Markdown 渲染。
 *
 * 三条约束决定了它不能只是 `<Markdown>{text}</Markdown>`：
 *
 * 1. **正文是流式到达的**。每来一个 delta 就全量重解析，长回答会越写越卡
 *    （docs/08 B4）。这里把已经写完的块缓存住，只有正在生长的最后一块重解析。
 * 2. **证据是不可信数据**。答案由模型依据检索到的文档生成，文档里可能藏着
 *    HTML 或 `javascript:` 链接。react-markdown 默认不渲染裸 HTML，链接协议
 *    也由 defaultUrlTransform 过滤——这两个默认值是这里的安全边界，
 *    **不要**加 rehype-raw 或自定义 urlTransform 把它们打开。
 * 3. **`[S1]` 是引用锚点不是普通文字**。渲染成可点的 chip，点了就选中对应引用，
 *    与右侧原文预览是同一套选中状态。
 * 4. **`[p.12]` 是阅读器 locator**。论文阅读档里模型按这个形式标出处，点了把阅读器
 *    翻到那一页。和 `[S1]` 走同一条 rehype 通道，因此同样不会误伤代码块里的字样。
 */

const CITATION_TAG = "citation-ref";
const CITATION_RE = /\[(S\d+)\]/g;

const LOCATOR_TAG = "locator-ref";
/**
 * `[p.12]`、`[p.12,17]`、`[p.12-14]`。
 *
 * 后面紧跟 `(` 的不匹配——那是一个 Markdown 链接的标签，不是引用。
 */
const LOCATOR_RE = /\[p\.\s*(\d[\d\s,\u2013\u2014-]*)\](?!\()/gi;
/** 一个 `[p.a-b]` 最多展开成几个 locator，防止 `[p.1-9999]` 铺满整段。 */
const MAX_LOCATOR_SPAN = 40;

/** 从 `[p.…]` 的内部文本解析出升序去重的 locator。 */
export function parseLocators(raw: string): number[] {
  const found = new Set<number>();
  for (const chunk of raw.split(",")) {
    const range = chunk.trim().match(/^(\d+)(?:\s*[\u2013\u2014-]\s*(\d+))?$/);
    if (range === null) continue;
    const start = Number(range[1]);
    const end = range[2] === undefined ? start : Number(range[2]);
    const [low, high] = start <= end ? [start, end] : [end, start];
    for (let value = low; value <= Math.min(high, low + MAX_LOCATOR_SPAN - 1); value += 1) {
      if (value >= 1) found.add(value);
    }
  }
  return [...found].sort((a, b) => a - b);
}

/**
 * 把文本里的 `[S1]` 换成自定义元素节点。
 *
 * 放在 rehype 阶段而不是自己 split children：这样它只作用于真正的文本节点，
 * 代码块、行内代码、链接地址里的 `[S1]` 不会被误伤。
 */
function rehypeCitationRefs() {
  return (tree: Root) => {
    visit(tree, "text", (node: Text, index, parent) => {
      if (parent === undefined || index === undefined) return;
      // 代码块里的内容原样保留：论文里出现 [S1] 字样是常事。
      if (parent.type === "element" && (parent.tagName === "code" || parent.tagName === "pre")) {
        return;
      }
      const matches = [...node.value.matchAll(CITATION_RE)];
      if (matches.length === 0) return;

      const children: (Element | Text)[] = [];
      let cursor = 0;
      for (const match of matches) {
        const start = match.index;
        if (start > cursor) {
          children.push({ type: "text", value: node.value.slice(cursor, start) });
        }
        children.push({
          type: "element",
          tagName: CITATION_TAG,
          properties: { citationId: match[1] },
          children: [{ type: "text", value: match[1] as string }],
        });
        cursor = start + match[0].length;
      }
      if (cursor < node.value.length) {
        children.push({ type: "text", value: node.value.slice(cursor) });
      }
      parent.children.splice(index, 1, ...children);
      return index + children.length;
    });
  };
}

/** 同上，但换成 `[p.N]`：一个 locator 一个 chip，用户能单独点其中任意一个。 */
function rehypeLocatorRefs() {
  return (tree: Root) => {
    visit(tree, "text", (node: Text, index, parent) => {
      if (parent === undefined || index === undefined) return;
      if (parent.type === "element" && (parent.tagName === "code" || parent.tagName === "pre")) {
        return;
      }
      const matches = [...node.value.matchAll(LOCATOR_RE)];
      if (matches.length === 0) return;

      const children: (Element | Text)[] = [];
      let cursor = 0;
      for (const match of matches) {
        const locators = parseLocators(match[1] as string);
        if (locators.length === 0) continue;
        const start = match.index;
        if (start > cursor) {
          children.push({ type: "text", value: node.value.slice(cursor, start) });
        }
        for (const locator of locators) {
          children.push({
            type: "element",
            tagName: LOCATOR_TAG,
            properties: { locator: String(locator) },
            children: [{ type: "text", value: `p.${locator}` }],
          });
        }
        cursor = start + match[0].length;
      }
      if (children.length === 0) return;
      if (cursor < node.value.length) {
        children.push({ type: "text", value: node.value.slice(cursor) });
      }
      parent.children.splice(index, 1, ...children);
      return index + children.length;
    });
  };
}

/**
 * 按块切分，且不切开围栏代码块。
 *
 * 空行是 Markdown 的块分隔符，但代码块内部的空行不是——按 `\n\n` 硬切会把
 * 代码块拦腰截断，渲染出两段坏掉的内容。
 */
export function splitMarkdownBlocks(text: string): string[] {
  const lines = text.split("\n");
  const blocks: string[] = [];
  let current: string[] = [];
  let inFence = false;

  const flush = () => {
    if (current.length > 0) {
      blocks.push(current.join("\n"));
      current = [];
    }
  };

  for (const line of lines) {
    if (/^\s*(```|~~~)/.test(line)) {
      inFence = !inFence;
      current.push(line);
      continue;
    }
    if (!inFence && line.trim() === "") {
      flush();
      continue;
    }
    current.push(line);
  }
  flush();
  return blocks;
}

function CitationChip({
  citationId,
  onSelect,
  active,
}: {
  citationId: string;
  onSelect: ((citationId: string) => void) | undefined;
  active: boolean;
}) {
  if (onSelect === undefined) {
    return <span className="citation-chip static">{citationId}</span>;
  }
  return (
    <button
      aria-label={`查看引用 ${citationId} 的原文`}
      aria-pressed={active}
      className={`citation-chip${active ? " active" : ""}`}
      onClick={() => onSelect(citationId)}
      type="button"
    >
      {citationId}
    </button>
  );
}

function LocatorChip({
  locator,
  onSelect,
}: {
  locator: number;
  onSelect: ((locator: number) => void) | undefined;
}) {
  if (onSelect === undefined) {
    return <span className="locator-chip static">p.{locator}</span>;
  }
  return (
    <button
      aria-label={`在阅读器中打开第 ${locator} 处`}
      className="locator-chip"
      onClick={() => onSelect(locator)}
      type="button"
    >
      p.{locator}
    </button>
  );
}

interface BlockProps {
  source: string;
  onSelectCitation: ((citationId: string) => void) | undefined;
  onSelectLocator: ((locator: number) => void) | undefined;
  activeCitationId: string | null;
}

/**
 * 一个已完成的块。
 *
 * memo 的比较刻意忽略 onSelectCitation 的引用变化：调用方每次渲染都可能传新函数，
 * 那会让缓存彻底失效，增量渲染就白做了。activeCitationId 变化时只有含该引用的块
 * 需要重画，这里为简单起见让所有块重画——切换引用不是高频操作。
 */
const MarkdownBlock = memo(
  function MarkdownBlock({
    source,
    onSelectCitation,
    onSelectLocator,
    activeCitationId,
  }: BlockProps) {
    return (
      <Markdown
        components={{
          // @ts-expect-error react-markdown 的 components 类型只覆盖标准 HTML 标签，
          // 自定义标签是 rehype 插件注入的，运行时按 tagName 匹配。
          [CITATION_TAG]: ({ citationId }: { citationId?: string }) => (
            <CitationChip
              active={citationId !== undefined && citationId === activeCitationId}
              citationId={citationId ?? "?"}
              onSelect={onSelectCitation}
            />
          ),
          // 同样是 rehype 注入的自定义标签；上面那条 ts-expect-error 已覆盖整个对象字面量，
          // 再加一条会被判成多余的抑制。
          [LOCATOR_TAG]: ({ locator }: { locator?: string }) => (
            <LocatorChip locator={Number(locator ?? 0)} onSelect={onSelectLocator} />
          ),
          // 站外链接一律新窗口打开并断开 opener，避免答案里的链接反控本页。
          a: ({ children, href }) => (
            <a href={href} rel="noreferrer noopener" target="_blank">
              {children}
            </a>
          ),
        }}
        rehypePlugins={[rehypeCitationRefs, rehypeLocatorRefs]}
        remarkPlugins={[remarkGfm]}
      >
        {source}
      </Markdown>
    );
  },
  (previous, next) =>
    previous.source === next.source && previous.activeCitationId === next.activeCitationId,
);

export function AnswerMarkdown({
  text,
  onSelectCitation,
  onSelectLocator,
  activeCitationId = null,
}: {
  text: string;
  onSelectCitation?: (citationId: string) => void;
  /** 论文阅读档：点 `[p.12]` 把阅读器翻过去。不传就渲染成静态标记。 */
  onSelectLocator?: (locator: number) => void;
  activeCitationId?: string | null;
}) {
  const blocks = useMemo(() => splitMarkdownBlocks(text), [text]);

  return (
    <article className="answer-copy">
      {blocks.map((block, index) => (
        <MarkdownBlock
          activeCitationId={activeCitationId}
          // 用下标而不是内容做 key: 流式时最后一块的内容一直在变, 用内容当 key
          // 会让它每次都被卸载重建, 正在生长的那块反而最该复用。
          key={index}
          onSelectCitation={onSelectCitation}
          onSelectLocator={onSelectLocator}
          source={block}
        />
      ))}
    </article>
  );
}
