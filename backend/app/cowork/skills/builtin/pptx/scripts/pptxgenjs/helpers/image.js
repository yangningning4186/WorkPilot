// Adapted from the local slides Skill: pptxgenjs_helpers/image.js.
// Copyright (c) OpenAI. All rights reserved.
"use strict";

const fs = require("node:fs");

function readInputAsBuffer(source) {
  if (!source) throw new Error("Image source is empty");
  if (Buffer.isBuffer(source)) return source;
  if (typeof source !== "string") throw new Error("Unsupported image source type");
  if (source.startsWith("data:")) {
    const comma = source.indexOf(",");
    return Buffer.from(comma === -1 ? source : source.slice(comma + 1), "base64");
  }
  return fs.readFileSync(source);
}

function isPng(buffer) {
  return (
    buffer.length >= 24 &&
    buffer[0] === 0x89 &&
    buffer[1] === 0x50 &&
    buffer[2] === 0x4e &&
    buffer[3] === 0x47
  );
}

function isJpeg(buffer) {
  return buffer.length > 3 && buffer[0] === 0xff && buffer[1] === 0xd8;
}

function isGif(buffer) {
  return buffer.length >= 10 && buffer.slice(0, 3).toString("ascii") === "GIF";
}

function readJpegSize(buffer) {
  let offset = 2;
  while (offset + 9 < buffer.length) {
    if (buffer[offset] !== 0xff) {
      offset += 1;
      continue;
    }
    const marker = buffer[offset + 1];
    if (
      (marker >= 0xc0 && marker <= 0xc3) ||
      (marker >= 0xc5 && marker <= 0xc7) ||
      (marker >= 0xc9 && marker <= 0xcb) ||
      (marker >= 0xcd && marker <= 0xcf)
    ) {
      return {
        width: buffer.readUInt16BE(offset + 7),
        height: buffer.readUInt16BE(offset + 5),
      };
    }
    const blockLength = buffer.readUInt16BE(offset + 2);
    if (blockLength < 2) break;
    offset += 2 + blockLength;
  }
  throw new Error("JPEG size not found");
}

function getImageDimensions(source) {
  const buffer = readInputAsBuffer(source);
  let width;
  let height;
  if (isPng(buffer)) {
    width = buffer.readUInt32BE(16);
    height = buffer.readUInt32BE(20);
  } else if (isJpeg(buffer)) {
    ({ width, height } = readJpegSize(buffer));
  } else if (isGif(buffer)) {
    width = buffer.readUInt16LE(6);
    height = buffer.readUInt16LE(8);
  } else {
    throw new Error("Unsupported image format; expected validated PNG, JPEG, or GIF");
  }
  if (!(width > 0 && height > 0)) throw new Error("Image dimensions are invalid");
  return { width, height, aspectRatio: width / height };
}

function imageSizingCrop(source, x, y, w, h) {
  const { aspectRatio } = getImageDimensions(source);
  const boxAspect = w / h;
  let cropX;
  let cropY;
  let cropW;
  let cropH;
  if (aspectRatio >= boxAspect) {
    cropH = 1;
    cropW = boxAspect / aspectRatio;
    cropX = (1 - cropW) / 2;
    cropY = 0;
  } else {
    cropW = 1;
    cropH = aspectRatio / boxAspect;
    cropX = 0;
    cropY = (1 - cropH) / 2;
  }
  let virtualW = w / cropW;
  let virtualH = virtualW / aspectRatio;
  if (Math.abs(virtualH * cropH - h) > 1e-6) {
    virtualH = h / cropH;
    virtualW = virtualH * aspectRatio;
  }
  return {
    x,
    y,
    w: virtualW,
    h: virtualH,
    sizing: {
      type: "crop",
      x: cropX * virtualW,
      y: cropY * virtualH,
      w,
      h,
    },
  };
}

function imageSizingContain(source, x, y, w, h) {
  const { aspectRatio } = getImageDimensions(source);
  const boxAspect = w / h;
  let fittedW;
  let fittedH;
  if (aspectRatio >= boxAspect) {
    fittedW = w;
    fittedH = w / aspectRatio;
  } else {
    fittedH = h;
    fittedW = h * aspectRatio;
  }
  return {
    x: x + (w - fittedW) / 2,
    y: y + (h - fittedH) / 2,
    w: fittedW,
    h: fittedH,
  };
}

module.exports = { getImageDimensions, imageSizingCrop, imageSizingContain };
