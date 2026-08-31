"use strict";

const { imageSizingContain, imageSizingCrop } = require("./helpers/image");
const {
  warnIfSlideElementsOutOfBounds,
  warnIfSlideHasOverlaps,
} = require("./helpers/layout");
const { safeOuterShadow } = require("./helpers/util");

const SLIDE_WIDTH = 13.333;
const SLIDE_HEIGHT = 7.5;
const MARGIN_X = 0.72;
const CONTENT_TOP = 1.42;
const CONTENT_BOTTOM = 7.02;
const CANVAS = {
  x: MARGIN_X,
  y: CONTENT_TOP,
  w: SLIDE_WIDTH - MARGIN_X * 2,
  h: CONTENT_BOTTOM - CONTENT_TOP,
};

function mixHex(foreground, background, weight) {
  const bounded = Math.max(0, Math.min(1, weight));
  return [0, 2, 4]
    .map((offset) => {
      const front = Number.parseInt(foreground.slice(offset, offset + 2), 16);
      const back = Number.parseInt(background.slice(offset, offset + 2), 16);
      return Math.round(front * bounded + back * (1 - bounded))
        .toString(16)
        .padStart(2, "0");
    })
    .join("")
    .toUpperCase();
}

function displayUnits(value) {
  return [...String(value || "")].reduce(
    (total, character) => total + (/[^\u0000-\u00ff]/u.test(character) ? 2 : 1),
    0
  );
}

function adaptiveFontSize(text, normal, medium, compact, mediumAfter, compactAfter) {
  const units = displayUnits(text);
  if (units > compactAfter) return compact;
  if (units > mediumAfter) return medium;
  return normal;
}

function fontFor(text, theme, role = "body") {
  if (/[^\u0000-\u00ff]/u.test(String(text || ""))) return theme.east_asia_font;
  return role === "title" ? theme.title_font : theme.body_font;
}

function addText(slide, text, box, options = {}) {
  const theme = options.theme;
  const role = options.role || "body";
  const textOptions = {
    x: box.x,
    y: box.y,
    w: box.w,
    h: box.h,
    fontFace: options.fontFace || fontFor(text, theme, role),
    fontSize: options.fontSize || 18,
    color: options.color || theme.text_primary,
    bold: Boolean(options.bold),
    align: options.align || "left",
    valign: options.valign || "top",
    margin: options.margin ?? 0.04,
    breakLine: false,
    ...(options.shape ? {} : { isTextBox: true }),
    ...(options.shape ? { shape: options.shape } : {}),
    ...(options.fill ? { fill: options.fill } : {}),
    ...(options.line ? { line: options.line } : {}),
    ...(options.shadow ? { shadow: options.shadow } : {}),
  };
  slide.addText(String(text), textOptions);
}

function addSurface(slide, pptx, theme, box, options = {}) {
  const shape = options.shape || pptx.ShapeType.roundRect;
  slide.addShape(shape, {
    x: box.x,
    y: box.y,
    w: box.w,
    h: box.h,
    rectRadius: options.rectRadius || 0.06,
    fill: { color: options.fill || theme.surface },
    line: {
      color: options.line || mixHex(theme.text_secondary, theme.surface, 0.22),
      width: options.lineWidth ?? 0.7,
      transparency: options.lineTransparency ?? 0,
    },
    ...(options.shadow === false
      ? {}
      : { shadow: safeOuterShadow("000000", options.shadowOpacity ?? 0.08, 45, 1.5, 0.8) }),
  });
}

function addConnector(slide, pptx, start, end, options = {}) {
  const x = Math.min(start.x, end.x);
  const y = Math.min(start.y, end.y);
  const shape =
    options.style === "elbow" && pptx.ShapeType.bentConnector3
      ? pptx.ShapeType.bentConnector3
      : pptx.ShapeType.line;
  slide.addShape(shape, {
    x,
    y,
    w: Math.max(0.001, Math.abs(end.x - start.x)),
    h: Math.max(0.001, Math.abs(end.y - start.y)),
    flipH: end.x < start.x,
    flipV: end.y < start.y,
    line: {
      color: options.color || "7A8B83",
      width: options.width || 1.5,
      endArrowType: options.arrow === false ? "none" : "triangle",
    },
  });
}

function addImageInBox(slide, path, box, options = {}) {
  const placement =
    options.fit === "cover"
      ? imageSizingCrop(path, box.x, box.y, box.w, box.h)
      : imageSizingContain(path, box.x, box.y, box.w, box.h);
  slide.addImage({
    path,
    ...placement,
    altText: options.alt || "",
  });
}

function addTitle(slide, pptx, theme, title, family) {
  addText(
    slide,
    title,
    { x: MARGIN_X, y: 0.42, w: 11.9, h: 0.76 },
    {
      theme,
      role: "title",
      fontSize: adaptiveFontSize(title, 35, 32, 29, 58, 82),
      bold: true,
      valign: "mid",
    }
  );
  const width = family === "editorial" || family === "consulting" ? 1.45 : 0.72;
  slide.addShape(pptx.ShapeType.rect, {
    x: MARGIN_X,
    y: 1.19,
    w: width,
    h: family === "editorial" ? 0.045 : 0.065,
    fill: { color: theme.accent },
    line: { color: theme.accent, transparency: 100 },
  });
}

function decorateBackground(slide, pptx, theme, family) {
  slide.background = { color: theme.background };
  if (family === "editorial") {
    slide.addShape(pptx.ShapeType.rect, {
      x: 12.63,
      y: 0.35,
      w: 0.035,
      h: 6.78,
      fill: { color: mixHex(theme.accent, theme.background, 0.62) },
      line: { transparency: 100 },
    });
  } else if (family === "luminous") {
    for (const [x, y, size, weight] of [
      [11.74, 0.34, 0.62, 0.54],
      [12.42, 0.92, 0.28, 0.76],
    ]) {
      slide.addShape(pptx.ShapeType.ellipse, {
        x,
        y,
        w: size,
        h: size,
        fill: { color: mixHex(theme.accent, theme.background, weight) },
        line: { transparency: 100 },
      });
    }
  } else if (family === "organic") {
    slide.addShape(pptx.ShapeType.ellipse, {
      x: 11.92,
      y: 6.61,
      w: 0.82,
      h: 0.34,
      fill: { color: mixHex(theme.accent, theme.background, 0.3) },
      line: { transparency: 100 },
    });
  } else if (family === "consulting") {
    slide.addShape(pptx.ShapeType.rect, {
      x: 12.15,
      y: 0.42,
      w: 0.48,
      h: 0.08,
      fill: { color: theme.accent },
      line: { transparency: 100 },
    });
  } else if (family === "bold" || family === "tech") {
    slide.addShape(pptx.ShapeType.rect, {
      x: 11.72,
      y: 0.32,
      w: 0.94,
      h: 0.16,
      fill: { color: theme.accent },
      line: { transparency: 100 },
    });
  }
}

function addFooter(slide, theme, pageNumber) {
  addText(
    slide,
    String(pageNumber).padStart(2, "0"),
    { x: 0.72, y: 6.96, w: 0.49, h: 0.32 },
    {
      theme,
      fontSize: 16,
      color: theme.text_secondary,
      align: "left",
      valign: "mid",
      margin: 0,
    }
  );
}

function auditSlide(slide, pptx) {
  const messages = [];
  const originalWarn = console.warn;
  const originalError = console.error;
  try {
    console.warn = (...parts) => messages.push(parts.join(" "));
    console.error = (...parts) => messages.push(parts.join(" "));
    warnIfSlideHasOverlaps(slide, pptx, { ignoreLines: true });
    warnIfSlideElementsOutOfBounds(slide, pptx);
  } finally {
    console.warn = originalWarn;
    console.error = originalError;
  }
  const blocking = messages.filter(
    (message) => message.includes("Severe text overlap") || message.includes("exceeds slide bounds")
  );
  if (blocking.length) throw new Error(blocking.join("; "));
  return messages;
}

module.exports = {
  CANVAS,
  CONTENT_BOTTOM,
  CONTENT_TOP,
  MARGIN_X,
  SLIDE_HEIGHT,
  SLIDE_WIDTH,
  adaptiveFontSize,
  addConnector,
  addFooter,
  addImageInBox,
  addSurface,
  addText,
  addTitle,
  auditSlide,
  decorateBackground,
  displayUnits,
  fontFor,
  mixHex,
};
