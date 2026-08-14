import { expect, test } from "@playwright/test";

import {
  MD_CITATION,
  PDF_CITATION_S1,
  PDF_CITATION_S2,
  S1_BBOX_PAGE3,
  S1_BBOX_PAGE4,
  S2_BBOX_PAGE5,
} from "./fixtures/scenarios.mjs";
import {
  answerCopy,
  ask,
  citationCards,
  evidencePanel,
  expectHighlightMatchesBbox,
  mockRequests,
} from "./helpers";

test.describe("提问 → SSE 回答 → 点击引用 → 原文高亮", () => {
  test("PDF 引用：正文流式出现，点击引用后高亮落在 bbox_norm 指定位置", async ({
    page,
    request,
  }) => {
    const runId = await ask(page, "混合检索为什么比单路召回更稳定？");

    // 1) 流式：先看到前半句，此时后半句还没到。
    //    这一步失败通常意味着 SSE 被某一层缓冲成了整包响应。
    await expect(answerCopy(page)).toContainText("失败模式并不重叠");
    await expect(answerCopy(page)).not.toContainText("0.62 提到 0.81");

    // 2) 终态：正文补齐，成本与耗时落到页脚。
    await expect(answerCopy(page)).toContainText("0.62 提到 0.81");
    await expect(page.locator(".run-footnote")).toContainText("本次回答耗时 1.8 秒");
    await expect(page.locator(".run-footnote")).toContainText("$0.0031");

    // 3) 引用卡片：两条，且带出处与页码。
    await expect(citationCards(page)).toHaveCount(2);
    const firstCard = citationCards(page).first();
    await expect(firstCard).toContainText(PDF_CITATION_S1.title);
    await expect(firstCard).toContainText("4.2 融合策略");
    await expect(firstCard).toContainText("第 3 页");
    await expect(firstCard).toContainText(PDF_CITATION_S1.quote);

    // 4) 点击引用 → 原文预览打开，且是这条引用对应的文档。
    await firstCard.click();
    await expect(firstCard).toHaveAttribute("aria-pressed", "true");
    const panel = evidencePanel(page);
    await expect(panel).toBeVisible();
    await expect(panel.locator(".eyebrow")).toContainText("原文证据 · S1");
    await expect(panel.getByRole("heading", { level: 2 })).toHaveText(PDF_CITATION_S1.title);
    await expect(panel.locator(".breadcrumb")).toHaveText("4 检索 / 4.2 融合策略");

    // 5) 高亮：位置必须与 bbox_norm × 渲染尺寸一致，光是"面板打开了"不算验收通过。
    const highlight = panel.getByLabel("引用原文高亮");
    await expect(highlight).toHaveCount(1);
    await expectHighlightMatchesBbox(
      panel.locator(".pdf-canvas"),
      highlight,
      S1_BBOX_PAGE3,
    );

    // 6) 页面图确实是按 version_id + 页码取的，不是随便渲染了一张。
    const requests = await mockRequests(request);
    expect(
      requests.some(
        (item) => item.path === `/api/v1/documents/${PDF_CITATION_S1.version_id}/pages/3.png`,
      ),
    ).toBe(true);

    // 7) 兜底出口：打开完整原文要跳到同一版本，并定位到引用所在页。
    await expect(panel.getByRole("link", { name: "打开完整原文" })).toHaveAttribute(
      "href",
      `/api/v1/documents/${PDF_CITATION_S1.version_id}/file#page=3`,
    );

    expect(runId.length).toBeGreaterThan(0);
  });

  test("跨页引用：切换页码 tab 后高亮跟着换到该页的 bbox", async ({ page }) => {
    await ask(page, "混合检索为什么比单路召回更稳定？");
    await citationCards(page).first().click();

    const panel = evidencePanel(page);
    const tabs = panel.getByLabel("引用所在页面").getByRole("button");
    await expect(tabs).toHaveCount(2);
    await expect(tabs.first()).toHaveText("第 3 页");

    await expect(panel.getByLabel("引用原文高亮")).toHaveCount(1);
    await tabs.nth(1).click();

    // 换页后要重新等图片加载完，高亮才会重新挂上去。
    const highlight = panel.getByLabel("引用原文高亮");
    await expect(highlight).toHaveCount(1);
    await expectHighlightMatchesBbox(
      panel.locator(".pdf-canvas"),
      highlight,
      S1_BBOX_PAGE4,
    );
  });

  test("切换引用与关闭预览：面板内容跟随选中项，关闭后回到占位态", async ({ page }) => {
    await ask(page, "混合检索为什么比单路召回更稳定？");
    await expect(citationCards(page)).toHaveCount(2);

    await expect(citationCards(page).nth(1)).toContainText(PDF_CITATION_S2.quote);
    await citationCards(page).nth(1).click();
    const panel = evidencePanel(page);
    await expect(panel.locator(".eyebrow")).toContainText("原文证据 · S2");
    await expect(panel.locator(".breadcrumb")).toHaveText("5 实验 / 5.1 分类别结果");
    // 单页引用不出页码 tab。
    await expect(panel.getByLabel("引用所在页面")).toHaveCount(0);
    await expectHighlightMatchesBbox(
      panel.locator(".pdf-canvas"),
      panel.getByLabel("引用原文高亮"),
      S2_BBOX_PAGE5,
    );

    await panel.getByRole("button", { name: "关闭原文预览" }).click();
    await expect(evidencePanel(page)).toHaveCount(0);
    await expect(page.getByLabel("原文预览占位")).toBeVisible();
    await expect(citationCards(page).nth(1)).toHaveAttribute("aria-pressed", "false");
  });

  test("Markdown 引用：原文按 quote 精确高亮", async ({ page }) => {
    await ask(page, "markdown 笔记里英文集的结论是什么？");
    await expect(citationCards(page)).toHaveCount(1);
    await citationCards(page).first().click();

    const panel = evidencePanel(page);
    await expect(panel).toBeVisible();
    // markdown 走文本高亮分支，不应该去请求 PDF 页图。
    await expect(panel.locator(".pdf-canvas")).toHaveCount(0);
    await expect(panel.locator(".markdown-preview mark")).toHaveText(MD_CITATION.quote);
    // 命中 quote 时不应该退化成 fallback 卡片。
    await expect(panel.locator(".quote-fallback")).toHaveCount(0);
  });

  test("拒答：不给引用、不给预览，明确说明没有证据", async ({ page }) => {
    await ask(page, "请回答一个资料库里没有的问题，应该拒答");

    await expect(page.locator(".refusal-card")).toContainText("资料库中未找到足够证据");
    await expect(citationCards(page)).toHaveCount(0);
    await expect(evidencePanel(page)).toHaveCount(0);
    await expect(page.locator(".answer-copy")).toHaveCount(0);
    // 拒答也是正常终态：输入框要能继续提问。
    await expect(page.getByRole("button", { name: "提问" })).toBeVisible();
  });
});
