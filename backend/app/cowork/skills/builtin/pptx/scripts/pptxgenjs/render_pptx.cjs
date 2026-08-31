#!/usr/bin/env node
"use strict";

const fs = require("node:fs");
const os = require("node:os");
const path = require("node:path");
const PptxGenJS = require("pptxgenjs");

const {
  CANVAS,
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
  mixHex,
} = require("./components.cjs");

const RUNTIME_PROFILE = "pptxgenjs:1.0.0";
const PPTXGENJS_VERSION = "4.0.1";

function addBulletRows(slide, pptx, theme, values, box, options = {}) {
  if (!values.length) return;
  const gap = options.gap ?? 0.1;
  const height = Math.min(
    options.maxRowHeight || 0.76,
    (box.h - gap * Math.max(0, values.length - 1)) / values.length
  );
  const used = height * values.length + gap * Math.max(0, values.length - 1);
  const startY = box.y + Math.max(0, (box.h - used) / 2);
  values.forEach((value, index) => {
    const y = startY + index * (height + gap);
    slide.addShape(pptx.ShapeType.roundRect, {
      x: box.x,
      y: y + Math.max(0.1, (height - 0.34) / 2),
      w: 0.08,
      h: Math.min(0.34, height - 0.12),
      fill: { color: options.markerColor || theme.accent },
      line: { transparency: 100 },
    });
    addText(
      slide,
      value,
      { x: box.x + 0.2, y, w: box.w - 0.2, h: height },
      {
        theme,
        fontSize: String(value).length > 64 ? 16 : options.fontSize || 18,
        color: options.color || theme.text_primary,
        valign: "mid",
      }
    );
  });
}

function addCallout(slide, pptx, theme, text, box) {
  addSurface(slide, pptx, theme, box, {
    fill: mixHex(theme.accent, theme.background, 0.1),
    line: mixHex(theme.accent, theme.background, 0.22),
    shadow: false,
  });
  addText(slide, text, { x: box.x + 0.22, y: box.y + 0.06, w: box.w - 0.44, h: box.h - 0.12 }, {
    theme,
    fontSize: 16,
    align: "center",
    valign: "mid",
  });
}

function renderTitle(slide, pptx, spec, item) {
  const theme = spec.theme;
  const withImage = Boolean(item.image_path);
  if (withImage) {
    addSurface(slide, pptx, theme, { x: 9.52, y: 0.34, w: 3.56, h: 6.82 }, {
      fill: mixHex(theme.accent, theme.background, 0.1),
      line: mixHex(theme.accent, theme.background, 0.2),
      shadow: false,
    });
    addImageInBox(slide, item.image_path, { x: 9.62, y: 0.42, w: 3.36, h: 6.56 }, {
      fit: item.image_fit,
      alt: item.image_alt || item.title,
    });
  }
  slide.addShape(pptx.ShapeType.roundRect, {
    x: 0.9,
    y: 1.34,
    w: 0.12,
    h: 2.5,
    fill: { color: theme.accent },
    line: { transparency: 100 },
  });
  addText(slide, item.title, { x: 1.25, y: 1.22, w: withImage ? 7.9 : 10.65, h: 2.88 }, {
    theme,
    role: "title",
    // CJK glyphs are close to a full em wide.  The previous 44pt middle tier
    // left 18–20-character Chinese titles with a one-character orphan line.
    fontSize: adaptiveFontSize(item.title, 48, 39, 34, 28, 44),
    bold: true,
    valign: "mid",
  });
  const subtitle = item.subtitle || item.body || spec.purpose;
  if (subtitle) {
    addText(slide, subtitle, { x: 1.28, y: 4.12, w: withImage ? 7.55 : 9.75, h: 1.18 }, {
      theme,
      fontSize: 24,
      color: theme.text_secondary,
      valign: "top",
    });
  }
  if (item.image_caption) {
    addText(slide, item.image_caption, { x: 9.72, y: 6.9, w: 3.16, h: 0.34 }, {
      theme,
      fontSize: 16,
      color: theme.text_secondary,
      align: "center",
      margin: 0,
    });
  }
}

function renderStatement(slide, pptx, spec, item) {
  const theme = spec.theme;
  addSurface(slide, pptx, theme, { x: 0.78, y: 1.12, w: 11.78, h: 5.12 }, {
    fill: mixHex(theme.accent, theme.background, 0.08),
    line: mixHex(theme.accent, theme.background, 0.24),
    shadowOpacity: 0.06,
  });
  slide.addShape(pptx.ShapeType.roundRect, {
    x: 0.78,
    y: 1.12,
    w: 0.12,
    h: 5.12,
    fill: { color: theme.accent },
    line: { transparency: 100 },
  });
  const message = item.body || item.title;
  if (item.body) {
    addText(slide, item.title, { x: 1.35, y: 1.46, w: 10.65, h: 0.46 }, {
      theme,
      fontSize: 18,
      bold: true,
      color: theme.accent,
      align: "center",
      valign: "mid",
    });
  }
  addText(slide, message, { x: 1.35, y: item.body ? 1.95 : 1.45, w: 10.65, h: item.body ? 3.35 : 3.85 }, {
    theme,
    role: "title",
    fontSize: adaptiveFontSize(message, 36, 32, 28, 44, 76),
    bold: true,
    align: "center",
    valign: "mid",
  });
  if (item.subtitle) {
    addText(slide, item.subtitle, { x: 2.0, y: 5.38, w: 9.3, h: 0.65 }, {
      theme,
      fontSize: 22,
      color: theme.text_secondary,
      align: "center",
      valign: "mid",
    });
  }
}

function renderSection(slide, pptx, spec, item) {
  const theme = spec.theme;
  slide.addShape(pptx.ShapeType.rect, {
    x: 0,
    y: 0,
    w: 3.35,
    h: SLIDE_HEIGHT,
    fill: { color: mixHex(theme.accent, theme.background, 0.18) },
    line: { transparency: 100 },
  });
  [1.65, 1.15, 0.72].forEach((width, index) => {
    slide.addShape(pptx.ShapeType.roundRect, {
      x: 0.82,
      y: (item.body ? 3.0 : 2.35) + index * 0.42,
      w: width,
      h: 0.12,
      fill: { color: mixHex(theme.accent, theme.background, 0.5 + index * 0.15) },
      line: { transparency: 100 },
    });
  });
  const withImage = Boolean(item.image_path);
  const message = item.body || item.title;
  if (item.body) {
    addText(slide, item.title, { x: 0.7, y: 1.42, w: 2.05, h: 1.1 }, {
      theme,
      role: "title",
      fontSize: 21,
      bold: true,
      valign: "mid",
    });
  }
  addText(slide, message, { x: 3.95, y: 1.5, w: withImage ? 4.05 : 8.2, h: 3.38 }, {
    theme,
    role: "title",
    fontSize: adaptiveFontSize(message, 40, 35, 30, 42, 66),
    bold: true,
    valign: "mid",
  });
  if (item.subtitle) {
    addText(slide, item.subtitle, { x: 4.02, y: 5.13, w: withImage ? 3.94 : 7.65, h: 0.86 }, {
      theme,
      fontSize: 21,
      color: theme.text_secondary,
    });
  }
  if (withImage) {
    addSurface(slide, pptx, theme, { x: 8.34, y: 0.8, w: 4.46, h: 5.84 }, {
      fill: mixHex(theme.accent, theme.background, 0.06),
      shadow: false,
    });
    addImageInBox(slide, item.image_path, { x: 8.42, y: 0.88, w: 4.3, h: 5.68 }, {
      fit: item.image_fit,
      alt: item.image_alt || item.title,
    });
    if (item.image_caption) {
      addText(slide, item.image_caption, { x: 8.5, y: 6.64, w: 4.15, h: 0.34 }, {
        theme,
        fontSize: 16,
        color: theme.text_secondary,
        align: "center",
        margin: 0,
      });
    }
  }
}

function renderTwoColumn(slide, pptx, spec, item, comparison = false) {
  const theme = spec.theme;
  addTitle(slide, pptx, theme, item.title, spec.visual_family);
  const columns = [
    { x: MARGIN_X, heading: item.left_title, values: item.left_items, tone: theme.accent },
    {
      x: 6.86,
      heading: item.right_title,
      values: item.right_items,
      tone: comparison ? theme.warning : theme.accent,
    },
  ];
  columns.forEach((column) => {
    addSurface(slide, pptx, theme, { x: column.x, y: 1.52, w: 5.72, h: 5.02 }, {
      fill: mixHex(theme.surface, theme.background, 0.9),
      line: mixHex(column.tone, theme.background, 0.22),
    });
    addText(slide, column.heading, { x: column.x + 0.28, y: 1.76, w: 5.16, h: 0.48 }, {
      theme,
      role: "title",
      fontSize: 24,
      bold: true,
      color: column.tone,
    });
    addBulletRows(slide, pptx, theme, column.values, { x: column.x + 0.3, y: 2.46, w: 5.1, h: 3.7 }, {
      markerColor: column.tone,
      maxRowHeight: 0.7,
      fontSize: 17,
    });
  });
}

function renderBigNumber(slide, pptx, spec, item) {
  const theme = spec.theme;
  addTitle(slide, pptx, theme, item.title, spec.visual_family);
  const count = item.metrics.length;
  const gap = 0.18;
  const totalWidth = count === 1 ? 7.4 : 11.9;
  const width = (totalWidth - gap * (count - 1)) / count;
  const start = (SLIDE_WIDTH - totalWidth) / 2;
  const cardHeight = item.body ? 3.55 : 4.35;
  item.metrics.forEach((metric, index) => {
    const x = start + index * (width + gap);
    addSurface(slide, pptx, theme, { x, y: 1.65, w: width, h: cardHeight }, {
      fill: mixHex(theme.accent, theme.surface, 0.06 + 0.025 * (index % 2)),
      line: mixHex(theme.accent, theme.surface, 0.18),
    });
    addText(slide, metric.value, { x: x + 0.12, y: 1.92, w: width - 0.24, h: 1.58 }, {
      theme,
      role: "title",
      fontSize: adaptiveFontSize(metric.value, 46, 40, 34, 18, 26),
      bold: true,
      color: theme.accent,
      align: "center",
      valign: "mid",
    });
    addText(slide, metric.label, { x: x + 0.22, y: 3.55, w: width - 0.44, h: 0.62 }, {
      theme,
      fontSize: 18,
      align: "center",
      valign: "mid",
    });
    if (metric.detail) {
      addText(slide, metric.detail, { x: x + 0.22, y: 4.25, w: width - 0.44, h: 0.7 }, {
        theme,
        fontSize: 16,
        color: theme.text_secondary,
        align: "center",
      });
    }
  });
  if (item.body) addCallout(slide, pptx, theme, item.body, { x: 1.25, y: 5.52, w: 10.83, h: 0.85 });
}

function renderChart(slide, pptx, spec, item) {
  const theme = spec.theme;
  addTitle(slide, pptx, theme, item.title, spec.visual_family);
  const chart = item.chart;
  const data = chart.series.map((series) => ({
    name: series.name,
    labels: chart.categories,
    values: series.values,
  }));
  const isLine = chart.chart_type === "line";
  slide.addChart(isLine ? pptx.ChartType.line : pptx.ChartType.bar, data, {
    x: 0.95,
    y: 1.6,
    w: 11.45,
    h: item.body ? 4.35 : 4.95,
    altText: `${item.title}：${chart.series.map((series) => series.name).join("、")}`,
    barDir: chart.chart_type === "bar" ? "bar" : "col",
    barGrouping: "clustered",
    showLegend: chart.series.length > 1,
    legendPos: "b",
    showTitle: false,
    showValue: false,
    showCatName: false,
    chartColors: [theme.accent, theme.positive, theme.warning, theme.text_secondary],
    catAxisLabelColor: theme.text_secondary,
    valAxisLabelColor: theme.text_secondary,
    catAxisLabelFontFace: theme.body_font,
    valAxisLabelFontFace: theme.body_font,
    catAxisLabelFontSize: 16,
    valAxisLabelFontSize: 16,
    catAxisLineColor: mixHex(theme.text_secondary, theme.background, 0.42),
    valAxisLineColor: mixHex(theme.text_secondary, theme.background, 0.42),
    valGridLine: { color: mixHex(theme.text_secondary, theme.background, 0.18), width: 1 },
    showCatName: true,
    showValue: false,
    showSerName: false,
    showBorder: false,
    showLine: isLine,
    lineSize: 2.5,
    lineDataSymbol: "circle",
    lineDataSymbolSize: 6,
  });
  if (item.body) addCallout(slide, pptx, theme, item.body, { x: 1.2, y: 6.08, w: 10.93, h: 0.72 });
}

function renderImageText(slide, pptx, spec, item) {
  const theme = spec.theme;
  addTitle(slide, pptx, theme, item.title, spec.visual_family);
  addSurface(slide, pptx, theme, { x: 0.75, y: 1.55, w: 6.0, h: 4.9 }, {
    fill: mixHex(theme.accent, theme.background, 0.055),
    line: mixHex(theme.accent, theme.background, 0.16),
    shadow: false,
  });
  addSurface(slide, pptx, theme, { x: 6.92, y: 1.55, w: 5.66, h: 4.9 }, {
    fill: mixHex(theme.surface, theme.background, 0.92),
  });
  addImageInBox(slide, item.image_path, { x: 0.83, y: 1.63, w: 5.84, h: 4.74 }, {
    fit: item.image_fit,
    alt: item.image_alt || item.title,
  });
  const values = item.bullets.length ? item.bullets : item.body ? [item.body] : [item.image_caption];
  addBulletRows(slide, pptx, theme, values, { x: 7.25, y: 1.82, w: 5.05, h: 4.35 }, {
    fontSize: 19,
    maxRowHeight: 0.9,
    gap: 0.16,
  });
  if (item.image_caption) {
    addText(slide, item.image_caption, { x: 0.82, y: 6.48, w: 5.86, h: 0.36 }, {
      theme,
      fontSize: 16,
      color: theme.text_secondary,
      align: "center",
      margin: 0,
    });
  }
}

function renderQuote(slide, pptx, spec, item) {
  const theme = spec.theme;
  addTitle(slide, pptx, theme, item.title, spec.visual_family);
  addSurface(slide, pptx, theme, { x: 0.92, y: 1.48, w: 11.5, h: 4.92 }, {
    fill: mixHex(theme.accent, theme.background, 0.07),
    line: mixHex(theme.accent, theme.background, 0.18),
  });
  addText(slide, "“", { x: 1.28, y: 1.66, w: 0.65, h: 1.0 }, {
    theme,
    role: "title",
    fontSize: 66,
    bold: true,
    color: theme.accent,
  });
  addText(slide, item.body, { x: 2.08, y: 1.82, w: 9.15, h: 3.48 }, {
    theme,
    role: "title",
    fontSize: adaptiveFontSize(item.body, 32, 28, 25, 50, 82),
    bold: true,
    align: "center",
    valign: "mid",
  });
  if (item.quote_attribution) {
    addText(slide, `— ${item.quote_attribution}`, { x: 3.2, y: 5.55, w: 6.9, h: 0.58 }, {
      theme,
      fontSize: 18,
      color: theme.text_secondary,
      align: "center",
      valign: "mid",
    });
  }
}

function renderTimeline(slide, pptx, spec, item) {
  const theme = spec.theme;
  addTitle(slide, pptx, theme, item.title, spec.visual_family);
  const entries = item.timeline;
  const width = 11.55 / entries.length;
  slide.addShape(pptx.ShapeType.line, {
    x: 0.9,
    y: 3.02,
    w: 11.55,
    h: 0.001,
    line: { color: mixHex(theme.text_secondary, theme.background, 0.34), width: 2 },
  });
  entries.forEach((entry, index) => {
    const x = 0.9 + width * index;
    const center = x + width / 2;
    slide.addShape(pptx.ShapeType.ellipse, {
      x: center - 0.13,
      y: 2.89,
      w: 0.26,
      h: 0.26,
      fill: { color: theme.accent },
      line: { color: theme.background, width: 1.2 },
    });
    addText(slide, entry.label, { x, y: 1.88, w: width, h: 0.54 }, {
      theme,
      fontSize: 16,
      bold: true,
      color: theme.accent,
      align: "center",
      valign: "mid",
    });
    addSurface(slide, pptx, theme, { x: x + 0.08, y: 3.42, w: width - 0.16, h: 2.2 }, {
      fill: mixHex(theme.surface, theme.background, 0.9),
      shadowOpacity: 0.05,
    });
    addText(slide, entry.title, { x: x + 0.24, y: 3.68, w: width - 0.48, h: 0.58 }, {
      theme,
      role: "title",
      fontSize: 20,
      bold: true,
      align: "center",
      valign: "mid",
    });
    if (entry.detail) {
      addText(slide, entry.detail, { x: x + 0.24, y: 4.42, w: width - 0.48, h: 0.88 }, {
        theme,
        fontSize: 16,
        color: theme.text_secondary,
        align: "center",
        valign: "mid",
      });
    }
  });
}

function renderMatrix(slide, pptx, spec, item) {
  const theme = spec.theme;
  addTitle(slide, pptx, theme, item.title, spec.visual_family);
  const positions = [
    { x: 0.9, y: 1.62 },
    { x: 6.78, y: 1.62 },
    { x: 0.9, y: 4.22 },
    { x: 6.78, y: 4.22 },
  ];
  item.matrix.forEach((entry, index) => {
    const position = positions[index];
    addSurface(slide, pptx, theme, { ...position, w: 5.65, h: 2.22 }, {
      fill: mixHex(index % 2 ? theme.positive : theme.accent, theme.background, 0.08),
      line: mixHex(index % 2 ? theme.positive : theme.accent, theme.background, 0.22),
      shadowOpacity: 0.05,
    });
    addText(slide, `${entry.x} · ${entry.y}`, { x: position.x + 0.28, y: position.y + 0.24, w: 5.09, h: 0.42 }, {
      theme,
      fontSize: 16,
      bold: true,
      color: index % 2 ? theme.positive : theme.accent,
    });
    addText(slide, entry.label, { x: position.x + 0.28, y: position.y + 0.78, w: 5.09, h: 1.08 }, {
      theme,
      role: "title",
      fontSize: 22,
      bold: true,
      valign: "mid",
    });
  });
}

function renderCards(slide, pptx, spec, item) {
  const theme = spec.theme;
  addTitle(slide, pptx, theme, item.title, spec.visual_family);
  const cards = item.cards;
  const gap = 0.22;
  const totalWidth = 11.9;
  const width = (totalWidth - gap * (cards.length - 1)) / cards.length;
  cards.forEach((card, index) => {
    const x = MARGIN_X + index * (width + gap);
    addSurface(slide, pptx, theme, { x, y: 1.6, w: width, h: 4.95 }, {
      fill: mixHex(index % 2 ? theme.surface : theme.accent, theme.background, index % 2 ? 0.92 : 0.06),
      line: mixHex(theme.accent, theme.background, 0.18),
    });
    if (card.kicker) {
      addText(slide, card.kicker, { x: x + 0.28, y: 1.92, w: width - 0.56, h: 0.42 }, {
        theme,
        fontSize: 16,
        bold: true,
        color: theme.accent,
      });
    }
    addText(slide, card.title, { x: x + 0.28, y: card.kicker ? 2.48 : 2.05, w: width - 0.56, h: 0.9 }, {
      theme,
      role: "title",
      fontSize: adaptiveFontSize(card.title, 24, 21, 18, 24, 40),
      bold: true,
      valign: "mid",
    });
    addText(slide, card.detail, { x: x + 0.28, y: 3.56, w: width - 0.56, h: 2.22 }, {
      theme,
      fontSize: cards.length === 4 ? 16 : 17,
      color: theme.text_secondary,
      valign: "top",
    });
  });
}

function renderActivity(slide, pptx, spec, item) {
  const theme = spec.theme;
  addTitle(slide, pptx, theme, item.title, spec.visual_family);
  addSurface(slide, pptx, theme, { x: 0.78, y: 1.5, w: 9.5, h: 1.28 }, {
    fill: mixHex(theme.accent, theme.background, 0.1),
    line: mixHex(theme.accent, theme.background, 0.24),
  });
  addText(slide, item.activity_prompt, { x: 1.08, y: 1.72, w: 8.9, h: 0.82 }, {
    theme,
    role: "title",
    fontSize: adaptiveFontSize(item.activity_prompt, 26, 23, 20, 50, 82),
    bold: true,
    valign: "mid",
  });
  addText(slide, item.activity_timebox, { x: 10.55, y: 1.5, w: 2.0, h: 1.28 }, {
    theme,
    role: "title",
    fontSize: 24,
    bold: true,
    color: theme.background,
    fill: { color: theme.accent },
    line: { color: theme.accent, transparency: 100 },
    shape: pptx.ShapeType.roundRect,
    align: "center",
    valign: "mid",
  });
  addSurface(slide, pptx, theme, { x: 0.78, y: 3.04, w: 7.28, h: 3.3 }, {
    fill: mixHex(theme.surface, theme.background, 0.94),
  });
  addText(slide, "现场步骤", { x: 1.05, y: 3.28, w: 2.0, h: 0.38 }, {
    theme,
    fontSize: 18,
    bold: true,
    color: theme.accent,
  });
  addBulletRows(slide, pptx, theme, item.activity_steps, { x: 1.06, y: 3.82, w: 6.62, h: 2.14 }, {
    fontSize: 17,
    maxRowHeight: 0.5,
    gap: 0.08,
  });
  addSurface(slide, pptx, theme, { x: 8.32, y: 3.04, w: 4.24, h: 3.3 }, {
    fill: mixHex(theme.positive, theme.background, 0.09),
    line: mixHex(theme.positive, theme.background, 0.22),
  });
  addText(slide, "复盘问题", { x: 8.62, y: 3.3, w: 3.64, h: 0.4 }, {
    theme,
    fontSize: 18,
    bold: true,
    color: theme.positive,
  });
  addText(slide, item.activity_debrief, { x: 8.62, y: 3.98, w: 3.64, h: 1.78 }, {
    theme,
    role: "title",
    fontSize: adaptiveFontSize(item.activity_debrief, 21, 19, 17, 48, 78),
    bold: true,
    valign: "mid",
  });
}

function diagramNodeBox(node, box, index, count, orientation) {
  if (orientation === "vertical") {
    const gap = 0.18;
    const height = (box.h - gap * (count - 1)) / count;
    return { x: box.x + 3.1, y: box.y + index * (height + gap), w: box.w - 6.2, h: height };
  }
  const gap = 0.2;
  const width = (box.w - gap * (count - 1)) / count;
  return { x: box.x + index * (width + gap), y: box.y + 1.3, w: width, h: box.h - 2.6 };
}

function addDiagramNode(slide, pptx, theme, node, box) {
  const fill =
    node.emphasis === "primary"
      ? mixHex(theme.accent, theme.background, 0.18)
      : node.emphasis === "muted"
        ? mixHex(theme.text_secondary, theme.background, 0.08)
        : mixHex(theme.surface, theme.background, 0.94);
  const color = node.emphasis === "primary" ? theme.accent : theme.text_primary;
  const value = node.detail ? `${node.title}\n${node.detail}` : node.title;
  addText(slide, value, box, {
    theme,
    role: "title",
    fontSize: node.detail ? 17 : 20,
    bold: true,
    color,
    shape: pptx.ShapeType.roundRect,
    fill: { color: fill },
    line: { color: mixHex(color, theme.background, 0.34), width: 1 },
    align: "center",
    valign: "mid",
    margin: 0.12,
  });
}

function renderProcessDiagram(slide, pptx, spec, item) {
  const theme = spec.theme;
  const diagram = item.diagram;
  const area = { x: 0.9, y: 1.62, w: 11.55, h: 4.9 };
  const boxes = diagram.nodes.map((node, index) =>
    diagramNodeBox(node, area, index, diagram.nodes.length, diagram.orientation)
  );
  for (let index = 0; index < boxes.length - 1; index += 1) {
    const current = boxes[index];
    const next = boxes[index + 1];
    addConnector(
      slide,
      pptx,
      diagram.orientation === "vertical"
        ? { x: current.x + current.w / 2, y: current.y + current.h }
        : { x: current.x + current.w, y: current.y + current.h / 2 },
      diagram.orientation === "vertical"
        ? { x: next.x + next.w / 2, y: next.y }
        : { x: next.x, y: next.y + next.h / 2 },
      { color: theme.accent }
    );
  }
  diagram.nodes.forEach((node, index) => addDiagramNode(slide, pptx, theme, node, boxes[index]));
}

function renderCycleDiagram(slide, pptx, spec, item) {
  const theme = spec.theme;
  const nodes = item.diagram.nodes;
  const center = { x: 6.66, y: 4.05 };
  const radiusX = 3.8;
  const radiusY = 1.72;
  const nodeWidth = nodes.length > 4 ? 1.8 : 2.05;
  const nodeHeight = 1.08;
  const boxes = nodes.map((node, index) => {
    const angle = -Math.PI / 2 + (2 * Math.PI * index) / nodes.length;
    return {
      x: center.x + Math.cos(angle) * radiusX - nodeWidth / 2,
      y: center.y + Math.sin(angle) * radiusY - nodeHeight / 2,
      w: nodeWidth,
      h: nodeHeight,
    };
  });
  boxes.forEach((box, index) => {
    const next = boxes[(index + 1) % boxes.length];
    addConnector(
      slide,
      pptx,
      { x: box.x + box.w / 2, y: box.y + box.h / 2 },
      { x: next.x + next.w / 2, y: next.y + next.h / 2 },
      { color: mixHex(theme.accent, theme.text_secondary, 0.65) }
    );
  });
  nodes.forEach((node, index) => addDiagramNode(slide, pptx, theme, node, boxes[index]));
  if (item.diagram.center_label) {
    addText(slide, item.diagram.center_label, { x: 5.35, y: 3.42, w: 2.62, h: 1.22 }, {
      theme,
      role: "title",
      fontSize: 22,
      bold: true,
      color: theme.background,
      shape: pptx.ShapeType.ellipse,
      fill: { color: theme.accent },
      line: { color: theme.accent, transparency: 100 },
      align: "center",
      valign: "mid",
    });
  }
}

function hierarchyLevels(diagram) {
  const incoming = new Map(diagram.nodes.map((node) => [node.id, 0]));
  const children = new Map(diagram.nodes.map((node) => [node.id, []]));
  diagram.edges.forEach((edge) => {
    incoming.set(edge.target, (incoming.get(edge.target) || 0) + 1);
    children.get(edge.source).push(edge.target);
  });
  const root = diagram.nodes.find((node) => incoming.get(node.id) === 0);
  const depths = new Map([[root.id, 0]]);
  const queue = [root.id];
  while (queue.length) {
    const current = queue.shift();
    (children.get(current) || []).forEach((child) => {
      depths.set(child, depths.get(current) + 1);
      queue.push(child);
    });
  }
  const maxDepth = Math.max(...depths.values());
  return Array.from({ length: maxDepth + 1 }, (_, depth) =>
    diagram.nodes.filter((node) => depths.get(node.id) === depth)
  );
}

function renderHierarchyDiagram(slide, pptx, spec, item) {
  const theme = spec.theme;
  const levels = hierarchyLevels(item.diagram);
  const boxes = new Map();
  const top = 1.62;
  const availableHeight = 4.9;
  const rowHeight = Math.min(1.05, availableHeight / levels.length - 0.18);
  const rowGap = levels.length === 1 ? 0 : (availableHeight - rowHeight * levels.length) / (levels.length - 1);
  levels.forEach((nodes, depth) => {
    const gap = 0.24;
    const width = Math.min(3.05, (11.55 - gap * (nodes.length - 1)) / nodes.length);
    const total = width * nodes.length + gap * (nodes.length - 1);
    const start = 0.9 + (11.55 - total) / 2;
    nodes.forEach((node, index) => {
      boxes.set(node.id, { x: start + index * (width + gap), y: top + depth * (rowHeight + rowGap), w: width, h: rowHeight });
    });
  });
  item.diagram.edges.forEach((edge) => {
    const source = boxes.get(edge.source);
    const target = boxes.get(edge.target);
    addConnector(slide, pptx, { x: source.x + source.w / 2, y: source.y + source.h }, { x: target.x + target.w / 2, y: target.y }, {
      color: theme.accent,
      style: "elbow",
    });
  });
  item.diagram.nodes.forEach((node) => addDiagramNode(slide, pptx, theme, node, boxes.get(node.id)));
}

function renderLayeredDiagram(slide, pptx, spec, item) {
  const theme = spec.theme;
  const nodes = item.diagram.nodes;
  const isFunnel = item.diagram.kind === "funnel";
  const centerX = 6.66;
  const height = 4.65 / nodes.length;
  nodes.forEach((node, index) => {
    const level = isFunnel ? index : nodes.length - index - 1;
    const width = 4.2 + (nodes.length - level - 1) * (6.6 / Math.max(1, nodes.length - 1));
    const x = centerX - width / 2;
    const y = 1.74 + index * height;
    const fill =
      node.emphasis === "primary"
        ? theme.accent
        : mixHex(theme.accent, theme.background, 0.24 + 0.1 * index);
    addText(slide, node.detail ? `${node.title} · ${node.detail}` : node.title, { x, y, w: width, h: height - 0.08 }, {
      theme,
      role: "title",
      fontSize: 18,
      bold: true,
      color: node.emphasis === "primary" ? theme.background : theme.text_primary,
      shape: pptx.ShapeType.trapezoid,
      fill: { color: fill },
      line: { color: mixHex(theme.accent, theme.background, 0.4), width: 0.8 },
      align: "center",
      valign: "mid",
    });
  });
}

function renderDiagram(slide, pptx, spec, item) {
  addTitle(slide, pptx, spec.theme, item.title, spec.visual_family);
  if (item.diagram.kind === "process") renderProcessDiagram(slide, pptx, spec, item);
  else if (item.diagram.kind === "cycle") renderCycleDiagram(slide, pptx, spec, item);
  else if (item.diagram.kind === "hierarchy") renderHierarchyDiagram(slide, pptx, spec, item);
  else renderLayeredDiagram(slide, pptx, spec, item);
}

function canvasBox(element) {
  return {
    x: CANVAS.x + (CANVAS.w * element.x) / 100,
    y: CANVAS.y + (CANVAS.h * element.y) / 100,
    w: (CANVAS.w * element.width) / 100,
    h: (CANVAS.h * element.height) / 100,
  };
}

function renderCanvas(slide, pptx, spec, item) {
  const theme = spec.theme;
  addTitle(slide, pptx, theme, item.title, spec.visual_family);
  const positioned = item.canvas.elements.filter((element) => element.type !== "connector");
  const boxes = new Map(positioned.map((element) => [element.id, canvasBox(element)]));
  item.canvas.elements
    .filter((element) => element.type === "connector")
    .forEach((connector) => {
      const source = boxes.get(connector.source_id);
      const target = boxes.get(connector.target_id);
      addConnector(
        slide,
        pptx,
        { x: source.x + source.w / 2, y: source.y + source.h / 2 },
        { x: target.x + target.w / 2, y: target.y + target.h / 2 },
        {
          style: connector.style,
          color: theme[connector.color_role],
        }
      );
      if (connector.label) {
        addText(slide, connector.label, {
          x: (source.x + source.w / 2 + target.x + target.w / 2) / 2 - 0.55,
          y: (source.y + source.h / 2 + target.y + target.h / 2) / 2 - 0.18,
          w: 1.1,
          h: 0.45,
        }, {
          theme,
          fontSize: 16,
          color: theme[connector.color_role],
          fill: { color: theme.background },
          align: "center",
          valign: "mid",
          margin: 0.02,
        });
      }
    });
  positioned.forEach((element) => {
    const box = boxes.get(element.id);
    if (element.type === "text") {
      addText(slide, element.text, box, {
        theme,
        fontSize: element.font_size,
        bold: element.bold,
        color: theme[element.color_role],
        align: element.align,
        valign: element.valign === "middle" ? "mid" : element.valign,
      });
    } else if (element.type === "shape") {
      const shape = {
        rectangle: pptx.ShapeType.rect,
        rounded_rectangle: pptx.ShapeType.roundRect,
        oval: pptx.ShapeType.ellipse,
        chevron: pptx.ShapeType.chevron,
        hexagon: pptx.ShapeType.hexagon,
      }[element.shape];
      const fill =
        element.fill_style === "solid"
          ? theme[element.fill_role]
          : mixHex(theme[element.fill_role], theme.background, 0.16);
      const color = element.fill_style === "solid" ? theme.background : theme.text_primary;
      addText(slide, element.detail ? `${element.title}\n${element.detail}` : element.title, box, {
        theme,
        role: "title",
        fontSize: element.font_size,
        bold: true,
        color,
        shape,
        fill: { color: fill },
        line: { color: mixHex(theme[element.fill_role], theme.background, 0.42), width: 1 },
        align: "center",
        valign: "mid",
        margin: 0.1,
      });
    } else if (element.type === "image") {
      addImageInBox(slide, element.image_path, box, {
        fit: element.image_fit,
        alt: element.image_alt,
      });
    }
  });
}

const RENDERERS = {
  title: renderTitle,
  statement: renderStatement,
  section: renderSection,
  two_column: (slide, pptx, spec, item) => renderTwoColumn(slide, pptx, spec, item, false),
  comparison: (slide, pptx, spec, item) => renderTwoColumn(slide, pptx, spec, item, true),
  big_number: renderBigNumber,
  chart: renderChart,
  image_text: renderImageText,
  quote: renderQuote,
  timeline: renderTimeline,
  matrix: renderMatrix,
  cards: renderCards,
  activity: renderActivity,
  diagram: renderDiagram,
  canvas: renderCanvas,
};

async function renderPresentation(payload, target) {
  if (!payload || payload.schema_version !== 1 || payload.renderer !== "pptxgenjs") {
    throw new Error("Invalid WorkPilot PptxGenJS renderer payload");
  }
  const spec = { ...payload.spec, visual_family: payload.visual_family };
  if (!spec || !Array.isArray(spec.slides) || spec.slides.length === 0) {
    throw new Error("PresentationSpec must contain at least one slide");
  }
  if (path.extname(target).toLowerCase() !== ".pptx") {
    throw new Error("PptxGenJS renderer target must end in .pptx");
  }
  const pptx = new PptxGenJS();
  pptx.defineLayout({ name: "WORKPILOT_WIDE", width: SLIDE_WIDTH, height: SLIDE_HEIGHT });
  pptx.layout = "WORKPILOT_WIDE";
  pptx.author = "WorkPilot";
  pptx.company = "WorkPilot";
  pptx.subject = spec.purpose || "";
  pptx.title = spec.title;
  pptx.lang = "zh-CN";
  pptx.theme = {
    headFontFace: spec.theme.title_font,
    bodyFontFace: spec.theme.body_font,
    lang: "zh-CN",
  };
  const auditWarnings = [];
  spec.slides.forEach((item, index) => {
    const renderer = RENDERERS[item.layout];
    if (!renderer) throw new Error(`Unsupported presentation layout: ${item.layout}`);
    const slide = pptx.addSlide();
    decorateBackground(slide, pptx, spec.theme, spec.visual_family);
    renderer(slide, pptx, spec, item);
    addFooter(slide, spec.theme, index + 1);
    if (item.notes) slide.addNotes(item.notes);
    auditWarnings.push(...auditSlide(slide, pptx).map((message) => `slide ${index + 1}: ${message}`));
  });
  await pptx.writeFile({ fileName: target, compression: true });
  return { slide_count: spec.slides.length, warnings: auditWarnings };
}

function runtimeInfo() {
  return {
    profile: RUNTIME_PROFILE,
    node: process.versions.node,
    dependencies: { pptxgenjs: PPTXGENJS_VERSION },
  };
}

async function selftest() {
  const directory = fs.mkdtempSync(path.join(os.tmpdir(), "workpilot-pptxgenjs-"));
  const target = path.join(directory, "probe.pptx");
  try {
    await renderPresentation(
      {
        schema_version: 1,
        renderer: "pptxgenjs",
        visual_family: "clean",
        spec: {
          title: "WorkPilot PptxGenJS self-test",
          purpose: "runtime probe",
          theme: {
            background: "F7F8F6",
            surface: "FFFFFF",
            text_primary: "17211D",
            text_secondary: "5F6D66",
            accent: "167A5B",
            positive: "26845F",
            warning: "C37632",
            title_font: "Arial",
            body_font: "Arial",
            east_asia_font: "Microsoft YaHei",
          },
          slides: [
            {
              id: "probe",
              layout: "title",
              title: "PptxGenJS renderer ready",
              subtitle: "Editable OOXML output",
              body: null,
              image_path: null,
              image_caption: null,
              image_alt: null,
              image_fit: "contain",
              notes: "Runtime self-test",
            },
          ],
        },
      },
      target
    );
    const content = fs.readFileSync(target);
    if (content.length < 1000 || content[0] !== 0x50 || content[1] !== 0x4b) {
      throw new Error("Self-test output is not a valid ZIP-based PPTX");
    }
    return { ok: true, ...runtimeInfo() };
  } finally {
    fs.rmSync(directory, { recursive: true, force: true });
  }
}

async function main(argv = process.argv.slice(2)) {
  if (argv.length === 1 && argv[0] === "--workpilot-runtime-info") {
    process.stdout.write(`${JSON.stringify(runtimeInfo())}\n`);
    return;
  }
  if (argv.length === 1 && argv[0] === "--workpilot-selftest") {
    process.stdout.write(`${JSON.stringify(await selftest())}\n`);
    return;
  }
  if (argv.length !== 2) {
    throw new Error("Usage: workpilot-pptx-renderer <spec.json> <target.pptx>");
  }
  const [specPath, target] = argv;
  const stat = fs.statSync(specPath);
  if (!stat.isFile() || stat.size <= 0 || stat.size > 8 * 1024 * 1024) {
    throw new Error("Renderer input JSON must be a regular file no larger than 8 MiB");
  }
  const payload = JSON.parse(fs.readFileSync(specPath, "utf8"));
  const result = await renderPresentation(payload, path.resolve(target));
  process.stdout.write(`${JSON.stringify({ ok: true, ...runtimeInfo(), ...result })}\n`);
}

if (require.main === module) {
  main().catch((error) => {
    process.stderr.write(`WorkPilot PptxGenJS renderer failed: ${error.stack || error.message}\n`);
    process.exitCode = 1;
  });
}

module.exports = { renderPresentation, runtimeInfo };
