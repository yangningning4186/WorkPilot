import { expect, test, type Page } from "@playwright/test";

type ConnectorAccountFixture = {
  id: string;
  kind: string;
  name: string;
  auth_type: "oauth2" | "token";
  status: "configured" | "authorizing" | "connected";
  enabled: boolean;
};

const catalog = [
  {
    kind: "feishu",
    label: "飞书",
    blurb: "覆盖日历、云文档、云盘、多维表格、任务与审批的中国办公主连接器。",
    logo: "feishu",
    brand_color: "#3370ff",
    category: "china_office",
    auth_types: ["oauth2", "token"],
    default_scopes: ["offline_access", "calendar:calendar", "bitable:app"],
    capabilities: ["openapi", "calendar", "base", "docs", "drive", "tasks", "approval"],
  },
  {
    kind: "github",
    label: "GitHub",
    blurb: "连接仓库、Issue 与 Pull Request，用于代码协作、检索和自动化交付。",
    logo: "github",
    brand_color: "#24292f",
    category: "developer",
    auth_types: ["oauth2", "token"],
    default_scopes: ["read:user", "repo"],
    capabilities: ["openapi"],
  },
];

function accountFixture(value: ConnectorAccountFixture) {
  return {
    config: {},
    scopes: ["offline_access"],
    capabilities: ["openapi"],
    external_account_id: null,
    external_account_name: null,
    expires_at: null,
    last_checked_at: null,
    last_error: null,
    has_secrets: true,
    created_at: "2026-08-22T08:00:00Z",
    updated_at: "2026-08-22T08:00:00Z",
    ...value,
  };
}

async function mockConnectorApis(
  page: Page,
  accounts: ReturnType<typeof accountFixture>[] = [],
) {
  await page.route("**/api/v1/**", async (route) => {
    const url = new URL(route.request().url());
    if (url.pathname === "/api/v1/auth/admin/session") {
      await route.fulfill({ json: { authenticated: true }, status: 200 });
      return;
    }
    if (url.pathname === "/api/v1/conversations") {
      await route.fulfill({ json: { items: [], total: 0 }, status: 200 });
      return;
    }
    if (url.pathname === "/api/v1/connectors/catalog") {
      await route.fulfill({ json: { items: catalog }, status: 200 });
      return;
    }
    if (url.pathname === "/api/v1/connectors") {
      await route.fulfill({ json: { items: accounts }, status: 200 });
      return;
    }
    if (url.pathname.endsWith("/oauth/start")) {
      await route.fulfill({
        json: {
          authorization_url: "https://accounts.feishu.cn/open-apis/authen/v1/authorize?state=test-state",
          state: "test-state",
          expires_at: "2026-08-22T08:10:00Z",
        },
        status: 200,
      });
      return;
    }
    if (url.pathname === "/api/v1/integrations/mcp") {
      await route.fulfill({ json: { source_path: "/tmp/mcp.yaml", servers: [] }, status: 200 });
      return;
    }
    await route.fulfill({ json: { detail: "not mocked" }, status: 404 });
  });
}

test.describe("统一连接器目录", () => {
  test.beforeEach(async ({ page }) => {
    await mockConnectorApis(page);
  });

  test("官方连接器与 Custom MCP 在同一入口，连接弹窗保留双通道", async ({ page }) => {
    await page.goto("/connectors");

    await expect(page.getByRole("heading", { name: "连接你的工作" })).toBeVisible();
    await expect(page.getByRole("heading", { name: "Custom · MCP" })).toBeVisible();
    await expect(page.getByRole("link", { name: /自定义 MCP 服务/ })).toBeVisible();
    await expect(page.getByRole("heading", { name: "飞书", level: 3 })).toBeVisible();

    await page.getByRole("button", { name: "授权 飞书" }).click();
    const dialog = page.getByRole("dialog", { name: "飞书" });
    await expect(dialog).toBeVisible();
    await expect(dialog.getByRole("tab", { name: "一键授权" })).toBeVisible();
    await expect(dialog.getByRole("tab", { name: "手动配置" })).toHaveAttribute("aria-selected", "true");
    await expect(dialog.getByLabel("显示名称")).toHaveValue("飞书");

    await dialog.getByRole("tab", { name: "一键授权" }).click();
    await expect(dialog.getByRole("heading", { name: "首次连接需要登记应用" })).toBeVisible();
    await expect(dialog.getByText(/不托管厂商 Client Secret/)).toBeVisible();
  });

  test("已登记的单账户从目录卡片直接跳到官方授权页", async ({ page }) => {
    await page.unrouteAll();
    await mockConnectorApis(page, [accountFixture({
      id: "0198d987-8f00-7000-8000-000000000001",
      kind: "feishu",
      name: "飞书主账号",
      auth_type: "oauth2",
      status: "configured",
      enabled: true,
    })]);
    await page.goto("/connectors");

    const popupPromise = page.waitForEvent("popup");
    await page.getByRole("button", { name: "授权 飞书" }).click();
    const popup = await popupPromise;

    await expect.poll(() => popup.url()).toContain("accounts.feishu.cn/open-apis/authen/v1/authorize");
    await expect(page.getByRole("status")).toContainText("等待浏览器授权");
    await expect(page.getByRole("dialog")).toHaveCount(0);
    await popup.close();
  });

  test("窄屏目录不横向溢出", async ({ page }) => {
    await page.setViewportSize({ width: 375, height: 812 });
    await page.goto("/connectors");
    await expect(page.getByRole("heading", { name: "连接你的工作" })).toBeVisible();
    const sizes = await page.evaluate(() => ({
      document: document.documentElement.scrollWidth,
      viewport: window.innerWidth,
    }));
    expect(sizes.document).toBeLessThanOrEqual(sizes.viewport);
  });
});
