import { expect, test } from "@playwright/test";

import {
  answerCopy,
  ask,
  citationCards,
  loginAsAdmin,
  MOCK_BASE,
  mockRequests,
  selectConfiguredProvider,
} from "./helpers";

/** Cowork 的实时流、补历史和失败态都必须由同一份 run events 驱动。 */
test.describe("Cowork 流式恢复与失败态", () => {
  test.beforeEach(async ({ request }) => {
    const response = await request.post(`${MOCK_BASE}/__reset`);
    expect(response.ok()).toBe(true);
  });

  test("桌面 SSE 使用 sidecar API base 并携带 launch header", async ({ page }) => {
    const appPort = Number(process.env.E2E_APP_PORT ?? 3100);
    const sidecarBase = `http://127.0.0.1:${appPort}/desktop-sidecar`;
    const launchToken = "desktop-launch-token-test";
    const runId = "7d0e4c55-0000-4d00-8000-000000000001";
    const conversationId = "7d0e4c55-0000-4d00-8000-000000000002";
    let eventRequest: { url: string; launchToken: string | undefined } | null = null;

    await page.addInitScript(
      ({ apiBase, token }) => {
        Object.defineProperty(window, "isTauri", { configurable: true, value: true });
        Object.defineProperty(window, "__TAURI_INTERNALS__", {
          configurable: true,
          value: {
            invoke: async (command: string) => {
              if (command !== "desktop_context") throw new Error(`unexpected command: ${command}`);
              return { api_base: apiBase, launch_token: token };
            },
          },
        });
      },
      { apiBase: sidecarBase, token: launchToken },
    );
    await page.route(`${sidecarBase}/**`, async (route) => {
      const request = route.request();
      const url = new URL(request.url());
      if (url.pathname.endsWith(`/runs/${runId}/events`)) {
        eventRequest = {
          url: request.url(),
          launchToken: request.headers()["x-workpilot-launch-token"],
        };
        const envelopes = [
          { seq: "1", type: "plan", created_at: "2026-08-23T02:00:00Z", data: { workflow_type: "cowork", tools: [] } },
          { seq: "2", type: "message.delta", created_at: "2026-08-23T02:00:01Z", data: { text: "桌面事件流已连接。" } },
          { seq: "3", type: "run.done", created_at: "2026-08-23T02:00:02Z", data: { workflow_type: "cowork", status: "done" } },
        ];
        const body = envelopes
          .map(
            (event) =>
              `id: ${runId}:${event.seq}\nevent: ${event.type}\ndata: ${JSON.stringify({ id: `${runId}:${event.seq}`, run_id: runId, ...event })}\n\n`,
          )
          .join("");
        await route.fulfill({ body, contentType: "text/event-stream; charset=utf-8", status: 200 });
        return;
      }
      if (url.pathname.endsWith("/conversations")) {
        const items = url.searchParams.get("archived") === "true"
          ? []
          : [{
              id: conversationId,
              title: "桌面连接测试",
              active_run_id: runId,
              message_count: 1,
              latest_message: "桌面连接测试",
              last_message_at: "2026-08-23T02:00:00Z",
              provider_profile_id: null,
              provider_name: null,
              provider: null,
              selected_model: null,
              unattended: false,
              approval_mode: "interactive",
              persona_name: "general",
              archived_at: null,
              created_at: "2026-08-23T02:00:00Z",
              updated_at: "2026-08-23T02:00:00Z",
            }];
        await route.fulfill({ json: { items, total: items.length }, status: 200 });
        return;
      }
      if (url.pathname.endsWith("/providers")) {
        await route.fulfill({ json: { items: [] }, status: 200 });
        return;
      }
      if (url.pathname.endsWith("/auth/admin/session")) {
        await route.fulfill({ json: { authenticated: true }, status: 200 });
        return;
      }
      await route.fulfill({ json: { detail: "not mocked" }, status: 404 });
    });

    await page.goto(`/cowork?conversation=${conversationId}`);
    await expect(answerCopy(page)).toHaveText("桌面事件流已连接。");
    expect(eventRequest).toEqual({
      url: `${sidecarBase}/api/v1/runs/${runId}/events?after_seq=0`,
      launchToken,
    });
  });

  test("刷新恢复：刷新后正文逐字一致，且不会重新创建 run", async ({ page, request }) => {
    await ask(page, "混合检索为什么比单路召回更稳定？");
    await expect(citationCards(page)).toHaveCount(2);
    const before = await answerCopy(page).innerText();

    await page.reload();

    await expect(citationCards(page)).toHaveCount(2);
    await expect(answerCopy(page)).toHaveText(before);
    const calls = await mockRequests(request);
    expect(calls.filter((item) => item.method === "POST" && item.path === "/api/v1/runs/cowork")).toHaveLength(1);
  });

  test("断线续传：重连后从断点续上，正文不重复不缺段", async ({ page, request }) => {
    const runId = await ask(page, "这一题会在中途断线");

    await expect(answerCopy(page)).toHaveText("断线前的前半句，断线后续上的后半句，以及结尾 S1。");
    await expect(citationCards(page)).toHaveCount(1);

    const calls = await mockRequests(request);
    const streams = calls.filter((item) => item.path === `/api/v1/runs/${runId}/events`);
    expect(streams.length).toBeGreaterThanOrEqual(2);
    expect(calls.some((item) => item.path === `/api/v1/runs/${runId}/event-log`)).toBe(true);
  });

  test("重连重放：服务端重发看过的事件，前端按 seq 去重", async ({ page }) => {
    await ask(page, "这一题重连后会重放全部事件");

    await expect(answerCopy(page)).toHaveText("重放前的前半句，重放后补上的后半句，以及结尾 S1。");
    await expect(citationCards(page)).toHaveCount(1);
  });

  test("取消：停止按钮落到 cancel 接口，页面进入明确终态", async ({ page, request }) => {
    const runId = await ask(page, "保持运行直到我停止");
    const stop = page.getByRole("button", { name: "停止 Cowork 任务" });
    await expect(stop).toBeVisible();
    await stop.click();

    await expect(page.locator(".workdesk-run-answer.cancelled")).toContainText(
      "Cowork 任务已停止。已完成的文件修改会保留。",
    );
    await expect(stop).toHaveCount(0);
    const calls = await mockRequests(request);
    expect(calls.some((item) => item.path === `/api/v1/runs/${runId}/cancel`)).toBe(true);
  });

  test("失败态：可读错误不会覆盖已经生成的半截正文", async ({ page }) => {
    await ask(page, "这一题会报错");

    const error = page.locator(".workdesk-run-answer.error");
    await expect(error.getByRole("alert")).toContainText("本次回答超出了每日费用上限");
    await expect(error.locator(".answer-copy")).toContainText("正在整理证据");
    await expect(page.getByRole("button", { name: "停止 Cowork 任务" })).toHaveCount(0);
  });

  test("关掉页面再回来：凭 conversation URL 拿回完整答案", async ({ page, context }) => {
    await ask(page, "混合检索为什么比单路召回更稳定？");
    await expect(answerCopy(page)).toContainText("0.62 提到 0.81");
    const conversationUrl = page.url();
    expect(conversationUrl).toContain("conversation=");
    await page.close();

    const reopened = await context.newPage();
    await reopened.goto(conversationUrl);
    await expect(answerCopy(reopened)).toContainText("0.62 提到 0.81");
    await expect(citationCards(reopened)).toHaveCount(2);
    await reopened.close();
  });

  test("并发隔离：两个标签页的回答互不污染", async ({ page, context }) => {
    const second = await context.newPage();
    await ask(page, "混合检索为什么比单路召回更稳定？");
    await ask(second, "markdown 笔记里英文集的结论是什么？");

    await expect(answerCopy(page)).toContainText("0.62 提到 0.81");
    await expect(answerCopy(second)).toContainText("融合后增量为零");
    await expect(answerCopy(page)).not.toContainText("融合后增量为零");
    await expect(answerCopy(second)).not.toContainText("0.62 提到 0.81");
    await expect(citationCards(second)).toHaveCount(1);
    await second.close();
  });

  test("创建 run 失败：给出可读错误，输入区可以立即重试", async ({ page }) => {
    await page.route("**/api/v1/runs/cowork", (route) =>
      route.fulfill({ status: 503, body: "backend unavailable" }),
    );
    await page.goto("/cowork?new=1");
    await loginAsAdmin(page);
    await selectConfiguredProvider(page);
    const input = page.getByLabel("你想让 Cowork 完成什么？");
    await input.fill("后端挂了的时候会怎样？");
    await page.getByRole("button", { name: "开始执行任务" }).click();

    await expect(page.getByRole("status")).toContainText(
      "Cowork 依赖服务尚未就绪，请检查 sidecar、PostgreSQL 与 Redis。",
    );
    await expect(page.locator(".workdesk-run-answer")).toHaveCount(0);
    await input.fill("再试一次");
    await expect(page.getByRole("button", { name: "开始执行任务" })).toBeEnabled();
  });
});
