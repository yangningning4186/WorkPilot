import { type APIRequestContext, type Locator, type Page, expect } from "@playwright/test";

export const MOCK_BASE = `http://127.0.0.1:${process.env.MOCK_BACKEND_PORT ?? 8787}`;

/** 从当前 Cowork 入口发起一轮任务，并从创建响应里取得 run id。 */
export async function ask(
  page: Page,
  query: string,
  options: { readingPath?: string } = {},
): Promise<string> {
  await page.goto("/cowork?new=1");
  await loginAsAdmin(page);
  await selectConfiguredProvider(page);
  if (options.readingPath !== undefined) {
    await page.getByRole("tab", { name: "论文阅读" }).click();
    await page
      .getByPlaceholder("要读的文档，例如 papers/attention.pdf")
      .fill(options.readingPath);
  }
  const created = page.waitForResponse((response) => {
    const request = response.request();
    return request.method() === "POST" && new URL(response.url()).pathname === "/api/v1/runs/cowork";
  });
  await page.getByLabel("你想让 Cowork 完成什么？").fill(query);
  await page.getByRole("button", { name: "开始执行任务" }).click();
  const response = await created;
  expect(response.ok()).toBe(true);
  const body = (await response.json()) as { run_id: string };
  await expect(page).toHaveURL(new RegExp("[?&]conversation="));
  return body.run_id;
}

/** 兼容显式选择测试；产品现在会默认绑定假后端排在首位的用户 Provider。 */
export async function selectConfiguredProvider(page: Page): Promise<void> {
  const select = page.getByLabel("模型服务");
  if (!(await select.isVisible())) {
    await page.locator(".workdesk-run-settings > summary").click();
  }
  await expect(select).toBeVisible();
  if ((await select.inputValue()) === "") await select.selectOption({ index: 1 });
}

export function answerCopy(page: Page): Locator {
  return page.locator(".answer-copy").last();
}

export function citationCards(page: Page): Locator {
  return answerCopy(page).locator(".citation-chip");
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
  const badge = page.locator(".admin-badge");
  if (await badge.isVisible()) return;
  await page.getByRole("button", { name: "owner 登录" }).click();
  await page.getByLabel("owner 口令").fill(ADMIN_PASSWORD);
  await page.getByRole("button", { name: "登录", exact: true }).click();
  await expect(badge).toHaveText("owner");
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

/** 假后端在创建 run 时冻结的关键字段，用来验会话级配置的时序隔离。 */
export async function mockRuns(
  request: APIRequestContext,
): Promise<{
  id: string;
  conversation_id: string;
  status: string;
  kb_slug: string | null;
  workspace_path: string | null;
  workspace_files: string[] | null;
}[]> {
  const response = await request.get(`${MOCK_BASE}/__runs`);
  expect(response.ok()).toBe(true);
  const body = (await response.json()) as {
    runs: {
      id: string;
      conversation_id: string;
      status: string;
      kb_slug: string | null;
      workspace_path: string | null;
      workspace_files: string[] | null;
    }[];
  };
  return body.runs;
}
