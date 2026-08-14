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
 */

const CITATION_TAG = "citation-ref";
const CITATION_RE = /\[(S\d+)\]/g;

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

interface BlockProps {
  source: string;
  onSelectCitation: ((citationId: string) => void) | undefined;
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
  function MarkdownBlock({ source, onSelectCitation, activeCitationId }: BlockProps) {
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
          // 站外链接一律新窗口打开并断开 opener，避免答案里的链接反控本页。
          a: ({ children, href }) => (
            <a href={href} rel="noreferrer noopener" target="_blank">
              {children}
            </a>
          ),
        }}
        rehypePlugins={[rehypeCitationRefs]}
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
  activeCitationId = null,
}: {
  text: string;
  onSelectCitation?: (citationId: string) => void;
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
          source={block}
        />
      ))}
    </article>
  );
}
