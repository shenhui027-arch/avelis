// 定时设置：先选频率（每小时 / 每天 / 每周 / 每月），再选时间，下面用一句白话说明并列出接下来几次的执行时间；
// 懂 cron 的人可以切到“自定义”直接写表达式。预览由后端按业务时区计算，与实际调度同一套规则。
import { useQuery } from "@tanstack/react-query";
import { useEffect, useState } from "react";

import { api, unwrap } from "../../api/client";
import { Seg } from "../../components/ui";

type Mode = "none" | "hourly" | "daily" | "weekly" | "monthly" | "custom";
type State = { mode: Mode; minute: number; hour: number; weekdays: number[]; day: number; custom: string };

const WEEK = ["日", "一", "二", "三", "四", "五", "六"]; // cron：0 = 周日
const WEEK_ORDER = [1, 2, 3, 4, 5, 6, 0];
const pad = (n: number) => String(n).padStart(2, "0");
const DEFAULT: State = { mode: "daily", minute: 0, hour: 9, weekdays: [1], day: 1, custom: "0 9 * * *" };

/** 表达式 → 选择器状态；看不懂的形式归为“自定义”。 */
export function parseCron(expr: string | null | undefined): State {
  if (!expr?.trim()) return { ...DEFAULT, mode: "none" };
  const f = expr.trim().split(/\s+/);
  const num = (s: string) => (/^\d+$/.test(s) ? Number(s) : null);
  if (f.length === 5) {
    const [mi, h, dom, mon, dow] = f;
    const minute = num(mi), hour = num(h);
    if (minute !== null && h === "*" && dom === "*" && mon === "*" && dow === "*") return { ...DEFAULT, mode: "hourly", minute, custom: expr };
    if (minute !== null && hour !== null && mon === "*") {
      if (dom === "*" && dow === "*") return { ...DEFAULT, mode: "daily", minute, hour, custom: expr };
      if (dom === "*" && /^\d(,\d)*$/.test(dow)) return { ...DEFAULT, mode: "weekly", minute, hour, weekdays: [...new Set(dow.split(",").map((d) => Number(d) % 7))], custom: expr }; // 7 与 0 都是周日（与后端一致）
      if (num(dom) !== null && dow === "*") return { ...DEFAULT, mode: "monthly", minute, hour, day: num(dom)!, custom: expr };
    }
  }
  return { ...DEFAULT, mode: "custom", custom: expr };
}

export function buildCron(s: State): string {
  switch (s.mode) {
    case "none": return "";
    case "hourly": return `${s.minute} * * * *`;
    case "daily": return `${s.minute} ${s.hour} * * *`;
    case "weekly": return `${s.minute} ${s.hour} * * ${[...s.weekdays].sort((a, b) => a - b).join(",") || "1"}`;
    case "monthly": return `${s.minute} ${s.hour} ${s.day} * *`;
    default: return s.custom.trim();
  }
}

/** 一句白话说明，任务列表与选择器共用。 */
export function describeCron(expr: string | null | undefined): string {
  const s = parseCron(expr);
  const at = `${pad(s.hour)}:${pad(s.minute)}`;
  switch (s.mode) {
    case "none": return "不定时，只在需要时手动运行";
    case "hourly": return `每小时的第 ${s.minute} 分钟运行`;
    case "daily": return `每天 ${at} 运行`;
    case "weekly": return `每周${WEEK_ORDER.filter((d) => s.weekdays.includes(d)).map((d) => WEEK[d]).join("、")} ${at} 运行`;
    case "monthly": return `每月 ${s.day} 日 ${at} 运行${s.day > 28 ? "（没有这一天的月份会跳过）" : ""}`;
    default: return `按自定义规则运行（${s.custom}）`;
  }
}

type Preview = { expr: string; timezone: string; runs: string[]; error: string | null };

function useCronPreview(expr: string) {
  const [debounced, setDebounced] = useState(expr);
  useEffect(() => { const t = setTimeout(() => setDebounced(expr), 300); return () => clearTimeout(t); }, [expr]);
  return useQuery({
    queryKey: ["cron-preview", debounced], enabled: !!debounced, staleTime: 30_000,
    queryFn: () => unwrap(api.GET("/api/analysis/cron/preview", { params: { query: { expr: debounced, count: 5 } } })) as Promise<Preview>,
  });
}

/** 11月2日 周一 09:00（按业务时区显示） */
function formatRun(iso: string, tz: string) {
  const parts = Object.fromEntries(new Intl.DateTimeFormat("zh-CN", { timeZone: tz, month: "numeric", day: "numeric", weekday: "short",
    hour: "2-digit", minute: "2-digit", hour12: false }).formatToParts(new Date(iso)).map((p) => [p.type, p.value]));
  return `${parts.month}月${parts.day}日 ${parts.weekday} ${parts.hour}:${parts.minute}`;
}

export function CronPicker({ value, onChange }: { value: string; onChange: (expr: string) => void }) {
  const [s, setS] = useState<State>(() => parseCron(value));
  const set = (patch: Partial<State>) => { const next = { ...s, ...patch }; setS(next); onChange(buildCron(next)); };
  const expr = buildCron(s);
  const preview = useCronPreview(expr);
  const time = (
    <label className="av-cron__field"><span>时间</span>
      <input type="time" className="av-input" value={`${pad(s.hour)}:${pad(s.minute)}`}
        onChange={(e) => { const [h, m] = e.target.value.split(":").map(Number); if (!Number.isNaN(h)) set({ hour: h, minute: m || 0 }); }} /></label>
  );
  return (
    <div className="av-cron">
      <Seg label="运行频率" value={s.mode} onChange={(mode) => set(mode === "custom" ? { mode: "custom", custom: buildCron(s) || s.custom } : { mode: mode as Mode })} options={[
        { value: "none", label: "不定时" }, { value: "hourly", label: "每小时" }, { value: "daily", label: "每天" },
        { value: "weekly", label: "每周" }, { value: "monthly", label: "每月" }, { value: "custom", label: "自定义" }]} />

      {s.mode === "hourly" ? <label className="av-cron__field"><span>第几分钟</span>
        <select className="av-input" value={s.minute} onChange={(e) => set({ minute: Number(e.target.value) })}>
          {Array.from({ length: 60 }, (_, m) => <option key={m} value={m}>{m} 分</option>)}</select></label> : null}
      {s.mode === "daily" ? time : null}
      {s.mode === "weekly" ? <>
        <div className="av-cron__field"><span>星期</span>
          <div className="av-cron__days" role="group" aria-label="星期">
            {WEEK_ORDER.map((d) => {
              const on = s.weekdays.includes(d);
              return <button key={d} type="button" aria-pressed={on} className="av-cron__day"
                onClick={() => { if (on && s.weekdays.length === 1) return; set({ weekdays: on ? s.weekdays.filter((x) => x !== d) : [...s.weekdays, d] }); }}>周{WEEK[d]}</button>;
            })}
          </div></div>
        {time}
      </> : null}
      {s.mode === "monthly" ? <>
        <label className="av-cron__field"><span>日期</span>
          <select className="av-input" value={s.day} onChange={(e) => set({ day: Number(e.target.value) })}>
            {Array.from({ length: 31 }, (_, i) => i + 1).map((d) => <option key={d} value={d}>每月 {d} 日</option>)}</select></label>
        {time}
      </> : null}
      {s.mode === "custom" ? <label className="av-cron__field"><span>cron 表达式</span>
        <input className="av-input av-mono" value={s.custom} placeholder="分 时 日 月 周，例如 0 9 * * 1"
          onChange={(e) => set({ custom: e.target.value })} />
        <small>5 段：分 时 日 月 周；* 表示每个，1-5 表示范围，*/15 表示每隔 15</small></label> : null}

      <div className="av-cron__summary">
        <b>{describeCron(expr)}</b>
        {s.mode === "none" ? <span>保存后可以在“定时任务”里随时手动运行。</span>
          : preview.data?.error ? <span className="av-cron__error">{preview.data.error}</span>
          : preview.data?.runs.length ? <>
            <span>接下来 {preview.data.runs.length} 次（{preview.data.timezone}）</span>
            <ol>{preview.data.runs.map((r) => <li key={r}>{formatRun(r, preview.data!.timezone)}</li>)}</ol>
          </> : <span>{preview.isFetching ? "正在计算执行时间…" : ""}</span>}
      </div>
    </div>
  );
}
