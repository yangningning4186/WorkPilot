import { expect, test } from "@playwright/test";

import { answerCopy, ask, MOCK_BASE } from "./helpers";

/**
 * 回答正文的 Markdown 渲染。
 *
 * 三个真正会出事的点：流式半截语法、正文里的引用锚点、以及证据里的 HTML 注入。
 * "加粗显示成粗体"只是其中最不重要的一条。
 */
test.describe("回答 Markdown 渲染", () => {
  test.beforeEach(async ({ request }) => {
    const response = await request.post(`${MOCK_BASE}/__reset`);
    expect(response.ok()).toBe(true);
  });

  test("流式期间先到的块已经排好版，后到的表格随后补齐", async ({ page }) => {
    await ask(page, "帮我看看这段回答的排版");

    // 第一段到达时就已经是渲染好的标题 + 加粗，而不是等全文到齐再排版。
    await expect(answerCopy(page).locator("h2")).toHaveText("结论");
    await expect(answerCopy(page).locator("strong")).toHaveText("互补而非叠加");
    // 此刻表格还没开始到；半截的表格语法不能被渲染成坏掉的表格。
    await expect(answerCopy(page).locator("table")).toHaveCount(0);

    await expect(answerCopy(page).locator("li")).toHaveCount(2);
    await expect(answerCopy(page).locator("table")).toHaveCount(1);
    await expect(answerCopy(page).locator("table th").first()).toHaveText("类别");
    await expect(answerCopy(page).locator("table td")).toHaveCount(4);
    await expect(answerCopy(page).locator("pre code")).toContainText('search("rrf")');
    await expect(answerCopy(page).locator("p code").first()).toHaveText("search(query)");
  });

  test("引用标记不会冒充可用证据，论文 locator 可以打开对应页", async ({ page }) => {
    await ask(page, "帮我看看这段回答的排版", {
      readingPath: "/Users/demo/Documents/Quarterly/paper.pdf",
    });
    await expect(answerCopy(page).locator("table")).toHaveCount(1);

    const chips = answerCopy(page).locator(".citation-chip");
    // Cowork 没有旧 RAG 页的 citation payload 时，S1/S2 只能是静态标记，不能假装可点。
    await expect(chips).toHaveCount(2);
    await expect(chips.first()).toHaveClass(/static/);
    await expect(chips.first()).not.toHaveAttribute("aria-label", /.+/);

    const locator = answerCopy(page).getByRole("button", { name: "在阅读器中打开第 2 处" });
    await expect(locator).toBeVisible();
    await locator.click();
    await expect(page.getByRole("complementary", { name: "阅读器" })).toContainText("第 2 / 2 页");
  });

  test("代码块里的 [S1] 保持字面量，不会被误认成引用", async ({ page }) => {
    await ask(page, "帮我看看这段回答的排版");
    await expect(answerCopy(page).locator("pre code")).toContainText("[S1]");

    // 代码块内部一个 chip 都不该有：论文里出现 [S1] 字样是常事，误转会把代码改写掉。
    await expect(answerCopy(page).locator("pre .citation-chip")).toHaveCount(0);
  });

  test("旧事件中的跨片 think 块不会混入正文", async ({ page }) => {
    await ask(page, "验证思维链不进入正文");

    await expect(answerCopy(page)).toHaveText("我是 WorkPilot。");
    await expect(answerCopy(page)).not.toContainText("内部推理");
    await expect(answerCopy(page)).not.toContainText("</think>");
    await expect(page.locator(".workdesk-stage-history summary").first()).toContainText("2 个阶段");
    await expect.poll(async () => {
      const stageBox = await page.locator(".workdesk-stage-history").first().boundingBox();
      const answerBox = await answerCopy(page).boundingBox();
      return stageBox !== null && answerBox !== null && stageBox.y < answerBox.y;
    }).toBe(true);
    await page.locator(".workdesk-stage-history > summary").click();
    await expect(page.locator(".workdesk-stage-list")).toContainText("这是内部推理，不应进入正文。");
    await expect(page.locator(".workdesk-stage-list")).toContainText("正在读取资料。");
    await expect(page.locator(".workdesk-stage-list")).toContainText("核对读取结果。");

    // 任务结束并刷新后不再依赖 active_run_id；历史 assistant 仍可按 run_id 回放完整阶段。
    await page.reload();
    await expect.poll(async () => {
      const stageBox = await page.locator(".workdesk-historical-stages").boundingBox();
      const answerBox = await answerCopy(page).boundingBox();
      return stageBox !== null && answerBox !== null && stageBox.y < answerBox.y;
    }).toBe(true);
    await page.locator(".workdesk-historical-stages > button").click();
    await expect(page.locator(".workdesk-historical-stages .workdesk-stage-list")).toContainText("正在读取资料。");
    await expect(page.locator(".workdesk-historical-stages .workdesk-stage-list")).toContainText("核对读取结果。");
  });

  test("证据里的 HTML 与 javascript: 链接不会变成真元素", async ({ page }) => {
    await ask(page, "帮我看看这段回答的排版");
    await expect(answerCopy(page).locator("table")).toHaveCount(1);

    // 裸 HTML 不解析：既不能执行，也不能变成页面结构。
    await expect(answerCopy(page).locator("script")).toHaveCount(0);
    expect(await page.evaluate(() => "pwned" in window)).toBe(false);

    // javascript: 协议要被 url 过滤掉，链接不能真的指向它。
    const link = answerCopy(page).getByRole("link", { name: "点我" });
    await expect(link).toHaveCount(1);
    const href = await link.getAttribute("href");
    expect(href ?? "").not.toContain("javascript:");
    // 外链一律新窗口 + 断开 opener。
    await expect(link).toHaveAttribute("rel", /noopener/);
  });
});
