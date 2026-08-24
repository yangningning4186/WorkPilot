"use client";

import { useCallback, useEffect, useState } from "react";

import { useAdminSession } from "@/components/admin-session";
import { WorkdeskAppShell } from "@/components/workdesk-shell";
import {
  ApiError,
  type CostOverviewResponse,
  type CostTierUsage,
  fetchCostOverview,
} from "@/lib/api";

const WINDOWS = [7, 30, 90] as const;

/**
 * 金额的唯一渲染入口。
 *
 * **缺价格必须显示"不可用"，不能显示 $0.00。** 本机模型自部署价格表是 0，
 * 把 null 折成 0 之后，"没有可用价格"和"测过、就是不要钱"在页面上再也分不开
 * （docs/07 §7.4）。这个函数存在的全部理由就是不让那件事发生。
 */
function money(value: string | null): string {
  return value === null ? "不可用" : `$${value}`;
}

function ratio(value: number): string {
  return `${(value * 100).toFixed(1)}%`;
}

function count(value: number): string {
  return value.toLocaleString("zh-CN");
}

function TierCard({ tier }: { tier: CostTierUsage }) {
  return (
    <article className="cost-card">
      <header>
        <strong>{tier.tier}</strong>
        <span className="cost-card-models" title={tier.models.join(", ")}>
          {tier.models.join(", ") || "—"}
        </span>
      </header>
      <dl>
        <div>
          <dt>调用</dt>
          <dd>{count(tier.call_count)}</dd>
        </div>
        <div>
          <dt>缓存命中</dt>
          <dd title="命中不消耗 GPU，也不计入 token">{ratio(tier.cache_hit_rate)}</dd>
        </div>
        <div>
          <dt>Prompt Cache</dt>
          <dd
            title={`读取 ${count(tier.prompt_cache_read_tokens)} / 写入 ${count(tier.prompt_cache_write_tokens)} token`}
          >
            {ratio(tier.prompt_cache_read_rate)}
          </dd>
        </div>
        <div>
          <dt>token</dt>
          <dd>{count(tier.total_tokens)}</dd>
        </div>
        <div>
          <dt>p95 延迟</dt>
          <dd>{tier.p95_latency_ms === null ? "—" : `${count(tier.p95_latency_ms)}ms`}</dd>
        </div>
        {tier.fallback_count > 0 && (
          <div className="cost-warn">
            <dt>fallback</dt>
            <dd>{count(tier.fallback_count)}</dd>
          </div>
        )}
        {tier.failed_count > 0 && (
          <div className="cost-warn">
            <dt>失败</dt>
            <dd>{count(tier.failed_count)}</dd>
          </div>
        )}
      </dl>
    </article>
  );
}

export default function CostPage() {
  const { state: authState } = useAdminSession();
  const [days, setDays] = useState<number>(30);
  const [data, setData] = useState<CostOverviewResponse | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [loading, setLoading] = useState(false);

  const load = useCallback(async (window: number) => {
    setLoading(true);
    try {
      setData(await fetchCostOverview(window));
      setError(null);
    } catch (cause) {
      // 401 不是错误态，是"还没登录"——提示登录而不是报故障（约束 4 同样适用于人）
      setError(
        cause instanceof ApiError && cause.status === 401
          ? "成本页是运营信息，需要先登录 owner。"
          : "读取成本数据失败。",
      );
      setData(null);
    } finally {
      setLoading(false);
    }
  }, []);

  // 与资料库页同一个写法：延后一拍触发，顺带避开"在 effect 里同步 setState"。
  // authState 进依赖是因为登录成功后要自动重取，否则用户登录完还得手动刷新。
  useEffect(() => {
    const timer = setTimeout(() => void load(days), 0);
    return () => clearTimeout(timer);
  }, [days, load, authState]);

  return (
    <WorkdeskAppShell icon="automation" sectionTitle="成本">
      <div className="cost-body workdesk-route-surface">
        <div className="cost-head">
          <div>
            <h1>成本</h1>
            <p>
              按整批 GPU 墙钟摊销的口径。成本、吞吐、并发度、占用率一起看才有意义。
            </p>
          </div>
          <div className="cost-windows" role="group" aria-label="统计窗口">
            {WINDOWS.map((window) => (
              <button
                key={window}
                type="button"
                aria-pressed={days === window}
                onClick={() => setDays(window)}
              >
                {window} 天
              </button>
            ))}
          </div>
        </div>

        {error !== null && <p className="cost-notice">{error}</p>}
        {loading && data === null && <p className="cost-notice">加载中…</p>}

        {data !== null && (
          <>
            {data.undeployed_tiers.length > 0 && (
              // 这条不能省：light 没部署时，路由到 light 的任务其实都在跑 main，
              // 而档位分布上它只表现为"0 次调用"——那读起来像"没人用"，不像"用不了"。
              <p className="cost-notice warn">
                未部署的档位：{data.undeployed_tiers.join("、")}。
                路由到这些档位的任务实际按 fallback 链落在其他档，档位分布要照这个读。
              </p>
            )}

            <section className="cost-totals">
              <div>
                <span>调用</span>
                <strong>{count(data.totals.call_count)}</strong>
              </div>
              <div>
                <span>缓存命中率</span>
                <strong>{ratio(data.totals.cache_hit_rate)}</strong>
              </div>
              <div>
                <span>Prompt Cache</span>
                <strong>{ratio(data.totals.prompt_cache_read_rate)}</strong>
              </div>
              <div>
                <span>token</span>
                <strong>{count(data.totals.total_tokens)}</strong>
              </div>
              <div>
                <span>有单价</span>
                <strong>{count(data.totals.priced_count)}</strong>
              </div>
              <div>
                <span>金额</span>
                <strong>{money(data.totals.cost_usd)}</strong>
              </div>
            </section>

            {data.totals.unpriced_count > 0 && (
              <p className="cost-notice">
                {data.totals.unpriced_count} 次调用没有可用单价（本机自部署模型），
                金额口径为 <code>{data.totals.cost_status}</code>；
                这部分成本以 <strong>token 用量</strong>计，不折算美元——
                写成 $0.00 会让「免费」和「没测过」看起来一样。
              </p>
            )}

            <h2>分档</h2>
            <div className="cost-cards">
              {data.by_tier.map((tier) => (
                <TierCard key={tier.tier} tier={tier} />
              ))}
              {data.by_tier.length === 0 && <p className="cost-notice">窗口内没有调用记录。</p>}
            </div>

            <h2>按任务类型</h2>
            <div className="cost-table-wrap">
              <table className="cost-table">
                <thead>
                  <tr>
                    <th>任务</th>
                    <th>实际档位</th>
                    <th className="num">调用</th>
                    <th className="num">token</th>
                    <th className="num">命中率</th>
                  </tr>
                </thead>
                <tbody>
                  {data.by_task_type.map((row) => (
                    <tr key={`${row.task_type}:${row.tier}`}>
                      <td>{row.task_type}</td>
                      <td>{row.tier}</td>
                      <td className="num">{count(row.call_count)}</td>
                      <td className="num">{count(row.total_tokens)}</td>
                      <td className="num">{ratio(row.cache_hit_rate)}</td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          </>
        )}
      </div>
    </WorkdeskAppShell>
  );
}
