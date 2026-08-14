import { expect, test } from "@playwright/test";

import { PDF_CITATION_S1 } from "./fixtures/scenarios.mjs";
import { answerCopy, ask, evidencePanel, expectHighlightMatchesBbox } from "./helpers";
import { S1_BBOX_PAGE3 } from "./fixtures/scenarios.mjs";

/**
 * 回答正文的 Markdown 渲染。
 *
 * 三个真正会出事的点：流式半截语法、正文里的引用锚点、以及证据里的 HTML 注入。
 * "加粗显示成粗体"只是其中最不重要的一条。
 */
test.describe("回答 Markdown 渲染", () => {
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

  test("正文里的 [S1] 是可点锚点，点了直接打开对应原文", async ({ page }) => {
    await ask(page, "帮我看看这段回答的排版");
    await expect(answerCopy(page).locator("table")).toHaveCount(1);

    const chips = answerCopy(page).locator(".citation-chip");
    // 正文里出现两处引用锚点（S1、S2），代码块里那个不算。
    await expect(chips).toHaveCount(2);
    const first = chips.first();
    await expect(first).toHaveText("S1");
    await expect(first).toHaveAttribute("aria-label", "查看引用 S1 的原文");

    await first.click();

    const panel = evidencePanel(page);
    await expect(panel).toBeVisible();
    await expect(panel.locator(".eyebrow")).toContainText("原文证据 · S1");
    await expect(first).toHaveAttribute("aria-pressed", "true");
    // 点正文锚点和点下方引用卡片必须落到同一个高亮，不能是两套状态。
    await expectHighlightMatchesBbox(
      panel.locator(".pdf-canvas"),
      panel.getByLabel("引用原文高亮"),
      S1_BBOX_PAGE3,
    );
    await expect(panel.getByRole("heading", { level: 2 })).toHaveText(PDF_CITATION_S1.title);
  });

  test("代码块里的 [S1] 保持字面量，不会被误认成引用", async ({ page }) => {
    await ask(page, "帮我看看这段回答的排版");
    await expect(answerCopy(page).locator("pre code")).toContainText("[S1]");

    // 代码块内部一个 chip 都不该有：论文里出现 [S1] 字样是常事，误转会把代码改写掉。
    await expect(answerCopy(page).locator("pre .citation-chip")).toHaveCount(0);
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
