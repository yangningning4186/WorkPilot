"use client";

import { useCallback, useEffect, useLayoutEffect, useRef } from "react";

interface CoworkAutoScrollOptions {
  scopeKey: string | null;
  hasConversation: boolean;
  streaming: boolean;
  contentKey: string;
  eventCount: number;
}

/** 流式期间贴底跟随；用户主动向上滚后立即释放，回到底部再自动恢复。 */
export function useCoworkAutoScroll({
  scopeKey,
  hasConversation,
  streaming,
  contentKey,
  eventCount,
}: CoworkAutoScrollOptions) {
  const containerRef = useRef<HTMLDivElement>(null);
  const autoFollow = useRef(true);

  const pin = useCallback(() => {
    const container = containerRef.current;
    if (container !== null) container.scrollTop = container.scrollHeight;
  }, []);

  useLayoutEffect(() => {
    if (!hasConversation || !autoFollow.current) return;
    pin();
  }, [contentKey, eventCount, hasConversation, pin, streaming]);

  useEffect(() => {
    autoFollow.current = true;
    const frame = requestAnimationFrame(pin);
    return () => cancelAnimationFrame(frame);
  }, [pin, scopeKey]);

  useEffect(() => {
    if (!streaming) return;
    const container = containerRef.current;
    if (container === null) return;
    let frame = 0;
    const schedule = () => {
      if (frame !== 0) return;
      frame = requestAnimationFrame(() => {
        frame = 0;
        if (autoFollow.current) pin();
      });
    };
    const observer = new MutationObserver(schedule);
    observer.observe(container, { childList: true, characterData: true, subtree: true });
    container.addEventListener("load", schedule, true);
    return () => {
      observer.disconnect();
      container.removeEventListener("load", schedule, true);
      if (frame !== 0) cancelAnimationFrame(frame);
    };
  }, [pin, streaming]);

  useEffect(() => {
    if (streaming || !hasConversation) return;
    const container = containerRef.current;
    if (container === null) return;
    let previousHeight = container.scrollHeight;
    let frame = 0;
    const deadline = performance.now() + 4_000;
    const check = () => {
      if (frame !== 0) return;
      frame = requestAnimationFrame(() => {
        frame = 0;
        if (performance.now() > deadline) return;
        const nextHeight = container.scrollHeight;
        if (nextHeight > previousHeight && autoFollow.current) pin();
        previousHeight = nextHeight;
      });
    };
    const observer = new MutationObserver(check);
    observer.observe(container, { childList: true, subtree: true });
    container.addEventListener("load", check, true);
    const stop = window.setTimeout(() => {
      observer.disconnect();
      container.removeEventListener("load", check, true);
    }, 4_000);
    return () => {
      window.clearTimeout(stop);
      observer.disconnect();
      container.removeEventListener("load", check, true);
      if (frame !== 0) cancelAnimationFrame(frame);
    };
  }, [hasConversation, pin, streaming]);

  const handleScroll = useCallback(() => {
    const container = containerRef.current;
    if (container === null) return;
    autoFollow.current =
      container.scrollHeight - container.scrollTop - container.clientHeight < 80;
  }, []);

  useEffect(() => {
    const container = containerRef.current;
    if (container === null) return;
    const onWheel = (event: WheelEvent) => {
      if (event.deltaY < 0) autoFollow.current = false;
    };
    let touchY = 0;
    const onTouchStart = (event: TouchEvent) => {
      touchY = event.touches[0]?.clientY ?? 0;
    };
    const onTouchMove = (event: TouchEvent) => {
      const nextY = event.touches[0]?.clientY ?? 0;
      if (nextY - touchY > 4) autoFollow.current = false;
      touchY = nextY;
    };
    container.addEventListener("wheel", onWheel, { passive: true });
    container.addEventListener("touchstart", onTouchStart, { passive: true });
    container.addEventListener("touchmove", onTouchMove, { passive: true });
    return () => {
      container.removeEventListener("wheel", onWheel);
      container.removeEventListener("touchstart", onTouchStart);
      container.removeEventListener("touchmove", onTouchMove);
    };
  }, [hasConversation]);

  return { containerRef, handleScroll };
}
