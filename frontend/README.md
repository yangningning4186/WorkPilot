# frontend

Next.js 16 App Router + 原生 CSS + 自写 SSE 客户端（未引入 shadcn/ui 与 Vercel AI SDK）。
唯一的第三方渲染依赖是 react-markdown + remark-gfm：答案正文必须支持表格与代码块，
自己写 Markdown 解析器只会更糟。**不要加 rehype-raw**——证据是不可信数据，
"默认不渲染裸 HTML" 是这里的安全边界（见 `src/components/answer-markdown.tsx`）。
设计见 [docs/08-前端设计.md](../docs/08-前端设计.md)。

开发前先定死 SSE 事件协议（08 §3），再写两端。
边界情况清单 B1–B10 是面试真正会问的部分，逐条自测。

浏览器默认只访问同源 `/api/*`，由 Next.js 转发到后端，避免本地与线上分别维护 CORS。
服务端目标通过 `BACKEND_ORIGIN` 配置，开发环境默认是 `http://127.0.0.1:8000`。
页面字体使用系统字体栈，不在构建期或运行期请求 Google Fonts。

## 前端验收

覆盖主闭环「提问 → SSE 流式回答 → 点击引用 → 原文高亮」，共 13 条用例：

| 用例 | 验的是什么 |
|---|---|
| PDF 引用主闭环 | 正文分片到达、引用卡片、**高亮像素位置 = `bbox_norm` × 渲染尺寸** |
| 跨页引用 / 切换引用 / 关闭预览 | 多 location 的页码 tab、选中态跟随、回到占位态 |
| Markdown 引用 | 按 quote 精确 `<mark>`，不退化成 fallback |
| 拒答 | 无引用、无预览、可继续提问 |
| B1 刷新恢复 | 刷新后正文与引用**逐字一致**（补历史路径与实时路径同一套折叠逻辑） |
| B2 断线续传 | 掐断连接后 `Last-Event-ID` 续发，正文不重复不缺段 |
| 重连重放 | 服务端重发看过的事件时，前端按 seq 去重（**去掉去重这条会红**） |
| B3 关页面再回来 | run 状态只由 URL 上的 run_id 决定 |
| B5 并发隔离 | 两个标签页各自一条 EventSource，正文不串台 |
| 取消 / 失败态 / 创建失败 | 停止落到 cancel 接口、错误可读且不谎报可重试、不留假加载态 |
| Markdown 渲染 | 流式半截语法不炸、`[S1]` 变可点锚点、代码块里的 `[S1]` 保持字面量 |
| 证据注入 | 答案里的裸 HTML 不解析、`javascript:` 链接被过滤 |
| 通用知识切换 | 拒答后一键降级，回答挂免责标识且没有引用 |
| 资料库页 | 四种解析状态如实区分（尤其"新版失败但旧版仍在服务"）、统计与同步入口 |

```bash
npm run test:e2e          # 全量验收（自动起 mock 后端 + 产线构建）
npm run test:e2e:ui       # 交互式调试
npx playwright test -g 断线   # 只跑某条用例
```

首次运行需要 `npx playwright install chromium`。

**为什么打的是 mock 后端而不是真后端**：真链路要 Postgres + 本地推理服务 + 已入库语料，
CI runner 连不到自建集群；而且 LLM 输出不确定，断言只能写得很松，验收就退化成"页面没崩"。
假后端把 run 事件按剧本回放（`tests/e2e/mock-backend.mjs`），SSE 帧格式、`Last-Event-ID`
续传语义、引用 payload 字段都与后端逐字段对齐——**剧本改动必须同步真实契约**，
否则这套验收测的只是它自己。

关键断言不是"面板打开了"，而是**高亮矩形的实际像素位置等于 `bbox_norm` × 渲染尺寸**
（`helpers.ts::expectHighlightMatchesBbox`）。引用错位是只存 bbox 四个数最典型的失败方式，
只断言可见性会漏掉它。

用例是否真的会咬人，靠变异验证：把 `bbox_norm` 的左边界偏 1%、把 `run-state.ts` 的 seq 去重
删掉，对应用例分别变红。新增用例时照做一遍，否则很容易写出一条永远绿的断言。

**待补**：demo session 鉴权落地后要加 401 / 跨 session 访问的场景，
届时 mock 后端也要跟着发 cookie。

跑真后端的冒烟（需要后端在 8000 端口、库里有已入库文档）：

```bash
BACKEND_ORIGIN=http://127.0.0.1:8000 npm run dev   # 另开终端手动验
```

真链路目前没有自动化用例：gold answer 不确定，值得投入的是评测框架而不是 E2E 断言。
