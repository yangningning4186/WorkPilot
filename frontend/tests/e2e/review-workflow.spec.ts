import { type Page, expect, test } from "@playwright/test";

import { LIBRARY, REVIEW_DRAFT, REVIEW_OUTPUT_PATH } from "./fixtures/scenarios.mjs";
import { mockRequests } from "./helpers";

/**
 * 固定综述页验收。
 *
 * 核心断言只有一条：**人工确认之前，界面上不能出现任何"已写入"的痕迹**。
 * 写回是整条工作流唯一有副作用的一步（ADR-0007 的 HITL 边界），
 * 如果预览和写回在界面上长得一样，用户就无从判断自己批准的到底是什么。
 * 其余断言（时间线、恢复提示、刷新续流）都是围绕这一条的支撑。
 */

const REVIEWABLE_DOCS = LIBRARY.documents.filter(
  (item) => item.version_id !== null && item.searchable_chunk_count > 0,
);

async function fillForm(page: Page, goal: string): Promise<void> {
  await page.goto("/review");
  await page.getByRole("textbox", { name: "综述目标" }).fill(goal);
  await page.getByRole("textbox", { name: "写回路径" }).fill(REVIEW_OUTPUT_PATH);
  const boxes = page.locator(".doc-picker input[type=checkbox]");
  await boxes.nth(0).check();
  await boxes.nth(1).check();
}

async function startReview(page: Page, goal: string): Promise<string> {
  await fillForm(page, goal);
  await page.getByRole("button", { name: "开始生成综述" }).click();
  await expect(page).toHaveURL(/[?&]run=/);
  return new URL(page.url()).searchParams.get("run") as string;
}

test.describe("固定综述页", () => {
  test("只能选可检索文档，且不足两篇时开不了工", async ({ page }) => {
    await page.goto("/review");

    // 按真实能力过滤：新版失败但旧激活版仍在服务的文档可以选；没有激活版本或
    // 没有可检索块的文档不能选。
    await expect(page.locator(".doc-picker li")).toHaveCount(REVIEWABLE_DOCS.length);
    await expect(page.locator(".doc-picker")).toContainText("扫描件年报");
    await expect(page.locator(".doc-picker")).not.toContainText("刚拖进来的论文");

    const start = page.getByRole("button", { name: "开始生成综述" });
    await expect(start).toBeDisabled();

    await page.getByRole("textbox", { name: "综述目标" }).fill("比较两篇的取舍");
    await page.locator(".doc-picker input[type=checkbox]").first().check();
    // 只选一篇仍然不行——固定综述至少要两篇才谈得上"比较"。
    await expect(start).toBeDisabled();

    await page.locator(".doc-picker input[type=checkbox]").nth(1).check();
    await expect(start).toBeEnabled();
  });

  test("写回路径非法时前端就拦下，不往后端发", async ({ page, request }) => {
    await page.goto("/review");
    await page.getByRole("textbox", { name: "综述目标" }).fill("比较两篇的取舍");
    await page.locator(".doc-picker input[type=checkbox]").nth(0).check();
    await page.locator(".doc-picker input[type=checkbox]").nth(1).check();

    await page.getByRole("textbox", { name: "写回路径" }).fill("../outside.md");
    await expect(page.locator(".field-error")).toHaveText(/不能用绝对路径或 \.\./);
    await expect(page.getByRole("button", { name: "开始生成综述" })).toBeDisabled();

    await page.getByRole("textbox", { name: "写回路径" }).fill("notes/plain.txt");
    await expect(page.locator(".field-error")).toHaveText("只能写入 .md 文件");

    const log = await mockRequests(request);
    expect(
      log.filter((item: { path: string }) => item.path === "/api/v1/runs/reviews"),
    ).toHaveLength(0);
  });

  test("时间线推进到人工确认点，且确认前没有任何写回痕迹", async ({ page }) => {
    await startReview(page, "比较 AgentBench 与 CODESKILL 的侧重差异");

    const steps = page.locator(".timeline-step");
    await expect(steps).toHaveCount(6);
    await expect(steps.nth(0).locator(".step-badge")).toHaveText("已完成");
    await expect(steps.nth(0).locator(".timeline-summary")).toHaveText("已确认 2 篇文档");

    // 走到生成预览这一步时，第六步（写回）必须还停在"待执行"。
    await expect(steps.nth(4).locator(".step-badge")).toHaveText("已完成", { timeout: 15000 });
    await expect(steps.nth(5).locator(".step-badge")).toHaveText("待执行");

    await expect(page.locator(".preview-card .preview-body")).toHaveText(REVIEW_DRAFT);

    const approval = page.getByRole("group", { name: "写回确认" });
    await expect(approval).toBeVisible();
    await expect(approval).toContainText(REVIEW_OUTPUT_PATH);
    // 这是本用例的要害：预览已经出来了，但"已写入"绝不能出现。
    await expect(page.locator(".written-note")).toHaveCount(0);
  });

  test("拒绝写回：步骤标为已跳过，且始终没有写入提示", async ({ page }) => {
    await startReview(page, "比较 AgentBench 与 CODESKILL 的侧重差异");
    await expect(page.getByRole("group", { name: "写回确认" })).toBeVisible({ timeout: 15000 });

    await page.getByRole("button", { name: "拒绝" }).click();

    await expect(page.locator(".timeline-step").nth(5).locator(".step-badge")).toHaveText(
      "已跳过",
    );
    await expect(page.getByRole("group", { name: "写回确认" })).toHaveCount(0);
    await expect(page.locator(".written-note")).toHaveCount(0);
  });

  test("批准写回：出现写入路径，确认控件随之消失", async ({ page }) => {
    await startReview(page, "比较 AgentBench 与 CODESKILL 的侧重差异");
    await expect(page.getByRole("group", { name: "写回确认" })).toBeVisible({ timeout: 15000 });

    await page.getByRole("button", { name: "批准写回" }).click();

    await expect(page.locator(".written-note")).toContainText(REVIEW_OUTPUT_PATH);
    await expect(page.locator(".timeline-step").nth(5).locator(".step-badge")).toHaveText(
      "已完成",
    );
    // 确认控件必须收起来，否则用户会以为还能再点一次——重复确认在后端是幂等的，
    // 但界面不该把它呈现成一个待办。
    await expect(page.getByRole("group", { name: "写回确认" })).toHaveCount(0);
  });

  test("刷新后按 run_id 接回同一条流（B1）", async ({ page }) => {
    const runId = await startReview(page, "比较 AgentBench 与 CODESKILL 的侧重差异");
    await expect(page.getByRole("group", { name: "写回确认" })).toBeVisible({ timeout: 15000 });

    await page.reload();

    // run_events 是唯一真相源，刷新只是重新折叠一遍历史，不该丢进度。
    await expect(page).toHaveURL(new RegExp(`run=${runId}`));
    await expect(page.locator(".timeline-step")).toHaveCount(6);
    await expect(page.locator(".preview-card .preview-body")).toHaveText(REVIEW_DRAFT);
    await expect(page.getByRole("group", { name: "写回确认" })).toBeVisible();
  });

  test("worker 失联自动恢复时，界面说明发生过什么", async ({ page }) => {
    await startReview(page, "比较两篇并演示恢复");

    // 界面上突然从头跑起来而不解释，看起来就像系统在重复劳动。
    await expect(page.locator(".recovery-note")).toContainText("worker 曾失联 1 次");
    await expect(page.locator(".recovery-note")).toContainText("已完成的步骤不会重跑");
    // 恢复通知是 run 级的，不能把任何一个步骤改成 recovering。
    await expect(page.locator(".timeline-step").nth(0).locator(".step-badge")).toHaveText(
      "已完成",
    );
  });
});
