// 采集状态条（系统状态块的形态）：数据源最近若干次采集，一根一次，旧的在左、新的在右；悬停看时间与结果。
import { useQuery } from "@tanstack/react-query";

import { api, unwrap } from "../api/client";
import { formatTime } from "../format";
import { t } from "../i18n";
import { statusOf } from "./ui";

export function useRuns(id: string) {
  return useQuery({
    queryKey: ["runs", id],
    queryFn: () => unwrap(api.GET("/api/datasources/{datasource_id}/runs", { params: { path: { datasource_id: id } } })),
  });
}

const TONE: Record<string, string> = { succeeded: "is-ok", failed: "is-danger", running: "is-warn", queued: "is-warn" };

export function RunStrip({ id, count = 24, tz }: { id: string; count?: number; tz?: string }) {
  const runs = useRuns(id);
  const recent = [...(runs.data ?? [])]
    .sort((a, b) => String(a.started_at).localeCompare(String(b.started_at)))
    .slice(-count);
  const pad = Math.max(0, count - recent.length);
  const ok = recent.filter((r) => r.status === "succeeded").length;
  return (
    <span className="av-strip" role="img" aria-label={t("最近 {{count}} 次采集，成功 {{ok}} 次", { count: recent.length, ok })}>
      {Array.from({ length: pad }, (_, i) => <i key={`pad-${i}`} />)}
      {recent.map((r) => (
        <i key={r.id} className={TONE[r.status] ?? ""} data-tip={`${statusOf(r.status)[1]} · ${formatTime(r.started_at, tz)}`} />
      ))}
    </span>
  );
}
