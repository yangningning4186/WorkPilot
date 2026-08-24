import { expect, test, type Page } from "@playwright/test";

import { loginAsAdmin, MOCK_BASE, mockRequests, mockRuns, selectConfiguredProvider } from "./helpers";

async function openRunSettings(page: Page): Promise<void> {
  const settings = page.locator(".workdesk-run-settings");
  if (!(await settings.evaluate((element: HTMLDetailsElement) => element.open))) {
    await settings.locator("summary").click();
  }
  await expect(page.getByLabel("会话知识库")).toBeVisible();
}

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
    await expect(page.getByRole("link", { name: "办公工作台" })).toHaveCount(0);
    const permissionMenu = page.locator(".workdesk-permission-menu", { hasText: "默认权限" });
    await permissionMenu.locator("summary").click();
    await expect(permissionMenu).toContainText("Quarterly");
    await expect(permissionMenu).toContainText("读取文件");
    await expect(permissionMenu).toContainText("写入文件");
    await expect(permissionMenu).not.toContainText("编辑 Word");

    await page
      .getByLabel("你想让 Cowork 完成什么？")
      .fill("把季度汇报改成管理层语气，并保留原有数据");
    await page.getByRole("button", { name: /开始执行/ }).click();

    const activity = page.getByLabel("任务进度");
    await expect(activity.getByText("已完成", { exact: true })).toBeVisible();
    await expect(activity.locator("time")).toHaveText(/\d+(?:s|m)/);
    await expect(activity.getByText("执行过程", { exact: true })).toBeVisible();
    await expect(activity.getByText("列出文件")).toBeVisible();
    await expect(activity.getByText("加载格式 Skill")).toBeVisible();
    await expect(activity.getByText("执行 Shell 命令")).toBeVisible();
    await expect(activity.getByText("查看 *.docx", { exact: true })).toBeVisible();
    await expect(activity.getByText("office-deliverable", { exact: true })).toBeVisible();
    await expect(activity.getByText("渲染并验证 Word 交付物", { exact: true })).toBeVisible();
    await expect(activity.getByText("python render_docx.py 季度汇报.docx", { exact: true })).toBeVisible();
    await expect(page.locator(".workdesk-run-answer")).toHaveText(
      "已将季度汇报改为管理层语气，并保留原有数据。",
    );
    const artifactRail = page.getByLabel("Artifact 交付物");
    await expect(artifactRail).toBeVisible();
    await expect(artifactRail.getByText("季度汇报.docx", { exact: true }).first()).toBeVisible();
    await expect(artifactRail.getByText("已更新标题与结论段")).toBeVisible();
    await artifactRail.getByRole("tab", { name: /变更/ }).click();
    await expect(artifactRail.getByLabel("交付物差异")).toContainText("-季度总结");
    await expect(artifactRail.getByLabel("交付物差异")).toContainText("+管理层季度总结");
    await expect(page.locator(".workdesk-task-list").getByText("优化季度汇报管理层表达", { exact: true })).toBeVisible();
    await expect(page.getByRole("button", { name: /批准|确认应用/ })).toHaveCount(0);

    const calls = await mockRequests(request);
    expect(calls.some((item) => item.method === "POST" && item.path === "/api/v1/runs/cowork")).toBe(true);
    expect(calls.some((item) => item.path.endsWith("/events"))).toBe(true);
    expect(calls.some((item) => item.path.endsWith("/event-log"))).toBe(false);
    expect(calls.some((item) =>
      item.path.includes("/cowork/artifacts/") && item.path.endsWith("/preview")
    )).toBe(true);
    expect(calls.some((item) =>
      item.path.includes("/cowork/artifacts/") && item.path.endsWith("/diff")
    )).toBe(true);
  });

  test("输入区把低频配置收进运行设置，并随内容舒展", async ({ page }) => {
    await page.goto("/cowork");
    await loginAsAdmin(page);

    const composer = page.locator(".workdesk-composer");
    const input = page.getByLabel("你想让 Cowork 完成什么？");
    const actionRow = composer.locator(".workdesk-composer-actions");
    await expect(input).toBeVisible();
    await expect(actionRow.locator("select")).toHaveCount(0);
    await expect(actionRow.getByRole("button", { name: "开始执行任务" })).toBeVisible();

    const initialHeight = await input.evaluate((element) => element.clientHeight);
    expect(initialHeight).toBeGreaterThanOrEqual(130);
    await input.fill("第一行\n第二行\n第三行\n第四行\n第五行\n第六行\n第七行\n第八行");
    await expect.poll(() => input.evaluate((element) => element.clientHeight)).toBeGreaterThan(initialHeight);

    await composer.getByText("运行设置", { exact: true }).click();
    await expect(page.getByLabel("模型服务")).toBeVisible();
    await expect(page.getByLabel("执行角色")).toBeVisible();
    await expect(page.getByLabel("工作模式")).toBeVisible();
    await expect(page.getByLabel("会话知识库")).toBeVisible();
    await expect(page.getByRole("switch", { name: "先出计划再执行" })).toBeVisible();
    await expect(composer.getByText("不会扩大目录、能力或审批边界。", { exact: false })).toBeVisible();
  });

  test("选择本机原文件后输入区保持紧凑且操作栏仍可见", async ({ page }) => {
    await page.goto("/cowork");
    await loginAsAdmin(page);

    await page.evaluate(() => {
      Object.defineProperty(window, "isTauri", { configurable: true, value: true });
      Object.defineProperty(window, "__TAURI_INTERNALS__", {
        configurable: true,
        value: {
          invoke: async (command: string) => {
            if (command === "plugin:dialog|open") {
              return ["/Users/rance/workpilot/manual-test-kit/authorized/01_atlas_project_facts.txt"];
            }
            if (command === "desktop_context") {
              return { api_base: "", launch_token: "e2e-desktop-token" };
            }
            throw new Error(`unexpected command: ${command}`);
          },
        },
      });
    });

    // 任一局部状态变化都会让 useSyncExternalStore 重新读取桌面运行时快照。
    const input = page.getByLabel("你想让 Cowork 完成什么？");
    await input.fill("准备选择原文件");
    await expect(page.getByRole("button", { name: "选择本机工作文件" })).toBeVisible();
    await page.getByRole("button", { name: "选择本机工作文件" }).click();

    const composer = page.locator(".workdesk-composer");
    await expect(composer.getByText("01_atlas_project_facts.txt", { exact: true })).toBeVisible();
    await expect(composer.getByText("原文件 · 所在文件夹可读写", { exact: true })).toBeVisible();
    await expect(composer.locator(".workdesk-composer-actions")).toBeVisible();
    await expect(composer.locator(":scope > footer")).toBeVisible();

    const composerBox = await composer.boundingBox();
    const inputBox = await input.boundingBox();
    const layout = await composer.evaluate((element) => {
      const rect = element.getBoundingClientRect();
      const style = window.getComputedStyle(element);
      return {
        composer: { height: rect.height, cssHeight: style.height, display: style.display, flex: style.flex },
        children: Array.from(element.children).map((child) => {
          const childRect = child.getBoundingClientRect();
          const childStyle = window.getComputedStyle(child);
          return {
            className: child.className,
            height: childRect.height,
            cssHeight: childStyle.height,
            flex: childStyle.flex,
            position: childStyle.position,
          };
        }),
      };
    });
    expect(composerBox).not.toBeNull();
    expect(inputBox).not.toBeNull();
    if (composerBox !== null && inputBox !== null) {
      expect(composerBox.height, JSON.stringify(layout, null, 2)).toBeLessThan(380);
      expect(inputBox.height).toBeGreaterThanOrEqual(110);
      expect(inputBox.height).toBeLessThanOrEqual(242);
      expect(composerBox.y + composerBox.height).toBeLessThanOrEqual(900);
    }
  });

  test("空白页首轮先创建会话并等待知识库挂载，再创建 run", async ({ page, request }) => {
    await page.goto("/cowork?new=1");
    await loginAsAdmin(page);
    await expect(page.getByRole("heading", { name: "WorkPilot，我帮你" })).toBeVisible();
    await expect(page.getByLabel("正在计算上下文用量")).toHaveCount(0);

    await openRunSettings(page);
    await selectConfiguredProvider(page);
    const knowledgeBase = page.getByLabel("会话知识库");
    await expect(knowledgeBase.locator('option[value="papers"]')).toHaveCount(1);
    await knowledgeBase.selectOption("papers");
    await expect(knowledgeBase).toHaveValue("papers");

    await page.getByLabel("你想让 Cowork 完成什么？").fill("用论文资料库回答第一轮问题");
    await page.getByRole("button", { name: "开始执行任务" }).click();
    await expect(page.getByLabel("任务进度").getByText("已完成", { exact: true })).toBeVisible();

    const calls = await mockRequests(request);
    const createConversationAt = calls.findIndex((item) =>
      item.method === "POST" && item.path === "/api/v1/conversations"
    );
    const mountAt = calls.findIndex((item) =>
      item.method === "PUT" && item.path.endsWith("/knowledge-base")
    );
    const createRunAt = calls.findIndex((item) =>
      item.method === "POST" && item.path === "/api/v1/runs/cowork"
    );
    expect(createConversationAt).toBeGreaterThanOrEqual(0);
    expect(mountAt).toBeGreaterThan(createConversationAt);
    expect(createRunAt).toBeGreaterThan(mountAt);

    const runs = await mockRuns(request);
    expect(runs).toHaveLength(1);
    expect(runs[0]?.kb_slug).toBe("papers");
  });

  test("知识库 draft 与持久化挂载不会串到其他会话", async ({ page, request }) => {
    await page.goto("/cowork?new=1");
    await loginAsAdmin(page);
    await openRunSettings(page);
    await selectConfiguredProvider(page);

    const knowledgeBase = page.getByLabel("会话知识库");
    await expect(knowledgeBase.locator('option[value="papers"]')).toHaveCount(1);
    await knowledgeBase.selectOption("papers");
    await page.getByLabel("你想让 Cowork 完成什么？").fill("第一段会话");
    await page.getByRole("button", { name: "开始执行任务" }).click();
    await expect(page.getByLabel("任务进度").getByText("已完成", { exact: true })).toBeVisible();
    const firstRun = (await mockRuns(request))[0];
    expect(firstRun?.kb_slug).toBe("papers");

    await page.getByRole("button", { name: "新建任务" }).click();
    await expect(page.getByRole("heading", { name: "WorkPilot，我帮你" })).toBeVisible();
    await openRunSettings(page);
    await selectConfiguredProvider(page);
    await page.getByLabel("会话知识库").selectOption("agent-research");
    await page.getByLabel("你想让 Cowork 完成什么？").fill("第二段会话");
    await page.getByRole("button", { name: "开始执行任务" }).click();
    await expect(page.getByLabel("任务进度").getByText("已完成", { exact: true })).toBeVisible();
    const allRuns = await mockRuns(request);
    expect(allRuns).toHaveLength(2);
    expect(allRuns[1]?.kb_slug).toBe("agent-research");
    expect(allRuns[1]?.conversation_id).not.toBe(firstRun?.conversation_id);

    await page.goto(`/cowork?conversation=${firstRun?.conversation_id ?? ""}`);
    await openRunSettings(page);
    await expect(page.getByLabel("会话知识库")).toHaveValue("papers");

    await page.goto(`/cowork?conversation=${allRuns[1]?.conversation_id ?? ""}`);
    await openRunSettings(page);
    await expect(page.getByLabel("会话知识库")).toHaveValue("agent-research");
  });

  test("运行中修改知识库只影响下一轮 run", async ({ page, request }) => {
    await page.goto("/cowork?new=1");
    await loginAsAdmin(page);
    await openRunSettings(page);
    await selectConfiguredProvider(page);
    const knowledgeBase = page.getByLabel("会话知识库");
    await expect(knowledgeBase.locator('option[value="papers"]')).toHaveCount(1);
    await knowledgeBase.selectOption("papers");

    await page.getByLabel("你想让 Cowork 完成什么？").fill("保持运行直到我停止");
    await page.getByRole("button", { name: "开始执行任务" }).click();
    const stop = page.getByRole("button", { name: "停止 Cowork 任务" });
    await expect(stop).toBeVisible();

    await expect(knowledgeBase).toBeEnabled();
    await knowledgeBase.selectOption("agent-research");
    await expect.poll(async () => (
      await mockRequests(request)
    ).filter((item) => item.method === "PUT" && item.path.endsWith("/knowledge-base")).length)
      .toBeGreaterThanOrEqual(2);
    const firstRun = (await mockRuns(request))[0];
    expect(firstRun?.kb_slug).toBe("papers");

    await stop.click();
    await expect(page.getByText("已停止", { exact: true })).toBeVisible();
    await page.getByLabel("你想让 Cowork 完成什么？").fill("下一轮使用新知识库");
    await page.getByRole("button", { name: "开始执行任务" }).click();
    await expect(page.getByLabel("任务进度").getByText("已完成", { exact: true })).toBeVisible();

    const runs = await mockRuns(request);
    expect(runs).toHaveLength(2);
    expect(runs.map((item) => item.kb_slug)).toEqual(["papers", "agent-research"]);
  });

  test("空白会话把基础上下文明确标为预计占用", async ({ page }) => {
    await page.goto("/cowork");
    await loginAsAdmin(page);

    await page.getByLabel(/预计上下文占用/).click();
    await expect(page.getByText("上下文占用估算", { exact: true })).toBeVisible();
    await expect(page.getByText("提交后预计占用 37.4K / 102K", { exact: true })).toBeVisible();
    await expect(page.getByText("尚未调用模型；这里展示系统提示词、基础工具 Schema 与紧凑扩展目录的预计开销。", { exact: true })).toBeVisible();
    await expect(page.getByText("约 79.2K 时自动压缩", { exact: true })).toBeVisible();
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
    await expect(page.getByText("已停止", { exact: true })).toBeVisible();
    await expect(page.getByText("Cowork 任务已停止。已完成的文件修改会保留。", { exact: true })).toBeVisible();
    await expect(page.getByRole("button", { name: /停止 Cowork 任务/ })).toHaveCount(0);

    const calls = await mockRequests(request);
    expect(calls.some((item) => item.method === "POST" && item.path.endsWith("/cancel"))).toBe(true);
  });

  test("切走会话不会取消后台任务，切回后从事件流恢复", async ({ page, request }) => {
    await page.goto("/cowork");
    await loginAsAdmin(page);

    await page.getByLabel("你想让 Cowork 完成什么？").fill("保持运行直到我停止");
    await page.getByRole("button", { name: /开始执行/ }).click();
    await expect(page.getByRole("button", { name: "停止 Cowork 任务" })).toBeVisible();

    const newTask = page.getByRole("button", { name: "新建任务" });
    await expect(newTask).toBeEnabled();
    await newTask.click();
    const backgroundRow = page.locator(".workdesk-task-row", {
      hasText: "优化季度汇报管理层表达",
    });
    await expect(backgroundRow.getByText("执行中", { exact: true })).toBeVisible();

    await backgroundRow.locator(".workdesk-task-select").click();
    await expect(page.getByRole("button", { name: "停止 Cowork 任务" })).toBeVisible();

    const eventStreams = (await mockRequests(request)).filter((item) => item.path.endsWith("/events"));
    expect(eventStreams.length).toBeGreaterThanOrEqual(2);
  });

  test("Cowork SSE 断线后用 event-log 补齐并从游标续传", async ({ page, request }) => {
    await page.goto("/cowork");
    await loginAsAdmin(page);

    await page.getByLabel("你想让 Cowork 完成什么？").fill("验证断线续传");
    await page.getByRole("button", { name: /开始执行/ }).click();
    await expect(page.locator(".workdesk-run-answer")).toHaveText(
      "已将季度汇报改为管理层语气，并保留原有数据。",
    );
    await expect(page.getByLabel("任务进度").getByText("已完成", { exact: true })).toBeVisible();

    const calls = await mockRequests(request);
    const streams = calls.filter((item) => item.path.endsWith("/events"));
    expect(streams.length).toBeGreaterThanOrEqual(2);
    expect(streams.some((item) => item.search.includes("after_seq=4"))).toBe(true);
    expect(calls.some((item) => item.path.endsWith("/event-log"))).toBe(true);
  });

  test("论文阅读器渲染出可选中的文本层", async ({ page, request }) => {
    // 文本层是这条验收的全部意义：它之前贴的是后端渲染的 PNG，用户选不中、复制不了，
    // 也没法把"这一段"交给模型——阅读器因此只能是单向的。
    await page.goto("/cowork");
    await loginAsAdmin(page);
    await page.getByRole("tab", { name: "论文阅读" }).click();
    await page
      .getByPlaceholder("要读的文档，例如 papers/attention.pdf")
      .fill("/Users/demo/Documents/Quarterly/paper.pdf");
    await page.getByLabel("你想让 Cowork 完成什么？").fill("总结这篇论文");
    await page.getByRole("button", { name: /开始执行/ }).click();

    const reader = page.getByLabel("阅读器");
    await expect(reader.getByText("paper.pdf", { exact: true })).toBeVisible();

    // 画布画出了东西，文本层摆出了 span：两件事都得成立，缺一个就是"看得见但选不中"
    // 或者"选得中但一片空白"。
    const canvas = reader.locator("canvas.reader-canvas");
    await expect
      .poll(() => canvas.evaluate((node: HTMLCanvasElement) => node.width))
      .toBeGreaterThan(0);
    await expect(reader.locator(".textLayer span").first()).toBeAttached();
    await expect
      .poll(() => reader.locator(".textLayer").innerText())
      .toContain("Attention");

    const calls = await mockRequests(request);
    expect(calls.some((item) => item.path.endsWith("/reading/file"))).toBe(true);
  });
});
