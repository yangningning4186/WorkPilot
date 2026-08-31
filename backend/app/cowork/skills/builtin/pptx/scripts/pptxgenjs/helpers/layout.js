// Focused adaptation of the local slides Skill: pptxgenjs_helpers/layout.js.
// Copyright (c) OpenAI. All rights reserved.
"use strict";

function inferElementType(object) {
  if (!object) return "unknown";
  const data = object.data || object.options || {};
  const shapeName = String(
    object.shape || data.shape || data.shapeType || ""
  ).toLowerCase();
  if (
    object.type === "line" ||
    shapeName.includes("line") ||
    shapeName.includes("connector")
  ) {
    return "line";
  }
  if (typeof object.type === "string") return object.type;
  if (typeof object._type === "string") return object._type;
  if (object.text || typeof data.text === "string") return "text";
  if (data.path || object.image) return "image";
  if (data.chartType) return "chart";
  if (data.shape || data.line) return "shape";
  return "unknown";
}

function boundsOf(object) {
  const source = object?.data || object?.options || {};
  const x = Number.isFinite(source.x) ? source.x : 0;
  const y = Number.isFinite(source.y) ? source.y : 0;
  let w = Number.isFinite(source.w) ? source.w : 0;
  let h = Number.isFinite(source.h) ? source.h : 0;
  if (source.sizing?.type === "crop") {
    if (Number.isFinite(source.sizing.w)) w = source.sizing.w;
    if (Number.isFinite(source.sizing.h)) h = source.sizing.h;
  }
  return { x, y, w, h, x2: x + w, y2: y + h };
}

function compareElementPosition(slide, firstIndex, secondIndex) {
  const elements = slide?._slideObjects;
  if (!Array.isArray(elements)) throw new Error("Invalid PptxGenJS slide object");
  const a = boundsOf(elements[firstIndex]);
  const b = boundsOf(elements[secondIndex]);
  const epsilon = 1e-4;
  if (
    a.x2 <= b.x + epsilon ||
    b.x2 <= a.x + epsilon ||
    a.y2 <= b.y + epsilon ||
    b.y2 <= a.y + epsilon
  ) {
    return { relation: "disjoint", aBounds: a, bBounds: b, intersection: null };
  }
  const aContainsB =
    a.x <= b.x + epsilon &&
    a.y <= b.y + epsilon &&
    a.x2 >= b.x2 - epsilon &&
    a.y2 >= b.y2 - epsilon;
  const bContainsA =
    b.x <= a.x + epsilon &&
    b.y <= a.y + epsilon &&
    b.x2 >= a.x2 - epsilon &&
    b.y2 >= a.y2 - epsilon;
  const intersection = {
    x: Math.max(a.x, b.x),
    y: Math.max(a.y, b.y),
    w: Math.max(0, Math.min(a.x2, b.x2) - Math.max(a.x, b.x)),
    h: Math.max(0, Math.min(a.y2, b.y2) - Math.max(a.y, b.y)),
  };
  if (aContainsB || bContainsA) {
    return { relation: "contained", aBounds: a, bBounds: b, intersection };
  }
  return { relation: "overlapping", aBounds: a, bBounds: b, intersection };
}

function warnIfSlideHasOverlaps(slide, pptx, options = {}) {
  const elements = slide?._slideObjects;
  if (!Array.isArray(elements)) throw new Error("Invalid PptxGenJS slide object");
  const ignoreLines = options.ignoreLines !== false;
  for (let first = 0; first < elements.length; first += 1) {
    const firstType = inferElementType(elements[first]);
    if (ignoreLines && firstType === "line") continue;
    for (let second = first + 1; second < elements.length; second += 1) {
      const secondType = inferElementType(elements[second]);
      if (ignoreLines && secondType === "line") continue;
      const comparison = compareElementPosition(slide, first, second);
      if (comparison.relation !== "overlapping") continue;
      const severeTextOverlap =
        (firstType === "text" || secondType === "text") &&
        comparison.intersection.w >= 0.1 &&
        comparison.intersection.h >= 0.1;
      const slideIndex = Array.isArray(pptx?._slides) ? pptx._slides.indexOf(slide) + 1 : 0;
      const message = `Slide ${slideIndex || "?"}: ${firstType} ${first} overlaps ${secondType} ${second}`;
      if (severeTextOverlap) console.error(`Severe text overlap: ${message}`);
      else console.warn(message);
    }
  }
}

function getSlideDimensions(slide, pptx) {
  const width = Number(pptx?.presLayout?.width || slide?._presLayout?.width || 13.333);
  const height = Number(pptx?.presLayout?.height || slide?._presLayout?.height || 7.5);
  return { width, height };
}

function warnIfSlideElementsOutOfBounds(slide, pptx) {
  const elements = slide?._slideObjects;
  if (!Array.isArray(elements)) throw new Error("Invalid PptxGenJS slide object");
  const { width, height } = getSlideDimensions(slide, pptx);
  const slideIndex = Array.isArray(pptx?._slides) ? pptx._slides.indexOf(slide) + 1 : 0;
  elements.forEach((object, index) => {
    const bounds = boundsOf(object);
    if (
      bounds.x < -1e-4 ||
      bounds.y < -1e-4 ||
      bounds.x2 > width + 1e-4 ||
      bounds.y2 > height + 1e-4
    ) {
      console.warn(
        `Slide ${slideIndex || "?"}: Element ${index} exceeds slide bounds ` +
          `(${bounds.x.toFixed(3)}, ${bounds.y.toFixed(3)}, ${bounds.x2.toFixed(3)}, ${bounds.y2.toFixed(3)})`
      );
    }
  });
}

module.exports = {
  boundsOf,
  compareElementPosition,
  getSlideDimensions,
  inferElementType,
  warnIfSlideElementsOutOfBounds,
  warnIfSlideHasOverlaps,
};
