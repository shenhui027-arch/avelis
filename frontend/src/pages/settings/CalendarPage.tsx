// 业务日历（ADR 0014 第 4 点）：周末、节假日与调休上班日，日历折算据此给出工作日折算。管理员维护，其他人只读。
import { useQuery } from "@tanstack/react-query";
import { useEffect, useState } from "react";

import { api, unwrap } from "../../api/client";
import { errorText, useAction } from "../../api/hooks";
import { Banner, Hint, PaneHeader, Pill, QueryState, useMessage } from "../../components/ui";
import { SettingsTabs, useRoles } from "../../layout/Shell";

const WEEKDAYS = ["一", "二", "三", "四", "五", "六", "日"]; // Python weekday：周一为 0
const DATE = /^\d{4}-\d{2}-\d{2}$/;

/** 文本框里的日期：按换行、逗号、空格分隔，去重排序；不合法的单独列出，不悄悄丢掉。 */
function parseDates(text: string): { dates: string[]; bad: string[] } {
  const items = text.split(/[\s,，、]+/).map((s) => s.trim()).filter(Boolean);
  const ok = (s: string) => DATE.test(s) && !Number.isNaN(Date.parse(`${s}T00:00:00Z`))
    && new Date(`${s}T00:00:00Z`).toISOString().slice(0, 10) === s;
  return { dates: [...new Set(items.filter(ok))].sort(), bad: items.filter((s) => !ok(s)) };
}

export function CalendarPage() {
  const roles = useRoles();
  const isAdmin = roles.has("admin");
  const calendar = useQuery({ queryKey: ["business-calendar"],
    queryFn: () => unwrap(api.GET("/api/settings/business-calendar")) });
  const [weekend, setWeekend] = useState<number[]>([5, 6]);
  const [holidays, setHolidays] = useState("");
  const [workdays, setWorkdays] = useState("");
  const [message, setMessage] = useMessage();
  // 读到服务端的日历并填进表单后才能保存：加载中或读取失败时保存，会用默认值覆盖已有的节假日与调休
  const [loaded, setLoaded] = useState(false);
  useEffect(() => {
    if (!calendar.data) return;
    setWeekend(calendar.data.weekend_days ?? [5, 6]);
    setHolidays((calendar.data.holidays ?? []).join("\n"));
    setWorkdays((calendar.data.workdays ?? []).join("\n"));
    setLoaded(true);
  }, [calendar.data]);
  const h = parseDates(holidays), w = parseDates(workdays);
  const both = h.dates.filter((d) => w.dates.includes(d));
  const valid = !h.bad.length && !w.bad.length && !both.length;
  const save = useAction(() => unwrap(api.PUT("/api/settings/business-calendar", {
    body: { weekend_days: [...weekend].sort(), holidays: h.dates, workdays: w.dates } })), ["business-calendar"]);

  return (
    <div className="av-fill">
      <div className="av-pagehd">
        <div className="av-pagehd__title"><h1>设置</h1><SettingsTabs current="calendar" /></div>
        <div className="av-actions">
          <button type="button" className="av-btn av-btn--primary" disabled={!isAdmin || !loaded || !valid || save.isPending}
            aria-busy={save.isPending} onClick={() => save.mutate(undefined, {
              onSuccess: () => setMessage({ ok: true, text: "已保存业务日历" }),
              onError: (e) => setMessage({ ok: false, text: errorText(e) }) })}>{save.isPending ? "保存中…" : "保存"}</button>
        </div>
      </div>
      <div className="av-workspace">
        <section className="av-pane">
          <PaneHeader title={<>业务日历 <Hint text="分析里的日历折算按它计算节假日与工作日；未配置时只按自然日折算，节假日记为未知" /></>}>
            {calendar.data ? <Pill tone={calendar.data.configured ? "ok" : "warn"}>{calendar.data.configured ? "已配置" : "未配置"}</Pill> : null}
          </PaneHeader>
          <QueryState query={calendar} rows={3}>{() => <div className="av-scroll av-calendar">
            <fieldset className="av-calendar__group" disabled={!isAdmin}>
              <legend className="av-label">周末</legend>
              <div className="av-calendar__days">
                {WEEKDAYS.map((label, day) => <label key={day} className="av-calendar__day">
                  <input type="checkbox" checked={weekend.includes(day)}
                    onChange={(e) => setWeekend(e.target.checked ? [...weekend, day] : weekend.filter((d) => d !== day))} />
                  <span>周{label}</span></label>)}
              </div>
            </fieldset>
            <div className="av-calendar__lists">
              <label className="av-field"><span className="av-label">节假日 <span className="av-caption">{h.dates.length} 天</span></span>
                <textarea className="av-textarea av-mono" rows={12} readOnly={!isAdmin} value={holidays} placeholder="YYYY-MM-DD，每行一个"
                  aria-invalid={h.bad.length > 0} onChange={(e) => setHolidays(e.target.value)} />
                {h.bad.length ? <span className="av-help av-help--danger">无法识别：{h.bad.slice(0, 5).join("、")}</span> : null}</label>
              <label className="av-field"><span className="av-label">调休上班日 <span className="av-caption">{w.dates.length} 天</span></span>
                <textarea className="av-textarea av-mono" rows={12} readOnly={!isAdmin} value={workdays} placeholder="落在周末、但要上班的日期"
                  aria-invalid={w.bad.length > 0 || both.length > 0} onChange={(e) => setWorkdays(e.target.value)} />
                {w.bad.length ? <span className="av-help av-help--danger">无法识别：{w.bad.slice(0, 5).join("、")}</span> : null}
                {both.length ? <span className="av-help av-help--danger">同一天不能既是节假日又是上班日：{both.slice(0, 5).join("、")}</span> : null}</label>
            </div>
            <Banner message={message} />
          </div>}</QueryState>
        </section>
      </div>
    </div>
  );
}
