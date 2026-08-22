import { redirect } from "next/navigation";

/**
 * 根路径重定向到 Cowork。
 *
 * 这里原来是 RAG 可溯源问答的独立网页端。RAG 收进桌面端之后不再有独立入口：
 * 单篇走 Cowork 的论文阅读模式，跨篇走挂知识库。留一个重定向而不是删掉整个路由，
 * 是因为书签、桌面壳的启动地址和 OAuth 回跳都可能落在 `/` 上。
 */
export default function RootPage() {
  redirect("/cowork");
}
