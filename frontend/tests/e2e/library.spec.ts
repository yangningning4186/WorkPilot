import { expect, test } from "@playwright/test";

import { LIBRARY } from "./fixtures/scenarios.mjs";

/**
 * 资料库页验收。
 *
 * 核心断言不是"表格渲染出来了"，而是**四种状态被如实区分**——尤其是
 * failed：它表示最新一版没进去、检索还在用旧版（约束 10 的沉默降级）。
 * 把它显示成普通的"失败"或干脆不显示，这个页面就白做了。
 */
test.describe("资料库页", () => {
  test("列表按状态区分，并说明失败版本不影响检索", async ({ page }) => {
    await page.goto("/library");

    await expect(page.getByRole("heading", { name: "资料库" })).toBeVisible();
    await expect(page.locator("table.library-table tbody tr")).toHaveCount(
      LIBRARY.documents.length,
    );

    const failedRow = page.locator("tbody tr", { hasText: "扫描件年报" });
    await expect(failedRow.locator(".state-badge")).toHaveText("新版失败");
    // 失败原因要能看到，否则用户只知道"红了"不知道为什么。
    await expect(failedRow.locator(".doc-error")).toHaveText("MinerU 子进程超时");
    // 关键语义：旧版本仍在服务，所以这一行的可检索 chunk 数不是 0。
    await expect(failedRow.locator("td.numeric").last()).toHaveText("30");
    await expect(failedRow.locator(".state-badge")).toHaveAttribute(
      "title",
      /检索仍在用上一个成功版本/,
    );

    await expect(
      page.locator("tbody tr", { hasText: "刚拖进来的论文" }).locator(".state-badge"),
    ).toHaveText("解析中");
    await expect(
      page.locator("tbody tr", { hasText: "混合检索与 RRF 融合" }).locator(".state-badge"),
    ).toHaveText("可检索");
    // stale：激活了但没有可检索 chunk，属于"看起来在库里其实搜不到"。
    await expect(
      page.locator("tbody tr", { hasText: "检索评测笔记" }).locator(".state-badge"),
    ).toHaveText("无可检索块");
  });

  test("概览统计与引用定位能力如实呈现", async ({ page }) => {
    await page.goto("/library");

    const stats = page.getByLabel("资料库概览");
    await expect(stats).toContainText("4");
    await expect(stats).toContainText("72");
    // 有失败时这块要变成警示态，不能和正常数字长一样。
    await expect(page.locator(".stat-tile.warn")).toContainText("新版解析失败");

    // PDF 有 bbox 才能高亮，markdown 只能给文本引用——列表要提前说清楚。
    await expect(
      page.locator("tbody tr", { hasText: "混合检索与 RRF 融合" }).locator(".locatable"),
    ).toHaveText("可高亮");
    await expect(
      page.locator("tbody tr", { hasText: "检索评测笔记" }).locator(".locatable"),
    ).toHaveText("仅文本");
  });

  test("搜索按标题过滤，导航能在两个页面之间来回", async ({ page }) => {
    await page.goto("/library");
    await page.getByLabel("搜索资料").fill("年报");

    await expect(page.locator("table.library-table tbody tr")).toHaveCount(1);
    await expect(page.locator("tbody tr")).toContainText("扫描件年报");

    await page.getByRole("navigation", { name: "主导航" }).getByRole("link", { name: "问答" }).click();
    await expect(page.getByRole("heading", { name: /从你的资料里/ })).toBeVisible();
    await page
      .getByRole("navigation", { name: "主导航" })
      .getByRole("link", { name: "资料库" })
      .click();
    await expect(page.getByRole("heading", { name: "资料库" })).toBeVisible();
  });

  test("同步入口存在，只读会话触发时给出可读提示", async ({ page }) => {
    await page.goto("/library");
    // 资料维护接口强制 admin，只读会话点了要有明确解释而不是静默失败。
    await page.route("**/api/v1/sources/*/sync", (route) =>
      route.fulfill({ status: 401, body: "需要 demo admin 登录" }),
    );

    await page.getByRole("button", { name: "导入 / 同步" }).click();

    await expect(page.locator(".inline-notice")).toContainText("需要 owner 登录");
  });
});
