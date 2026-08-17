import { expect, test } from "@playwright/test";

import { loginAsAdmin, mockRequests } from "./helpers";

test.describe("owner 长期记忆", () => {
  test("匿名 demo 只看到登录门，不会请求个人记忆", async ({ page, request }) => {
    await page.goto("/memory");

    await expect(page.getByRole("heading", { name: "登录后才会打开个人记忆" })).toBeVisible();
    await expect(page.locator(".memory-card")).toHaveCount(0);
    const log = await mockRequests(request);
    expect(log.filter((item) => item.path === "/api/v1/memories")).toHaveLength(0);
  });

  test("窄屏仍能看到记忆导航和 owner 登录，页面不横向溢出", async ({ page }) => {
    await page.setViewportSize({ width: 375, height: 812 });
    await page.goto("/memory");

    await expect(page.getByRole("link", { name: "记忆" })).toBeVisible();
    await expect(page.getByRole("button", { name: "owner 登录" })).toBeVisible();
    const sizes = await page.evaluate(() => ({
      viewport: window.innerWidth,
      document: document.documentElement.scrollWidth,
    }));
    expect(sizes.document).toBeLessThanOrEqual(sizes.viewport);
  });

  test("登录后可新增、置顶、版本化编辑并查看历史", async ({ page }) => {
    await page.goto("/memory");
    await loginAsAdmin(page);

    await expect(page.locator(".memory-card")).toHaveCount(3);
    await expect(page.locator(".memory-card.pinned")).toHaveCount(1);

    await page.getByRole("button", { name: "手动添加" }).click();
    await page.getByLabel("记住这件事").fill("睡前长任务结束后先给摘要");
    await page.getByLabel("始终优先召回").check();
    await page.getByRole("button", { name: "加入记忆" }).click();
    await expect(page.getByText("睡前长任务结束后先给摘要", { exact: true })).toBeVisible();
    await expect(page.locator(".memory-card")).toHaveCount(4);

    const card = page.locator(".memory-card").filter({ hasText: "正在开发 WorkPilot" });
    await card.getByRole("button", { name: "编辑" }).click();
    const editor = page.locator(".memory-card.editing");
    await editor.getByLabel("记住这件事").fill("正在开发带长期记忆的 WorkPilot");
    await editor.getByRole("button", { name: "保存新版本" }).click();
    await expect(page.getByText("正在开发带长期记忆的 WorkPilot", { exact: true })).toBeVisible();

    await page.getByRole("tab", { name: "历史版本" }).click();
    await expect(page.getByText("正在开发 WorkPilot", { exact: true })).toBeVisible();
    await expect(page.getByRole("button", { name: "恢复此版本" }).first()).toBeVisible();
  });

  test("登出后已加载的 owner 记忆立即隐藏", async ({ page }) => {
    await page.goto("/memory");
    await loginAsAdmin(page);
    await expect(page.locator(".memory-card")).not.toHaveCount(0);

    await page.getByRole("button", { name: "登出" }).click();

    await expect(page.locator(".memory-card")).toHaveCount(0);
    await expect(page.getByRole("heading", { name: "登录后才会打开个人记忆" })).toBeVisible();
  });
});
