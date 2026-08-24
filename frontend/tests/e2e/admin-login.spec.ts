import { expect, test } from "@playwright/test";

import { ADMIN_PASSWORD, loginAsAdmin, mockRequests, setAdminConfigured } from "./helpers";

/**
 * admin 登录入口验收。
 *
 * 背景：记忆管理等 owner 写操作在后端全挂着 `require_admin_session`，
 * 浏览器必须有拿到 session 的入口。这组用例钉住三件事：
 * 入口存在且能用、失败原因说的是**下一步该做什么**、未登录时写操作不留假可点。
 */

test.describe("admin 登录入口", () => {
  test("未登录时记忆页说明缺什么，写操作不出现", async ({ page }) => {
    await page.goto("/memory");

    await expect(page.locator(".memory-gate")).toContainText("请使用右上角的 owner 登录");
    await expect(page.getByRole("button", { name: "手动添加" })).toHaveCount(0);
  });

  test("登录后写操作解锁，顶栏给出可见的已登录标识", async ({ page }) => {
    await page.goto("/memory");
    await loginAsAdmin(page);

    await expect(page.locator(".memory-gate")).toHaveCount(0);
    await expect(page.getByRole("button", { name: "手动添加" })).toBeVisible();
  });

  test("登录状态跨页面一致，且刷新后仍然有效", async ({ page }) => {
    await page.goto("/memory");
    await loginAsAdmin(page);

    // 状态存在 httpOnly cookie 里，换页面和刷新都不该掉——存内存就会掉。
    await page.goto("/cost");
    await expect(page.locator(".admin-badge")).toHaveText("owner");
    await page.reload();
    await expect(page.locator(".admin-badge")).toHaveText("owner");
  });

  test("密码错误说密码错误，且不谎称已登录", async ({ page }) => {
    await page.goto("/memory");
    await page.getByRole("button", { name: "owner 登录" }).click();
    await page.getByLabel("owner 口令").fill("wrong-password");
    await page.getByRole("button", { name: "登录", exact: true }).click();

    await expect(page.locator(".admin-login .form-error")).toHaveText("密码错误。");
    await expect(page.locator(".admin-badge")).toHaveCount(0);
    // 密码框清空重来，免得在一个已知错误的值上反复回车。
    await expect(page.getByLabel("owner 口令")).toHaveValue("");
    await expect(page.locator(".memory-gate")).toBeVisible();
  });

  test("后端没配口令时给的是配置指引，不是「密码错误」", async ({ page, request }) => {
    // 这是本组最要紧的一条：503 时任何密码都登不进去，
    // 提示"密码错误"会让人把时间全花在试密码上，而真正要改的是 .env。
    await setAdminConfigured(request, false);
    try {
      await page.goto("/memory");
      await page.getByRole("button", { name: "owner 登录" }).click();
      await page.getByLabel("owner 口令").fill(ADMIN_PASSWORD);
      await page.getByRole("button", { name: "登录", exact: true }).click();

      const error = page.locator(".admin-login .form-error");
      await expect(error).toContainText("DEMO_ADMIN_PASSWORD_HASH");
      await expect(error).toContainText("hash_admin_password");
      await expect(error).not.toContainText("密码错误");
    } finally {
      await setAdminConfigured(request, true);
    }
  });

  test("登出后写操作重新被挡住", async ({ page }) => {
    await page.goto("/memory");
    await loginAsAdmin(page);

    await page.getByRole("button", { name: "登出" }).click();

    await expect(page.locator(".admin-badge")).toHaveCount(0);
    await expect(page.getByRole("button", { name: "owner 登录" })).toBeVisible();
    await expect(page.locator(".memory-gate")).toBeVisible();
    await expect(page.getByRole("button", { name: "手动添加" })).toHaveCount(0);
  });

  test("登录浮层不把口令带进 URL", async ({ page, request }) => {
    await page.goto("/memory");
    await loginAsAdmin(page);

    // 口令只能在 POST body 里。落进 query string 就会进浏览器历史、代理日志和后端访问日志。
    expect(page.url()).not.toContain(ADMIN_PASSWORD);
    const log = await mockRequests(request);
    for (const item of log) {
      expect(item.search).not.toContain(ADMIN_PASSWORD);
    }
    expect(
      log.filter((item) => item.path === "/api/v1/auth/admin/login" && item.method === "POST")
        .length,
    ).toBeGreaterThan(0);
  });
});
