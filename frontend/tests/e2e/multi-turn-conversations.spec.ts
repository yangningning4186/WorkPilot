import { expect, test } from "@playwright/test";

import { answerCopy, ask, MOCK_BASE, mockRuns, selectConfiguredProvider } from "./helpers";

test("用户可以新建、切换和删除会话，时间线不会跨会话串联", async ({ page, request }) => {
  const reset = await request.post(`${MOCK_BASE}/__reset`);
  expect(reset.ok()).toBe(true);

  const firstPrompt = "混合检索为什么比单路召回更稳定？";
  await ask(page, firstPrompt);
  await expect(answerCopy(page)).toContainText("0.62 提到 0.81");
  const firstConversation = (await mockRuns(request))[0]?.conversation_id;
  expect(firstConversation).toBeDefined();

  await page.getByRole("button", { name: "新建任务" }).click();
  await expect(page).toHaveURL(/\/cowork\?new=1$/);
  await expect(page.locator(".workdesk-message")).toHaveCount(0);
  await expect(page.locator(".answer-copy")).toHaveCount(0);
  await selectConfiguredProvider(page);

  const secondPrompt = "整理第二份独立任务";
  await page.getByLabel("你想让 Cowork 完成什么？").fill(secondPrompt);
  await page.getByRole("button", { name: "开始执行任务" }).click();
  await expect(answerCopy(page)).toContainText("已将季度汇报改为管理层语气");
  const allRuns = await mockRuns(request);
  const secondConversation = allRuns[1]?.conversation_id;
  expect(secondConversation).toBeDefined();
  expect(secondConversation).not.toBe(firstConversation);

  const firstRow = page.locator(`[data-conversation-id="${firstConversation}"]`);
  await firstRow.locator(".workdesk-task-select").click();
  await expect(page).toHaveURL(new RegExp(`conversation=${firstConversation}`));
  await expect(page.locator(".workdesk-message.user")).toContainText(firstPrompt);
  await expect(answerCopy(page)).toContainText("0.62 提到 0.81");
  await expect(page.locator(".workdesk-message", { hasText: secondPrompt })).toHaveCount(0);

  await firstRow.getByRole("button", { name: /管理会话/ }).click();
  await firstRow.getByRole("menuitem", { name: "永久删除" }).click();
  const dialog = page.getByRole("alertdialog", { name: firstPrompt });
  await expect(dialog).toContainText("已经生成的文件不会被删除");
  await dialog.getByRole("button", { name: "取消" }).click();
  await expect(firstRow).toBeVisible();

  await firstRow.getByRole("button", { name: /管理会话/ }).click();
  await firstRow.getByRole("menuitem", { name: "永久删除" }).click();
  await page.getByRole("button", { name: "确认永久删除" }).click();
  await expect(page).toHaveURL(new RegExp(`conversation=${secondConversation}`));
  await expect(page.locator(`[data-conversation-id="${firstConversation}"]`)).toHaveCount(0);
  await expect(page.locator(".workdesk-message", { hasText: firstPrompt })).toHaveCount(0);
});
