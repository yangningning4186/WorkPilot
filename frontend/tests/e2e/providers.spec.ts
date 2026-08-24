import { expect, test } from "@playwright/test";

import { loginAsAdmin, MOCK_BASE } from "./helpers";

test.describe("模型服务配置", () => {
  test.beforeEach(async ({ request }) => {
    const response = await request.post(`${MOCK_BASE}/__reset`);
    expect(response.ok()).toBe(true);
  });

  test("不预填模型或地址，也不要求用户配置上下文窗口", async ({ page }) => {
    await page.goto("/providers");
    await loginAsAdmin(page);

    await expect(page.getByRole("heading", { name: "模型与密钥" })).toBeVisible();
    await expect(page.getByLabel("显示名称")).toHaveValue("");
    await expect(page.getByLabel("Base URL")).toHaveValue("");
    await expect(page.getByLabel("模型 ID")).toHaveValue("");
    await expect(page.getByText("上下文 tokens", { exact: true })).toHaveCount(0);
    await expect(page.getByText("上下文容量由系统管理", { exact: false })).toBeVisible();
    await expect(page.getByRole("button", { name: "保存 Provider" })).toBeDisabled();

    await page.getByLabel("Provider").selectOption("gemini");
    await expect(page.getByLabel("显示名称")).toHaveValue("");
    await expect(page.getByLabel("Base URL")).toHaveValue("");
    await expect(page.getByLabel("模型 ID")).toHaveValue("");
  });

  test("没有用户配置 Provider 时 Cowork 不会使用系统默认模型", async ({ page }) => {
    await page.route("**/api/v1/providers", (route) => route.fulfill({
      json: { items: [] },
      status: 200,
    }));
    await page.goto("/cowork?new=1");
    await loginAsAdmin(page);

    await page.getByLabel("你想让 Cowork 完成什么？").fill("检查未配置状态");
    await expect(page.getByText("请先配置模型服务", { exact: true })).toBeVisible();
    await expect(page.getByRole("button", { name: "开始执行任务" })).toBeDisabled();
    await page.locator(".workdesk-run-settings > summary").click();
    await expect(page.getByLabel("模型服务")).toHaveValue("");
    await expect(page.getByRole("link", { name: "前往“模型与密钥”配置" })).toHaveAttribute(
      "href",
      "/providers",
    );
  });
});
