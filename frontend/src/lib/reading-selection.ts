/**
 * 选区几何与引文清洗。纯函数，调用方把量好的矩形传进来，因此不依赖 DOM 也就测得动。
 *
 * 输出的归一化矩形只用来**画选区提示**，不进后端：批注的几何来自解析结果（后端约束
 * 3），浏览器量出来的框和 ParsedBlock 的框不是同一套坐标，混用会让同一份文件上模型
 * 留的高亮和用户划的高亮各偏各的。
 */

export interface Box {
  left: number;
  top: number;
  width: number;
  height: number;
}

export type NormalisedRect = [number, number, number, number];

/** 比这更细或更矮的矩形（px）是选区拖拽的碎屑，不是文字。 */
const MIN_RECT_PX = 2;
/** 纵向重叠超过这个比例就算同一行。 */
const SAME_LINE_OVERLAP = 0.5;

function clamp01(value: number): number {
  if (!Number.isFinite(value)) return 0;
  return Math.min(1, Math.max(0, value));
}

/**
 * 按纵向重叠把矩形并成文本行。
 *
 * 用重叠而不是"top 相等"：一行里的上下标和混排字号 top 并不相同，但它们显然属于同一行。
 */
export function mergeRectsByLine(rects: Box[]): Box[] {
  const usable = rects.filter((rect) => rect.width >= MIN_RECT_PX && rect.height >= MIN_RECT_PX);
  if (usable.length === 0) return [];

  const sorted = [...usable].sort((a, b) => a.top - b.top || a.left - b.left);
  const lines: Box[][] = [];
  for (const rect of sorted) {
    const line = lines.find((candidate) => {
      const probe = candidate[0];
      if (probe === undefined) return false;
      const overlap =
        Math.min(probe.top + probe.height, rect.top + rect.height) -
        Math.max(probe.top, rect.top);
      const shorter = Math.min(probe.height, rect.height);
      return shorter > 0 && overlap / shorter >= SAME_LINE_OVERLAP;
    });
    if (line !== undefined) line.push(rect);
    else lines.push([rect]);
  }

  return lines.map((line) => {
    const left = Math.min(...line.map((item) => item.left));
    const top = Math.min(...line.map((item) => item.top));
    const right = Math.max(...line.map((item) => item.left + item.width));
    const bottom = Math.max(...line.map((item) => item.top + item.height));
    return { left, top, width: right - left, height: bottom - top };
  });
}

/**
 * 视口坐标的矩形 → 相对 `container` 的归一化矩形。
 *
 * 两边都是 `getBoundingClientRect` 的口径，所以滚动位置自然抵消，不需要额外补偏移。
 */
export function normaliseRects(rects: Box[], container: Box): NormalisedRect[] {
  if (container.width <= 0 || container.height <= 0) return [];
  return mergeRectsByLine(rects)
    .map((rect): NormalisedRect => {
      const x0 = clamp01((rect.left - container.left) / container.width);
      const y0 = clamp01((rect.top - container.top) / container.height);
      const x1 = clamp01((rect.left + rect.width - container.left) / container.width);
      const y1 = clamp01((rect.top + rect.height - container.top) / container.height);
      return [Math.min(x0, x1), Math.min(y0, y1), Math.max(x0, x1), Math.max(y0, y1)];
    })
    .filter(([x0, y0, x1, y1]) => x1 - x0 > 0.001 && y1 - y0 > 0.001);
}

/** 一组归一化矩形的外接框，用来把浮层摆在选区旁边。 */
export function unionRect(rects: NormalisedRect[]): NormalisedRect | null {
  if (rects.length === 0) return null;
  return [
    Math.min(...rects.map((item) => item[0])),
    Math.min(...rects.map((item) => item[1])),
    Math.max(...rects.map((item) => item[2])),
    Math.max(...rects.map((item) => item[3])),
  ];
}

/**
 * 把选中的字符串清洗成可以当引文提交的形状。
 *
 * pdf.js 的文本层在 PDF 原本换行的地方就换行，所以原始选区里全是硬换行。折成单行正是
 * 让这段文字能在后端对上解析文本的那一步——`verify_quote` 的 `normalised` 那一层做的
 * 是同样的折叠。不折的话，用户明明是照着屏幕划下来的，却会被判成"文档里没有这句话"。
 */
export function cleanQuote(raw: string, limit = 2000): string {
  const flat = (raw || "").replace(/\s+/g, " ").trim();
  return flat.length <= limit ? flat : flat.slice(0, limit);
}
