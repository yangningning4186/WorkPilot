import { expect, test } from "@playwright/test";

import { IDS } from "./fixtures/scenarios.mjs";
import { ask, citationCards } from "./helpers";

test("两个浏览器 session 不能复用 conversation、读取 run/SSE 或打开引用原文", async ({
  browser,
  page,
}) => {
  const runId = await ask(page, "混合检索为什么比单路召回更稳定？");
  await expect(citationCards(page)).toHaveCount(2);

  const ownerUrl = new URL(page.url());
  const conversationId = ownerUrl.searchParams.get("conversation");
  expect(conversationId).not.toBeNull();
  const ownerCookies = await page.context().cookies();
  const sessionCookie = ownerCookies.find((cookie) => cookie.name === "workpilot_session");
  expect(sessionCookie).toBeDefined();
  expect(sessionCookie?.httpOnly).toBe(true);
  expect(sessionCookie?.sameSite).toBe("Lax");

  const appOrigin = ownerUrl.origin;
  const intruder = await browser.newContext({ baseURL: appOrigin });
  try {
    const intruderPage = await intruder.newPage();
    await ask(intruderPage, "markdown 笔记里英文集的结论是什么？");

    const reuse = await intruder.request.post("/api/v1/runs", {
      data: { query: "越权复用", conversation_id: conversationId },
    });
    expect(reuse.status()).toBe(404);
    expect((await intruder.request.get(`/api/v1/runs/${runId}/events`)).status()).toBe(404);
    expect((await intruder.request.post(`/api/v1/runs/${runId}/cancel`)).status()).toBe(404);
    expect(
      (await intruder.request.get(`/api/v1/documents/${IDS.pdfVersion}/file`)).status(),
    ).toBe(404);

    expect(
      (
        await page.context().request.get(
          `${appOrigin}/api/v1/documents/${IDS.pdfVersion}/file`,
        )
      ).status(),
    ).toBe(200);
  } finally {
    await intruder.close();
  }
});
