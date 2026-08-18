import { expect, test } from "@playwright/test";

import { loginAsAdmin, MOCK_BASE, mockRequests } from "./helpers";

test.describe("Cowork 工作台", () => {
  test.beforeEach(async ({ request }) => {
    const response = await request.post(`${MOCK_BASE}/__reset`);
    expect(response.ok()).toBe(true);
  });

  test("roots、Progress 和 Artifacts 连成一条可恢复执行链", async ({ page, request }) => {
    await page.goto("/cowork");
    await expect(page.getByRole("heading", { name: "等待 owner 身份" })).toBeVisible();
    await loginAsAdmin(page);

    await expect(page.getByRole("heading", { name: "WorkPilot，我帮你" })).toBeVisible();
    await expect(page.locator(".workdesk-space strong", { hasText: "Quarterly" })).toBeVisible();
    const permissionMenu = page.locator(".workdesk-permission-menu");
    await permissionMenu.getByText("读写与 Office 编辑", { exact: true }).click();
    await expect(permissionMenu).toContainText("编辑 Word");
    await expect(permissionMenu).toContainText("编辑 Excel");

    await page
      .getByLabel("你想让 Cowork 完成什么？")
      .fill("把季度汇报改成管理层语气，并保留原有数据");
    await page.getByRole("button", { name: /开始执行/ }).click();

    await expect(page.getByText("扫描 Word / Excel")).toBeVisible();
    await expect(page.getByText("读取文档结构")).toBeVisible();
    await expect(page.getByText("执行完成").first()).toBeVisible();
    await expect(page.getByText("已将季度汇报改为管理层语气，并保留原有数据。")).toBeVisible();
    await expect(page.getByText("季度汇报.docx", { exact: true })).toBeVisible();
    await expect(page.getByText("已更新标题与结论段")).toBeVisible();
    await expect(page.getByRole("button", { name: /批准|确认应用/ })).toHaveCount(0);

    const calls = await mockRequests(request);
    expect(calls.some((item) => item.method === "POST" && item.path === "/api/v1/runs/cowork")).toBe(true);
    expect(calls.some((item) => item.path.endsWith("/event-log"))).toBe(true);
  });

  test("运行中的 Cowork 可以从 Progress 安全停止", async ({ page, request }) => {
    await page.goto("/cowork");
    await loginAsAdmin(page);

    await page
      .getByLabel("你想让 Cowork 完成什么？")
      .fill("保持运行直到我停止");
    await page.getByRole("button", { name: /开始执行/ }).click();

    const stop = page.getByRole("button", { name: "停止 Cowork 任务" });
    await expect(stop).toBeVisible();
    await stop.click();
    await expect(page.getByText("任务已停止", { exact: true })).toBeVisible();
    await expect(page.getByText("Cowork 任务已停止。已完成的文件修改会保留。", { exact: true })).toBeVisible();
    await expect(page.getByRole("button", { name: /停止 Cowork 任务/ })).toHaveCount(0);

    const calls = await mockRequests(request);
    expect(calls.some((item) => item.method === "POST" && item.path.endsWith("/cancel"))).toBe(true);
  });
});
