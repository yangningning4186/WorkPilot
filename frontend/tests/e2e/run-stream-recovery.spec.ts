import { expect, test } from "@playwright/test";

import { answerCopy, ask, citationCards, mockRequests } from "./helpers";

/**
 * 边界情况 B1 / B2 与失败态。
 *
 * 这几条是 run 事件溯源（ADR-0007）在前端的落地验收：run_events 是唯一真相源，
 * 所以"实时看到的"和"刷新后看到的"必须逐字一致，断线重连也不许把正文重放两遍。
 */
test.describe("流式恢复与失败态", () => {
  test("B1 刷新恢复：刷新后正文与引用逐字一致", async ({ page }) => {
    await ask(page, "混合检索为什么比单路召回更稳定？");
    await expect(citationCards(page)).toHaveCount(2);
    const before = await answerCopy(page).innerText();

    await page.reload();

    // 刷新走的是同一条 after_seq=0 的补历史路径，不是重新生成。
    await expect(citationCards(page)).toHaveCount(2);
    await expect(answerCopy(page)).toHaveText(before);
    await expect(page.locator(".run-footnote")).toContainText("本次回答耗时 1.8 秒");

    // 恢复出来的引用同样要能点开原文。
    await citationCards(page).first().click();
    await expect(page.getByLabel("引用原文预览").getByLabel("引用原文高亮")).toHaveCount(1);
  });

  test("B2 断线续传：重连后从断点续上，正文不重复不缺段", async ({ page, request }) => {
    const runId = await ask(page, "这一题会在中途断线");

    await expect(answerCopy(page)).toContainText("断线前的前半句");

    // 逐字相等是关键：从头重放会变成"断线前的前半句"出现两次。
    await expect(answerCopy(page)).toHaveText(
      "断线前的前半句，断线后续上的后半句，以及结尾 [S1]。",
    );
    await expect(citationCards(page)).toHaveCount(1);

    // 确认真的断过并重连了，否则这条用例可能只是"一次连接跑到底"。
    const eventStreams = (await mockRequests(request)).filter(
      (item) => item.path === `/api/v1/runs/${runId}/events`,
    );
    expect(eventStreams.length).toBeGreaterThanOrEqual(2);
  });

  test("重连重放：服务端重发看过的事件，前端按 seq 去重不写第二遍", async ({ page }) => {
    await ask(page, "这一题重连后会重放全部事件");

    await expect(answerCopy(page)).toContainText("重放前的前半句");
    await expect(answerCopy(page)).toHaveText(
      "重放前的前半句，重放后补上的后半句，以及结尾 [S1]。",
    );
    // 引用同样只留一份，不因重放变两张卡。
    await expect(citationCards(page)).toHaveCount(1);
  });

  test("取消：停止按钮落到 cancel 接口，页面给出终态", async ({ page, request }) => {
    const runId = await ask(page, "这一题写到一半会被取消");

    await expect(answerCopy(page)).toContainText("正在写第一段");
    const stop = page.getByRole("button", { name: "停止" });
    await expect(stop).toBeVisible();
    await stop.click();

    await expect(page.locator(".inline-error")).toContainText("回答已取消");
    // 取消是可重试的，页面要给出这个提示。
    await expect(page.locator(".inline-error")).toContainText("稍后可以重新提问");
    // 终态之后不该再留着停止按钮。
    await expect(page.getByRole("button", { name: "停止" })).toHaveCount(0);

    const requests = await mockRequests(request);
    expect(requests.some((item) => item.path === `/api/v1/runs/${runId}/cancel`)).toBe(true);
  });

  test("失败态：错误事件显示可读原因，且不谎报可重试", async ({ page }) => {
    await ask(page, "这一题会报错");

    await expect(page.locator(".inline-error")).toContainText("本次回答超出了每日费用上限");
    // retryable=false 时不能出现"稍后可以重新提问"。
    await expect(page.locator(".inline-error")).not.toContainText("稍后可以重新提问");
    // 已经写出来的半截正文要保留，不能被错误态抹掉。
    await expect(answerCopy(page)).toContainText("正在整理证据");
    await expect(page.getByRole("button", { name: "停止" })).toHaveCount(0);
  });

  /**
   * B3 的后端承诺（worker 不依附 HTTP 连接）只能在后端验；这里验前端那一半：
   * run 的状态完全由 URL 上的 run_id 决定，关掉页面再从别处打开同一个 run，
   * 拿到的应该是完整结果，而不是"刚才那个标签页里的内存状态没了"。
   */
  test("B3 关掉页面再回来：凭 run URL 拿回完整答案", async ({ page, context }) => {
    await ask(page, "混合检索为什么比单路召回更稳定？");
    await expect(answerCopy(page)).toContainText("失败模式并不重叠");
    const runUrl = page.url();
    await page.close();

    const reopened = await context.newPage();
    await reopened.goto(runUrl);
    await expect(reopened.locator(".answer-copy")).toContainText("0.62 提到 0.81");
    await expect(reopened.locator("button.citation-card")).toHaveCount(2);
    await reopened.close();
  });

  /** B5 并发隔离：两个 run 各自一条 EventSource、各自一份 state，正文不许串台。 */
  test("B5 并发隔离：两个标签页的回答互不污染", async ({ page, context }) => {
    const second = await context.newPage();
    await ask(page, "混合检索为什么比单路召回更稳定？");
    await ask(second, "markdown 笔记里英文集的结论是什么？");

    await expect(answerCopy(page)).toContainText("0.62 提到 0.81");
    await expect(second.locator(".answer-copy")).toContainText("融合后增量为零");

    await expect(answerCopy(page)).not.toContainText("融合后增量为零");
    await expect(second.locator(".answer-copy")).not.toContainText("0.62 提到 0.81");
    await expect(second.locator("button.citation-card")).toHaveCount(1);
    await second.close();
  });

  test("创建 run 失败：给出可读错误，不留在假的加载态", async ({ page }) => {
    await page.route("**/api/v1/runs", (route) =>
      route.fulfill({ status: 503, body: "backend unavailable" }),
    );
    await page.goto("/");
    await page.getByLabel("向资料库提问").fill("后端挂了的时候会怎样？");
    await page.getByRole("button", { name: "提问" }).click();

    await expect(page.locator(".inline-error")).toContainText("创建回答失败（503）");
    // 没有 run 就不该出现回答区，输入框要能立刻重试。
    await expect(page.locator(".run-result")).toHaveCount(0);
    await page.getByLabel("向资料库提问").fill("再试一次");
    await expect(page.getByRole("button", { name: "提问" })).toBeEnabled();
  });
});
