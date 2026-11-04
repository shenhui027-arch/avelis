// 数据查询与操作。所有请求经 openapi-fetch，类型来自后端 OpenAPI。
import {
  keepPreviousData, useInfiniteQuery, useMutation, useQueries, useQuery, useQueryClient, type InfiniteData, type QueryKey,
} from "@tanstack/react-query";

import { t } from "../i18n";
import { api, ApiError, unwrap, type Schemas } from "./client";

export type Asset = Schemas["AssetOut"];
export type AssetDetail = Schemas["AssetDetailOut"];
export type Metric = Schemas["MetricOut"];
export type MetricVersion = Schemas["MetricVersionOut"];
export type Domain = Schemas["DomainOut"];
export type ChangeSet = Schemas["ChangeSetOut"];
export type Task = Schemas["TaskOut"];
export type Answer = Schemas["AnswerOut"];
export type ImportBatch = Schemas["ImportBatchOut"];
export type ImportStatement = Schemas["StatementOut"];
export type ImportCandidate = Schemas["CandidateOut"];
export type JoinPath = Schemas["JoinPathOut"];
export type Datasource = Schemas["DatasourceOut"];
export type User = Schemas["UserOut"];
export type Graph = Schemas["GraphOut"];

export function errorText(e: unknown): string {
  if (e instanceof ApiError) {
    const body = e.body as { blockers?: string[] } | undefined;
    return body?.blockers?.length ? t("{{message}}：{{reasons}}", { message: e.message, reasons: body.blockers.join(t("；")) }) : e.message;
  }
  return e instanceof Error ? e.message : t("操作失败");
}

export const useAppSettings = () => useQuery({
  queryKey: ["settings"],
  queryFn: () => unwrap(api.GET("/api/settings")) as Promise<{ business_timezone?: string; self_review_policy?: string; number_style?: "cn" | "intl" }>,
});
export const useTimeZone = () => useAppSettings().data?.business_timezone ?? "Asia/Shanghai";
/** 数字显示体系（ADR 0014）：cn 用万、亿，intl 用 K、M、B；与后端报告渲染一致 */
export const useNumberStyle = () => useAppSettings().data?.number_style ?? "cn";
export const useWorkbench = () => useQuery({ queryKey: ["workbench"], queryFn: () => unwrap(api.GET("/api/workbench")) });
export const useDatasources = () => useQuery({ queryKey: ["datasources"], queryFn: () => unwrap(api.GET("/api/datasources")) });
/** 支持问数执行的数据源（按引擎能力，不按数据库类型）：指标与关联路径只能建在这些数据源的资产上。 */
export function useQueryableDatasourceIds(): Set<string> {
  const sources = useDatasources().data ?? [];
  return new Set(sources.filter((d) => d.engine.capabilities.includes("query")).map((d) => d.id));
}
export const useEngines = () => useQuery({ queryKey: ["engines"], queryFn: () => unwrap(api.GET("/api/engines")), staleTime: Infinity });
export const useUsers = () => useQuery({ queryKey: ["users"], queryFn: () => unwrap(api.GET("/api/users")) });
export const useDomains = () => useQuery({ queryKey: ["domains"], queryFn: () => unwrap(api.GET("/api/domains")) });
/** 示例问题：来自可见业务域的配置，没有配置时由后端按可见口径生成（ADR 0010），不写死在前端。 */
export const useExampleQuestions = (mode: "ask" | "analysis") => useQuery({
  queryKey: ["example-questions", mode], staleTime: 60_000,
  queryFn: () => unwrap(api.GET("/api/example-questions", { params: { query: { mode } } })),
});
/** 资产按需查询：搜索、按数据源 / 业务域过滤、只要可查询的、按 id 批量取；不再整表加载（资产随数据规模增长）。 */
export type AssetQuery = { q?: string; datasource_id?: string; domain_id?: string; queryable?: boolean; ids?: string[]; limit?: number; offset?: number };
export const useAssetSearch = (query: AssetQuery, enabled = true) => useQuery({
  queryKey: ["assets", query], enabled, placeholderData: (prev) => prev,
  queryFn: () => unwrap(api.GET("/api/assets", { params: { query } })),
});
/** 资产分页搜索（选择器用）：每页多取一条判断有无下一页，“加载更多”时接着取，已显示的项不清空。 */
export const useAssetPages = (query: Omit<AssetQuery, "limit" | "offset">, page: number, enabled = true) => useInfiniteQuery({
  queryKey: ["assets", "pages", query, page], enabled, placeholderData: keepPreviousData, initialPageParam: 0,
  queryFn: ({ pageParam }) => unwrap(api.GET("/api/assets", { params: { query: { ...query, limit: page + 1, offset: pageParam } } })),
  getNextPageParam: (last, _all, offset) => (last.length > page ? offset + page : undefined),
});
/** 按 id 取资产（显示名称、核对是否仍可见）；id 排序去重，同一组 id 共用缓存。 */
export const useAssetsByIds = (ids: string[], extra: Omit<AssetQuery, "ids"> = {}) => {
  const unique = [...new Set(ids.filter(Boolean))].sort();
  return useAssetSearch({ ...extra, ids: unique }, unique.length > 0);
};
/** 按 id 取多个资产的名称：接口一次最多 200 个 id，超过时分批请求再合并，不截断。 */
export function useAssetNames(ids: string[]): Map<string, string> {
  const unique = [...new Set(ids.filter(Boolean))].sort();
  const batches: string[][] = [];
  for (let i = 0; i < unique.length; i += 200) batches.push(unique.slice(i, i + 200));
  const results = useQueries({ queries: batches.map((batch) => ({
    queryKey: ["assets", { ids: batch }],
    queryFn: () => unwrap(api.GET("/api/assets", { params: { query: { ids: batch } } })),
  })) });
  return new Map(results.flatMap((r) => r.data ?? []).map((a) => [a.id, a.qualified]));
}
/** 目录各类对象的可见数量（导航角标）：一次聚合查询，不加载列表。 */
export const useCatalogCounts = () => useQuery({ queryKey: ["catalog-counts"], queryFn: () => unwrap(api.GET("/api/catalog/counts")) });
export const useAsset = (id?: string) => useQuery({
  queryKey: ["asset", id], enabled: !!id,
  queryFn: () => unwrap(api.GET("/api/assets/{asset_id}", { params: { path: { asset_id: id! } } })),
});
export const useMetrics = () => useQuery({ queryKey: ["metrics"], queryFn: () => unwrap(api.GET("/api/metrics")) });
export const useMetricVersion = (id?: string) => useQuery({
  queryKey: ["metric-version", id], enabled: !!id,
  queryFn: () => unwrap(api.GET("/api/metric-versions/{version_id}", { params: { path: { version_id: id! } } })),
});
export const useJoinPaths = () => useQuery({ queryKey: ["join-paths"], queryFn: () => unwrap(api.GET("/api/join-paths")) });
export const useChangeSets = () => useQuery({ queryKey: ["change-sets"], queryFn: () => unwrap(api.GET("/api/change-sets")) });
export const useChangeSet = (id?: string) => useQuery({
  queryKey: ["change-set", id], enabled: !!id,
  queryFn: () => unwrap(api.GET("/api/change-sets/{change_set_id}", { params: { path: { change_set_id: id! } } })),
});
export const useTasks = (mine = false) => useQuery({
  queryKey: ["tasks", mine],
  queryFn: () => unwrap(api.GET("/api/review-tasks", { params: { query: { mine, status: null } } })),
});
export const useThreads = () => useQuery({ queryKey: ["threads"], queryFn: () => unwrap(api.GET("/api/ask/threads")) });
export const useThread = (id?: string) => useQuery({
  queryKey: ["thread", id], enabled: !!id,
  queryFn: () => unwrap(api.GET("/api/ask/threads/{thread_id}", { params: { path: { thread_id: id! } } })),
});
// 导入批次在后台执行：排队或进行中时轮询进度，结束后停止
const importBusy = (status?: string) => status === "queued" || status === "running";
export const useImports = () => useQuery({ queryKey: ["imports"], queryFn: () => unwrap(api.GET("/api/imports")),
  refetchInterval: (q) => (q.state.data?.some((b) => importBusy(b.status)) ? 2000 : false) });
export const useImport = (id?: string) => useQuery({
  queryKey: ["import", id], enabled: !!id,
  queryFn: () => unwrap(api.GET("/api/imports/{batch_id}", { params: { path: { batch_id: id! } } })),
  refetchInterval: (q) => (importBusy(q.state.data?.status) ? 1000 : false),
});
// 语句与候选按固定页大小分页（接口上限 500），“加载更多”递增 offset 并累积各页
type ImportPage = { offset: number; items: unknown[]; total: number };
const nextOffset = (last: ImportPage) =>
  last.offset + last.items.length < last.total && last.items.length ? last.offset + last.items.length : undefined;
export type ImportStatementQuery = { file_id?: string | null; status?: string | null; q?: string | null };
export const useImportStatements = (batchId: string, query: ImportStatementQuery, version: string, pageSize = 100) =>
  useInfiniteQuery<Schemas["ImportStatementPage"], Error, InfiniteData<Schemas["ImportStatementPage"]>, QueryKey, number>({
    queryKey: ["import", batchId, "statements", query, version, pageSize], placeholderData: keepPreviousData,
    initialPageParam: 0, getNextPageParam: nextOffset,
    queryFn: ({ pageParam }) => unwrap(api.GET("/api/imports/{batch_id}/statements", {
      params: { path: { batch_id: batchId }, query: { ...query, offset: pageParam, limit: pageSize } } })),
  });
export type ImportCandidateQuery = { file_id?: string | null; status?: string | null; origin?: string | null };
export const useImportCandidates = (batchId: string, query: ImportCandidateQuery, version: string, pageSize = 100) =>
  useInfiniteQuery<Schemas["ImportCandidatePage"], Error, InfiniteData<Schemas["ImportCandidatePage"]>, QueryKey, number>({
    queryKey: ["import", batchId, "candidates", query, version, pageSize], placeholderData: keepPreviousData,
    initialPageParam: 0, getNextPageParam: nextOffset,
    queryFn: ({ pageParam }) => unwrap(api.GET("/api/imports/{batch_id}/candidates", {
      params: { path: { batch_id: batchId }, query: { ...query, statement_id: null, offset: pageParam, limit: pageSize } } })),
  });
export const useImportContent = (batchId: string, fileId?: string) => useQuery({
  queryKey: ["import", batchId, "content", fileId], enabled: !!fileId, staleTime: Infinity,
  queryFn: () => unwrap(api.GET("/api/imports/{batch_id}/files/{job_id}/content", {
    params: { path: { batch_id: batchId, job_id: fileId! } } })),
});
export const useImportPayload = (batchId: string, version: string) => useQuery({
  queryKey: ["import", batchId, "payload", version],
  queryFn: () => unwrap(api.GET("/api/imports/{batch_id}/model-payload", { params: { path: { batch_id: batchId } } })),
});
export const useSqlFunctions = (datasourceId?: string) => useQuery({
  queryKey: ["import-sql-functions", datasourceId], enabled: !!datasourceId,
  queryFn: () => unwrap(api.GET("/api/imports/sql-functions/{datasource_id}", {
    params: { path: { datasource_id: datasourceId! } } })),
});
export const useGraph = (domainIds: string[], assetId?: string) => useQuery({
  queryKey: ["graph", domainIds.join(","), assetId],
  queryFn: () => unwrap(api.GET("/api/lineage/graph", {
    params: { query: { domain_ids: domainIds.length ? domainIds.join(",") : null, asset_id: assetId ?? null } },
  })),
});
export const useAudit = (objectType?: string, objectId?: string) => useQuery({
  queryKey: ["audit", objectType, objectId], enabled: !!objectId,
  queryFn: () => unwrap(api.GET("/api/audit", {
    params: { query: { object_type: objectType ?? null, object_id: objectId ?? null, limit: 50 } } })),
});

/** 操作：成功后刷新相关查询；失败抛出后端给出的原因。 */
const CATALOG_KEYS = new Set(["metrics", "assets", "domains", "join-paths"]);

export function useAction<TVars, TResult>(fn: (vars: TVars) => Promise<TResult>, invalidate: string[] = []) {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: fn,
    onSuccess: () => {
      // 目录对象增删改后，导航角标的计数也要刷新（计数接口独立于各列表的缓存）
      const counted = invalidate.some((key) => CATALOG_KEYS.has(key));
      for (const key of [...invalidate, "workbench", ...(counted ? ["catalog-counts"] : [])]) qc.invalidateQueries({ queryKey: [key] });
    },
  });
}

// 分析运行与定时任务：侧边栏的最近对话与分析页共用；有进行中的运行时定期刷新，状态与详情一致
export const useAnalysisRuns = () => useQuery({ queryKey: ["analysis-runs"], queryFn: () => unwrap(api.GET("/api/analysis/runs")),
  refetchInterval: (q) => (q.state.data?.some((r) => ["queued", "running"].includes(r.status)) ? 4000 : false) });
export const useAnalysisTasks = () => useQuery({ queryKey: ["analysis-tasks"], queryFn: () => unwrap(api.GET("/api/analysis/tasks")) });
