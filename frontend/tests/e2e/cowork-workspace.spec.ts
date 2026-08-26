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
    const railWidthBeforeResize = await artifactRail.evaluate((element) => element.getBoundingClientRect().width);
    const resizer = page.getByRole("separator", { name: "调整右侧预览宽度" });
    await expect(resizer).toBeVisible();
    const resizerBox = await resizer.boundingBox();
    expect(resizerBox).not.toBeNull();
    if (resizerBox !== null) {
      await page.mouse.move(resizerBox.x + resizerBox.width / 2, resizerBox.y + 180);
      await page.mouse.down();
      await page.mouse.move(resizerBox.x - 100, resizerBox.y + 180, { steps: 5 });
      await page.mouse.up();
    }
    await expect.poll(
      () => artifactRail.evaluate((element) => element.getBoundingClientRect().width),
    ).toBeGreaterThan(railWidthBeforeResize + 70);
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

  test("Agent Team 展示返工次数、拒绝原因和部分完成终态", async ({ page }) => {
    await page.goto("/cowork");
    await loginAsAdmin(page);

    await page
      .getByLabel("你想让 Cowork 完成什么？")
      .fill("展示 Agent Team 部分完成");
    await page.getByRole("button", { name: /开始执行/ }).click();

    const activity = page.getByLabel("任务进度");
    await expect(activity.locator(".workdesk-run-process-state")).toContainText("部分完成");
    const team = page.getByLabel("Agent Team 状态");
    await expect(team).toBeVisible();
    await expect(team).toContainText("1/2 已收束");
    await expect(team).toContainText("architecture");
    await expect(team).toContainText("testing");
    await expect(team).toContainText("已返工 2 次");
    await expect(team).toContainText("待返工");
    await expect(team).toContainText("已完成");

    const architecture = team.locator(".workdesk-team-task", { hasText: "architecture" });
    await architecture.getByText("最近拒绝原因", { exact: true }).click();
    await expect(architecture).toContainText("仍缺少主要脚本的模块边界证据。");
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

  test("运行授权固定在底部输入框中处理", async ({ page }) => {
    await page.goto("/cowork?new=1");
    await loginAsAdmin(page);
    await selectConfiguredProvider(page);

    await page.getByLabel("你想让 Cowork 完成什么？").fill("请求目录授权");
    await page.getByRole("button", { name: "开始执行任务" }).click();

    const composer = page.locator(".workdesk-composer");
    const authorization = composer.locator(".workdesk-composer-interaction");
    await expect(authorization).toBeVisible();
    await expect(authorization).toContainText("允许我使用另一个目录？");
    await expect(authorization).toContainText("读取与写入");
    await expect(page.locator(".workdesk-run-message .workdesk-composer-interaction")).toHaveCount(0);
    await expect(page.getByLabel("你想让 Cowork 完成什么？")).toBeDisabled();
    await expect(page.getByPlaceholder("请先处理输入框中的确认请求")).toBeVisible();

    const composerBox = await composer.boundingBox();
    const authorizationBox = await authorization.boundingBox();
    expect(composerBox).not.toBeNull();
    expect(authorizationBox).not.toBeNull();
    if (composerBox !== null && authorizationBox !== null) {
      expect(authorizationBox.y).toBeGreaterThanOrEqual(composerBox.y);
      expect(authorizationBox.y + authorizationBox.height).toBeLessThanOrEqual(
        composerBox.y + composerBox.height + 1,
      );
    }
  });

  test("首轮选择真正的工作空间，并在创建 run 前绑定为会话主目录", async ({ page, request }) => {
    await page.addInitScript(() => {
      Object.defineProperty(window, "isTauri", { configurable: true, value: true });
      Object.defineProperty(window, "__TAURI_INTERNALS__", {
        configurable: true,
        value: {
          invoke: async (command: string) => {
            if (command === "plugin:dialog|open") {
              return "/Users/rance/workpilot/manual-test-kit/authorized";
            }
            if (command === "desktop_context") {
              return { api_base: "", launch_token: "e2e-desktop-token" };
            }
            throw new Error(`unexpected command: ${command}`);
          },
        },
      });
    });
    await page.goto("/cowork?new=1");
    await expect(page.locator(".admin-badge")).toHaveText("desktop owner");
    await expect(page.getByRole("heading", { name: "WorkPilot，我帮你" })).toBeVisible();

    await selectConfiguredProvider(page);
    await page.locator(".workdesk-run-settings > summary").click();
    await expect(page.getByLabel("模型服务")).toBeHidden();
    const input = page.getByLabel("你想让 Cowork 完成什么？");
    await input.fill("在选定工作空间里整理项目资料");
    await expect(page.getByRole("button", { name: "选择工作空间" })).toBeVisible();
    await page.getByRole("button", { name: "选择工作空间" }).click();

    const workspaceSetup = page.getByLabel("会话工作空间");
    const composer = page.locator(".workdesk-composer");
    await expect(composer.locator(".workdesk-session-setup")).toHaveCount(1);
    await expect(workspaceSetup.getByText("authorized", { exact: true })).toBeVisible();
    await expect(workspaceSetup).toContainText("/Users/rance/workpilot/manual-test-kit/authorized");
    await expect(page.getByRole("button", { name: "恢复默认工作空间" })).toBeVisible();
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

    await page.getByRole("button", { name: "开始执行任务" }).click();
    await expect(page.getByLabel("任务进度").getByText("已完成", { exact: true })).toBeVisible();
    await expect(page.locator(".workdesk-topline")).toContainText("authorized");
    await expect(page.getByLabel("会话工作空间")).toHaveCount(0);

    const calls = await mockRequests(request);
    const createRootAt = calls.findIndex((item) =>
      item.method === "POST" && /\/cowork\/sessions\/[^/]+\/roots$/.test(item.path)
    );
    const createRunAt = calls.findIndex((item) =>
      item.method === "POST" && item.path === "/api/v1/runs/cowork"
    );
    expect(createRootAt).toBeGreaterThanOrEqual(0);
    expect(createRunAt).toBeGreaterThan(createRootAt);
    const runs = await mockRuns(request);
    expect(runs[0]?.workspace_path).toBe("/Users/rance/workpilot/manual-test-kit/authorized");
    expect(runs[0]?.workspace_files).toBeNull();
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

    // 发送任务会自动收起配置浮层，避免覆盖运行输出；需要调整下一轮配置时再主动打开。
    await openRunSettings(page);
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

    const reader = page.getByRole("complementary", { name: "阅读器" });
    await expect(reader.getByText("paper.pdf", { exact: true })).toBeVisible();

    // 画布画出了东西，文本层摆出了 span：两件事都得成立，缺一个就是"看得见但选不中"
    // 或者"选得中但一片空白"。
    const canvas = reader.locator("canvas.reader-canvas");
    await expect
      .poll(() => canvas.evaluate((node: HTMLCanvasElement) => node.width))
      .toBeGreaterThan(0);
    await expect(canvas).toHaveAttribute("data-render-state", "ready");
    await expect(reader.locator(".textLayer span").first()).toBeAttached();
    await expect
      .poll(() => reader.locator(".textLayer").innerText())
      .toContain("Attention");

    // macOS/Tauri 的 WebView 在窗口切出后可能回收 canvas 像素，但 DOM 与 pdf.js document
    // 仍然存在。模拟这条路径：先清空画布，再让窗口恢复焦点，当前页必须自动重绘。
    const paintedColour = () =>
      canvas.evaluate((node: HTMLCanvasElement) => {
        const context = node.getContext("2d");
        if (context === null || node.width === 0 || node.height === 0) return 0;
        const pixels = context.getImageData(0, 0, node.width, node.height).data;
        const pixelStride = Math.max(1, Math.floor(pixels.length / 4 / 400));
        let colour = 0;
        for (let pixel = 0; pixel < pixels.length / 4; pixel += pixelStride) {
          const index = pixel * 4;
          colour += (pixels[index] ?? 0) + (pixels[index + 1] ?? 0) + (pixels[index + 2] ?? 0);
        }
        return colour;
      });
    await expect.poll(paintedColour).toBeGreaterThan(0);
    const colourImmediatelyAfterDiscard = await canvas.evaluate((node: HTMLCanvasElement) => {
      const context = node.getContext("2d");
      if (context === null) return -1;
      context.clearRect(0, 0, node.width, node.height);
      const pixel = context.getImageData(0, 0, 1, 1).data;
      const colour = (pixel[0] ?? 0) + (pixel[1] ?? 0) + (pixel[2] ?? 0);
      // 在同一个浏览器任务里先读取清空后的像素，再模拟 WebView 恢复可见；避免测试运行器
      // 自己的窗口焦点事件提前触发重绘，把“已回收”状态覆盖掉。
      document.dispatchEvent(new Event("visibilitychange"));
      return colour;
    });
    expect(colourImmediatelyAfterDiscard).toBe(0);
    await expect.poll(paintedColour).toBeGreaterThan(0);

    // 侧栏页面是独立 Next.js 路由，会卸载整个 Cowork 页面。返回同一会话后不但要重新
    // 打开论文，也要回到离开前正在看的页，而不是悄悄退回办公模式或第 1 页。
    await reader.getByRole("button", { name: "下一页" }).click();
    await expect(reader).toContainText("第 2 / 2 页");
    await page.getByRole("link", { name: "知识库", exact: true }).click();
    await expect(page).toHaveURL(/\/knowledge$/);
    await page.locator(".workdesk-brand").click();
    await expect(page).toHaveURL(/\/cowork/);
    const restoredReader = page.getByRole("complementary", { name: "阅读器" });
    await expect(restoredReader.getByText("paper.pdf", { exact: true })).toBeVisible();
    await expect(restoredReader).toContainText("第 2 / 2 页");

    const calls = await mockRequests(request);
    expect(calls.some((item) => item.path.endsWith("/reading/file"))).toBe(true);
  });
});
