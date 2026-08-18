import { expect, test } from "@playwright/test";

import { loginAsAdmin, MOCK_BASE, mockRequests } from "./helpers";

test.describe("办公工作台", () => {
  test.beforeEach(async ({ request }) => {
    const response = await request.post(`${MOCK_BASE}/__reset`);
    expect(response.ok()).toBe(true);
  });

  test("匿名会话看不到本地文件，owner 登录后可看到三种格式", async ({ page }) => {
    await page.goto("/workspace");

    await expect(page.getByRole("heading", { name: "办公工作台只对 owner 开放" })).toBeVisible();
    await loginAsAdmin(page);

    await expect(page.getByRole("button", { name: /项目简报\.docx/ })).toBeVisible();
    await expect(page.getByRole("button", { name: /季度预算\.xlsx/ })).toBeVisible();
    await expect(page.getByRole("button", { name: /检索评测笔记\.md/ })).toBeVisible();
    await expect(page.getByLabel("办公文档内容")).toHaveValue(/当前进展需要进一步整理/);
  });

  test("用户授予限时权限后，Word 指令直接写入且不再逐次确认", async ({ page, request }) => {
    await page.goto("/workspace");
    await loginAsAdmin(page);
    await expect(page.getByRole("button", { name: /项目简报\.docx/ })).toBeVisible();

    const execute = page.getByRole("button", { name: "执行并直接写入" });
    await page.getByLabel("修改指令").fill("把进展段改成正式汇报语气");
    await expect(execute).toBeDisabled();

    await page.getByRole("button", { name: "授予限时写权限" }).click();
    await expect(page.getByText("本地写权限已启用")).toBeVisible();
    await expect(execute).toBeEnabled();
    await execute.click();

    await expect(page.getByRole("status")).toContainText("已直接写入 1 处修改");
    await expect(page.getByLabel("办公文档内容")).toHaveValue(/已由 WorkPilot 直接修改/);
    await expect(page.getByText("已按指令直接修改办公文档")).toBeVisible();
    await expect(page.getByRole("button", { name: /批准|确认应用/ })).toHaveCount(0);

    const calls = await mockRequests(request);
    expect(
      calls.some(
        (item) =>
          item.method === "POST" &&
          item.path === "/api/v1/editor/files/mock-word-file/execute",
      ),
    ).toBe(true);
  });

  test("Excel 使用同一权限直接执行单元格操作，收回权限后立即停写", async ({ page }) => {
    await page.goto("/workspace");
    await loginAsAdmin(page);
    await page.getByRole("button", { name: /季度预算\.xlsx/ }).click();
    await expect(page.getByLabel("办公文档内容")).toHaveValue(/\[预算!B2\] 1000/);

    await page.getByRole("button", { name: "授予限时写权限" }).click();
    await page.getByLabel("修改指令").fill("在 C2 写入 B2 的两倍公式");
    await page.getByRole("button", { name: "执行并直接写入" }).click();

    await expect(page.getByLabel("办公文档内容")).toHaveValue(/\[预算!C2\] =B2\*2/);
    await page.getByRole("button", { name: "收回权限" }).click();
    await expect(page.getByRole("button", { name: "执行并直接写入" })).toBeDisabled();
    await expect(page.getByText("写权限已收回")).toBeVisible();
  });
});
