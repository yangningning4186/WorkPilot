import { expect, test } from "@playwright/test";

import { ADMIN_PASSWORD, loginAsAdmin, mockRequests, setAdminConfigured } from "./helpers";

/**
 * admin 登录入口验收。
 *
 * 背景：创建综述、批准写回、触发同步在后端全挂着 `require_admin_session`，
 * 但浏览器里一直没有拿到 session 的地方——只能 curl。这组用例钉住三件事：
 * 入口存在且能用、失败原因说的是**下一步该做什么**、未登录时写操作不留假可点。
 */

test.describe("admin 登录入口", () => {
  test("未登录时综述页说明缺什么，创建按钮开不了", async ({ page }) => {
    await page.goto("/review");

    await expect(page.locator(".login-required")).toContainText("需要先在右上角完成 admin 登录");

    // 表单填满也不该变成可点——写回有副作用，不能让人填完再吃一个 401。
    await page.getByRole("textbox", { name: "综述目标" }).fill("比较两篇的取舍");
    await page.locator(".doc-picker input[type=checkbox]").nth(0).check();
    await page.locator(".doc-picker input[type=checkbox]").nth(1).check();
    await expect(page.getByRole("button", { name: "开始生成综述" })).toBeDisabled();
  });

  test("登录后写操作解锁，顶栏给出可见的已登录标识", async ({ page }) => {
    await page.goto("/review");
    await loginAsAdmin(page);

    await expect(page.locator(".login-required")).toHaveCount(0);
    await page.getByRole("textbox", { name: "综述目标" }).fill("比较两篇的取舍");
    await page.locator(".doc-picker input[type=checkbox]").nth(0).check();
    await page.locator(".doc-picker input[type=checkbox]").nth(1).check();
    await expect(page.getByRole("button", { name: "开始生成综述" })).toBeEnabled();
  });

  test("登录状态跨页面一致，且刷新后仍然有效", async ({ page }) => {
    await page.goto("/library");
    await loginAsAdmin(page);

    // 状态存在 httpOnly cookie 里，换页面和刷新都不该掉——存内存就会掉。
    await page.goto("/review");
    await expect(page.locator(".admin-badge")).toHaveText("admin");
    await page.reload();
    await expect(page.locator(".admin-badge")).toHaveText("admin");
  });

  test("密码错误说密码错误，且不谎称已登录", async ({ page }) => {
    await page.goto("/review");
    await page.getByRole("button", { name: "admin 登录" }).click();
    await page.getByLabel("管理员密码").fill("wrong-password");
    await page.getByRole("button", { name: "登录", exact: true }).click();

    await expect(page.locator(".admin-login .form-error")).toHaveText("密码错误。");
    await expect(page.locator(".admin-badge")).toHaveCount(0);
    // 密码框清空重来，免得在一个已知错误的值上反复回车。
    await expect(page.getByLabel("管理员密码")).toHaveValue("");
    await expect(page.locator(".login-required")).toBeVisible();
  });

  test("后端没配口令时给的是配置指引，不是「密码错误」", async ({ page, request }) => {
    // 这是本组最要紧的一条：503 时任何密码都登不进去，
    // 提示"密码错误"会让人把时间全花在试密码上，而真正要改的是 .env。
    await setAdminConfigured(request, false);
    try {
      await page.goto("/review");
      await page.getByRole("button", { name: "admin 登录" }).click();
      await page.getByLabel("管理员密码").fill(ADMIN_PASSWORD);
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
    await page.goto("/review");
    await loginAsAdmin(page);

    await page.getByRole("button", { name: "登出" }).click();

    await expect(page.locator(".admin-badge")).toHaveCount(0);
    await expect(page.getByRole("button", { name: "admin 登录" })).toBeVisible();
    await expect(page.locator(".login-required")).toBeVisible();

    await page.getByRole("textbox", { name: "综述目标" }).fill("比较两篇的取舍");
    await page.locator(".doc-picker input[type=checkbox]").nth(0).check();
    await page.locator(".doc-picker input[type=checkbox]").nth(1).check();
    await expect(page.getByRole("button", { name: "开始生成综述" })).toBeDisabled();
  });

  test("登录浮层不把口令带进 URL", async ({ page, request }) => {
    await page.goto("/review");
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
