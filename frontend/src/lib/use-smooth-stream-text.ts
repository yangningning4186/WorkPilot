"use client";

import { useEffect, useRef, useState } from "react";

interface SmoothStreamOptions {
  maxCharsPerFrame?: number;
  minCharsPerFrame?: number;
  catchUpDivisor?: number;
}

/**
 * 把上游突发到达的 delta 与屏幕可见增长解耦。
 *
 * 思路沿用 OpenWorker 的 rAF 实现：积压越多，每帧追赶越快；终态立即
 * 对齐完整文本，避免最后几个字还在打字机队列里。
 */
export function useSmoothStreamText(
  content: string,
  streaming: boolean,
  options: SmoothStreamOptions = {},
): string {
  const {
    maxCharsPerFrame = 120,
    minCharsPerFrame = 2,
    catchUpDivisor = 5,
  } = options;
  const [shown, setShown] = useState(content);
  const shownLength = useRef(content.length);
  const frame = useRef(0);

  useEffect(() => {
    if (!streaming) {
      if (frame.current !== 0) cancelAnimationFrame(frame.current);
      frame.current = 0;
      if (shownLength.current !== content.length || shown !== content) {
        shownLength.current = content.length;
        setShown(content);
      }
      return;
    }
    if (shownLength.current > content.length) {
      shownLength.current = content.length;
      setShown(content);
      return;
    }
    if (shownLength.current >= content.length) return;

    const reveal = () => {
      frame.current = 0;
      const backlog = content.length - shownLength.current;
      if (backlog <= 0) return;
      const advance = Math.min(
        maxCharsPerFrame,
        Math.max(minCharsPerFrame, Math.ceil(backlog / catchUpDivisor)),
      );
      const nextLength = Math.min(content.length, shownLength.current + advance);
      shownLength.current = nextLength;
      setShown(content.slice(0, nextLength));
      if (nextLength < content.length) frame.current = requestAnimationFrame(reveal);
    };
    frame.current = requestAnimationFrame(reveal);
    return () => {
      if (frame.current !== 0) cancelAnimationFrame(frame.current);
      frame.current = 0;
    };
    // shown 由同一条 rAF 链更新；把它放进依赖会每帧拆掉并重建链。
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [content, streaming, maxCharsPerFrame, minCharsPerFrame, catchUpDivisor]);

  return shown;
}
