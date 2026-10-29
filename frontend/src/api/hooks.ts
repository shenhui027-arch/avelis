// 数据查询与操作。所有请求经 openapi-fetch，类型来自后端 OpenAPI。
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";

import { api, ApiError, unwrap, type Schemas } from "./client";

export type Asset = Schemas["AssetOut"];
export type AssetDetail = Schemas["AssetDetailOut"];
export type Metric = Schemas["MetricOut"];
export type MetricVersion = Schemas["MetricVersionOut"];
export type Domain = Schemas["DomainOut"];
export type ChangeSet = Schemas["ChangeSetOut"];
export type Task = Schemas["TaskOut"];
export type Answer = Schemas["AnswerOut"];
export type ImportJob = Schemas["ImportJobOut"];
export type JoinPath = Schemas["JoinPathOut"];
export type Datasource = Schemas["DatasourceOut"];
export type User = Schemas["UserOut"];
export type Graph = Schemas["GraphOut"];

export function errorText(e: unknown): string {
  if (e instanceof ApiError) {
    const body = e.body as { blockers?: string[] } | undefined;
    return body?.blockers?.length ? `${e.message}：${body.blockers.join("；")}` : e.message;
  }
  return e instanceof Error ? e.message : "操作失败";
}

export const useAppSettings = () => useQuery({
  queryKey: ["settings"],
  queryFn: () => unwrap(api.GET("/api/settings")) as Promise<{ business_timezone?: string; self_review_policy?: string }>,
});
export const useTimeZone = () => useAppSettings().data?.business_timezone ?? "Asia/Shanghai";
export const useWorkbench = () => useQuery({ queryKey: ["workbench"], queryFn: () => unwrap(api.GET("/api/workbench")) });
export const useDatasources = () => useQuery({ queryKey: ["datasources"], queryFn: () => unwrap(api.GET("/api/datasources")) });
export const useUsers = () => useQuery({ queryKey: ["users"], queryFn: () => unwrap(api.GET("/api/users")) });
export const useDomains = () => useQuery({ queryKey: ["domains"], queryFn: () => unwrap(api.GET("/api/domains")) });
export const useAssets = () => useQuery({ queryKey: ["assets"], queryFn: () => unwrap(api.GET("/api/assets")) });
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
export const useImports = () => useQuery({ queryKey: ["imports"], queryFn: () => unwrap(api.GET("/api/imports")) });
export const useImport = (id?: string) => useQuery({
  queryKey: ["import", id], enabled: !!id,
  queryFn: () => unwrap(api.GET("/api/imports/{job_id}", { params: { path: { job_id: id! } } })),
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

/** 操作：成功后刷新相关查询；失败抛出后端的中文原因。 */
export function useAction<TVars, TResult>(fn: (vars: TVars) => Promise<TResult>, invalidate: string[] = []) {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: fn,
    onSuccess: () => {
      for (const key of [...invalidate, "workbench"]) qc.invalidateQueries({ queryKey: [key] });
    },
  });
}
