// 问数回答的文字部分：确定性要点卡（后端 ask/insights.py 算出，不依赖模型）与模型解读。问数页与分析页的取数答案共用。
import { Hint, Icon } from "./ui";
import { t } from "../i18n";
import type { Insight } from "../pages/ask/presentation";

const INSIGHT_LOOK: Record<string, { label: string; tone: string }> = {
  total: { label: t("合计"), tone: "collect" }, change: { label: t("变化"), tone: "ask" }, peak: { label: t("最高"), tone: "lineage" },
  low: { label: t("最低"), tone: "change" }, share: { label: t("集中度"), tone: "catalog" }, spread: { label: t("差距"), tone: "flow" },
};

/** 确定性要点：后端从结果直接算出（合计、变化、最高最低、集中度），不依赖模型，每条一张小卡。 */
export function Insights({ items }: { items: Insight[] }) {
  return (
    <ul className="av-insights" aria-label={t("要点")}>
      {items.map((item, i) => {
        const look = INSIGHT_LOOK[item.kind] ?? { label: t("要点"), tone: "accent" };
        return <li key={i} className={`av-insight tone-${look.tone}`}><span className="av-insight__label">{look.label}</span>{item.text}</li>;
      })}
    </ul>
  );
}

/** 模型的解读：首行是结论，“- ”开头的是观察，“追问：”开头的变成一键追问。 */
export function Narrative({ text, onPick, disabled }: { text: string; onPick: (q: string) => void; disabled: boolean }) {
  const lines = text.split(/\n+/).map((line) => line.trim()).filter(Boolean);
  const follow = lines.find((line) => /^(追问|追問|Follow-up)[:：]/i.test(line))?.replace(/^[^:：]+[:：]\s*/, "");
  // 列表标记后必须有空白，行首的负数（如“-5%”）不是列表项
  const bullet = /^[-•·*]\s+/;
  const points = lines.filter((line) => bullet.test(line)).map((line) => line.replace(bullet, ""));
  const lead = lines.filter((line) => !bullet.test(line) && !/^(追问|追問|Follow-up)[:：]/i.test(line)).join(" ");
  return (
    <section className="av-narrative" aria-label={t("模型解读")}>
      <div className="av-narrative__hd"><Icon name="analysis" small />{t("模型解读")}
        <Hint text={t("模型基于上面的结果与要点所写，只引用其中的数值；原因需要进一步拆解确认")} /></div>
      {lead ? <p className="av-narrative__lead">{lead}</p> : null}
      {points.length ? <ul className="av-narrative__points">{points.map((p, i) => <li key={i}>{p}</li>)}</ul> : null}
      {follow ? <button type="button" className="av-narrative__follow" disabled={disabled}
        onClick={(e) => { e.stopPropagation(); onPick(follow); }}><Icon name="arrow" small />{follow}</button> : null}
    </section>
  );
}
