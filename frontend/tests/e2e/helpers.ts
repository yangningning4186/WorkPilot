import { type APIRequestContext, type Locator, type Page, expect } from "@playwright/test";

export const MOCK_BASE = `http://127.0.0.1:${process.env.MOCK_BACKEND_PORT ?? 8787}`;

/** 提问并等 URL 上出现 run——run id 是后续所有断言的锚。 */
export async function ask(page: Page, query: string): Promise<string> {
  await page.goto("/");
  await page.getByLabel("向资料库提问").fill(query);
  await page.getByRole("button", { name: "提问" }).click();
  await expect(page).toHaveURL(/[?&]run=/);
  const runId = new URL(page.url()).searchParams.get("run");
  expect(runId).not.toBeNull();
  return runId as string;
}

export function answerCopy(page: Page): Locator {
  return page.locator(".answer-copy");
}

export function citationCards(page: Page): Locator {
  return page.locator("button.citation-card");
}

export function evidencePanel(page: Page): Locator {
  return page.getByLabel("引用原文预览");
}

/**
 * 高亮必须落在 bbox_norm × 实际渲染尺寸算出来的位置上。
 *
 * 这条断言才是"引用溯源"的验收点：面板打开、图片出来都不能证明高亮没错位，
 * 而错位恰恰是只存 bbox 四个数最典型的失败方式（约束 3）。
 *
 * 容差 2.5px：高亮有 1px 边框，且百分比定位在 devicePixelRatio 下会有亚像素取整。
 */
export async function expectHighlightMatchesBbox(
  canvas: Locator,
  highlight: Locator,
  bbox: readonly [number, number, number, number],
): Promise<void> {
  const canvasBox = await canvas.boundingBox();
  const highlightBox = await highlight.boundingBox();
  expect(canvasBox, "预览画布没有渲染").not.toBeNull();
  expect(highlightBox, "高亮层没有渲染").not.toBeNull();
  if (canvasBox === null || highlightBox === null) return;

  const [x0, y0, x1, y1] = bbox;
  const expected = {
    x: canvasBox.x + x0 * canvasBox.width,
    y: canvasBox.y + y0 * canvasBox.height,
    width: (x1 - x0) * canvasBox.width,
    height: (y1 - y0) * canvasBox.height,
  };
  const tolerance = 2.5;
  expect(Math.abs(highlightBox.x - expected.x), "高亮左边界错位").toBeLessThanOrEqual(tolerance);
  expect(Math.abs(highlightBox.y - expected.y), "高亮上边界错位").toBeLessThanOrEqual(tolerance);
  expect(Math.abs(highlightBox.width - expected.width), "高亮宽度错位").toBeLessThanOrEqual(
    tolerance,
  );
  expect(Math.abs(highlightBox.height - expected.height), "高亮高度错位").toBeLessThanOrEqual(
    tolerance,
  );
}

/** 假后端认的 demo 口令，与 mock-backend.mjs 的 ADMIN_PASSWORD 一致。 */
export const ADMIN_PASSWORD = "demo-admin-pw";

/**
 * 走顶栏登录入口拿到 admin session。
 *
 * 刻意不直接塞 cookie：cookie 是 httpOnly 的，用例要验的正是"浏览器里有路可走"，
 * 绕过 UI 注入等于把这个入口本身排除在验收之外。
 */
export async function loginAsAdmin(page: Page): Promise<void> {
  await page.getByRole("button", { name: "owner 登录" }).click();
  await page.getByLabel("owner 口令").fill(ADMIN_PASSWORD);
  await page.getByRole("button", { name: "登录", exact: true }).click();
  await expect(page.locator(".admin-badge")).toHaveText("owner");
}

/** 切换假后端的 DEMO_ADMIN_PASSWORD_HASH 配置状态。 */
export async function setAdminConfigured(
  request: APIRequestContext,
  configured: boolean,
): Promise<void> {
  const response = await request.post(`${MOCK_BASE}/__admin_configured?value=${String(configured)}`);
  expect(response.ok()).toBe(true);
}

/** 假后端记下的请求流水，用来断言"某个接口确实被调用过"。 */
export async function mockRequests(
  request: APIRequestContext,
): Promise<{ method: string; path: string; search: string }[]> {
  const response = await request.get(`${MOCK_BASE}/__requests`);
  expect(response.ok()).toBe(true);
  const body = (await response.json()) as {
    requests: { method: string; path: string; search: string }[];
  };
  return body.requests;
}
