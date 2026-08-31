// Adapted from the local slides Skill: pptxgenjs_helpers/util.js.
// Copyright (c) OpenAI. All rights reserved.
"use strict";

function safeOuterShadow(
  color = "000000",
  opacity = 0.18,
  angle = 45,
  blur = 2,
  offset = 1
) {
  return { type: "outer", color, opacity, angle, blur, offset };
}

module.exports = { safeOuterShadow };
