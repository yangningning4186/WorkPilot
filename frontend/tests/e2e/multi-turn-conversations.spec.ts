import { expect, test } from "@playwright/test";

import { answerCopy } from "./helpers";

test("用户可以新建和切换会话，时间线不会跨会话串联", async ({ page }) => {
  await page.goto("/");
  await page.getByRole("button", { name: "新建会话" }).click();
  await expect(page).toHaveURL(/[?&]conversation=/);
  const firstConversation = new URL(page.url()).searchParams.get("conversation");
  expect(firstConversation).not.toBeNull();

  await page.getByLabel("向资料库提问").fill("混合检索为什么比单路召回更稳定？");
  await page.getByRole("button", { name: "提问" }).click();
  await expect(answerCopy(page)).toContainText("0.62 提到 0.81");

  await page.getByRole("button", { name: "新建会话" }).click();
  await expect
    .poll(() => new URL(page.url()).searchParams.get("conversation"))
    .not.toBe(firstConversation);
  const secondConversation = new URL(page.url()).searchParams.get("conversation");
  expect(secondConversation).not.toBe(firstConversation);
  await expect(page.getByText("混合检索为什么比单路召回更稳定？")).toHaveCount(0);
  await expect(answerCopy(page)).toHaveCount(0);

  await page.locator(`[data-conversation-id="${firstConversation}"]`).click();
  await expect(page).toHaveURL(new RegExp(`conversation=${firstConversation}`));
  await expect(page.getByText("混合检索为什么比单路召回更稳定？")).toBeVisible();
  await expect(answerCopy(page)).toContainText("0.62 提到 0.81");

  const firstRow = page.locator(".conversation-list li", {
    has: page.locator(`[data-conversation-id="${firstConversation}"]`),
  });
  await firstRow.getByRole("button", { name: /删除会话/ }).click();
  const dialog = page.getByRole("alertdialog", { name: "新会话" });
  await expect(dialog).toContainText("长期记忆仍会保留");
  await dialog.getByRole("button", { name: "取消" }).click();
  await expect(firstRow).toBeVisible();

  await firstRow.getByRole("button", { name: /删除会话/ }).click();
  await page.getByRole("button", { name: "确认删除" }).click();
  await expect(page).toHaveURL(new RegExp(`conversation=${secondConversation}`));
  await expect(page.locator(`[data-conversation-id="${firstConversation}"]`)).toHaveCount(0);
  await expect(page.getByText("混合检索为什么比单路召回更稳定？")).toHaveCount(0);
});
