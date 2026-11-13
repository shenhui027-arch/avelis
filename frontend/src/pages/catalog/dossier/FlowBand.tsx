// 档案页顶部的流向带：从哪些表来 → 这张表 → 被哪些地方用。
import { useState } from "react";
import { Link } from "react-router";

import { errorText, useTimeZone, type AssetDetail, type AssetDossier } from "../../../api/hooks";
import { Hint, Icon, KIND_LABEL, Pill, Plat, Status, Tile } from "../../../components/ui";
import { formatShortTime } from "../../../format";
import { t } from "../../../i18n";
import { initialOf, latestAsker, schemaOf, summaryState, tableName, usage, visible } from "./dossier";
import { NoteEditor, useSaveNote } from "./notes";

type Message = (m: { ok: boolean; text: string }) => void;
type TableRef = NonNullable<AssetDossier["upstream"]>[number];

const UPSTREAM_LIMIT = 4;

export const WRITE_MODE_LABEL: Record<string, string> = {
  overwrite: t("每次整表覆盖"), append: t("每次追加"), create: t("每次重建"),
};

export function FlowBand({ a, d, onMessage }: { a: AssetDetail; d: AssetDossier; onMessage: Message }) {
  return (
    <div className="av-dossier-flow">
      <Upstream items={d.upstream ?? []} />
      <span className="av-dossier-flow__wire" aria-hidden><Icon name="arrow" /></span>
      <Hero a={a} d={d} onMessage={onMessage} />
      <span className="av-dossier-flow__wire" aria-hidden><Icon name="arrow" /></span>
      <Usage d={d} />
    </div>
  );
}

function Upstream({ items }: { items: TableRef[] }) {
  const [expanded, setExpanded] = useState(false);
  const { shown, hidden } = visible(items, UPSTREAM_LIMIT, expanded);
  return (
    <div className="av-dossier-flow__col" aria-label={t("上游表")} role="group">
      <span className="av-dossier-flow__cap">{items.length ? t("从这 {{count}} 张表来", { count: items.length }) : t("上游表")}</span>
      {shown.map((u, i) => <TableNode key={`${u.ref}-${i}`} item={u} />)}
      {hidden ? <button type="button" className="av-btn av-btn--ghost av-btn--sm av-dossier-flow__more" onClick={() => setExpanded(true)}>
        {t("另有 {{count}} 张", { count: hidden })}</button> : null}
      {!items.length ? <span className="av-dossier-flow__none">{t("没有采集到上游表")}</span> : null}
    </div>
  );
}

function TableNode({ item }: { item: TableRef }) {
  const body = <>
    <Tile icon="table" tone="lineage" size="sm" />
    <span className="av-dossier-row__main">
      <span className="av-mono" title={item.ref}>{tableName(item.ref)}</span>
      {item.comment ? <span>{item.comment}</span> : null}
    </span>
    {item.columns ? <span className="av-tag" title={t("{{count}} 个字段", { count: item.columns })}>{item.columns}</span> : null}
  </>;
  return item.asset_id
    ? <Link className="av-dossier-node av-dossier-node--link" to={`/catalog/assets/${item.asset_id}`}>{body}</Link>
    : <span className="av-dossier-node" title={t("未采集或无权查看")}>{body}</span>;
}

function Hero({ a, d, onMessage }: { a: AssetDetail; d: AssetDossier; onMessage: Message }) {
  const [editing, setEditing] = useState(false);
  const save = useSaveNote(a.id);
  const summary = d.summary;
  const state = summaryState(summary);
  const writeMode = d.produced?.write_mode;
  const schema = schemaOf(a.qualified);
  const count = d.column_count ?? a.columns.length;
  const submit = (text: string, confirm: boolean) => save.mutate({ column: null, text }, {
    onSuccess: () => { setEditing(false); onMessage({ ok: true, text: confirm ? t("已确认这张表的说明") : t("已保存这张表的说明") }); },
    onError: (e) => onMessage({ ok: false, text: errorText(e) }),
  });
  return (
    <div className="av-hero-card av-dossier-hero">
      <div className="av-hero-card__hd av-dossier-hero__tags">
        <span className="av-tag"><Plat kind={a.datasource.kind} /></span>
        {schema ? <span className="av-tag">{t("库 {{name}}", { name: schema })}</span> : null}
        {a.kind !== "table" ? <span className="av-tag">{KIND_LABEL[a.kind] ?? a.kind}</span> : null}
        {writeMode ? <span className="av-tag">{WRITE_MODE_LABEL[writeMode] ?? writeMode}<Hint text={t("从导入的脚本推断")} /></span> : null}
        {a.version ? <span className="av-tag">v{a.version.version_no}</span> : null}
        {a.status !== "active" ? <Status value={a.status} /> : null}
        {a.datasource.is_test_adapter ? <Pill tone="warn">{t("测试适配器")}</Pill> : null}
      </div>
      <h1 className="av-mono av-dossier-hero__name">{tableName(a.qualified)}</h1>
      {editing ? (
        <NoteEditor label={t("这张表的一句话说明")} initial={summary.source === "note" ? summary.text ?? "" : ""} busy={save.isPending}
          onCancel={() => setEditing(false)} onSave={(text) => submit(text, false)} />
      ) : summary.text ? (
        <div className="av-dossier-hero__say">
          <p className={state === "stale" ? "is-stale" : undefined}>{summary.text}</p>
          {state === "draft" || state === "stale" ? (
            <div className="av-dossier-hero__note">
              <Pill tone="warn">{state === "draft" ? t("模型草稿 · 待确认") : t("来源变了 · 需重新确认")}</Pill>
              {d.can_edit_notes ? <>
                <button type="button" className="av-btn av-btn--secondary av-btn--sm" disabled={save.isPending} onClick={() => submit(summary.text!, true)}>{t("确认")}</button>
                <button type="button" className="av-btn av-btn--secondary av-btn--sm" onClick={() => setEditing(true)}>{t("改一下")}</button>
              </> : null}
            </div>
          ) : null}
        </div>
      ) : d.can_edit_notes ? (
        <div className="av-dossier-hero__note">
          <button type="button" className="av-btn av-btn--secondary av-btn--sm" onClick={() => setEditing(true)}><Icon name="edit" small />{t("写一句说明")}</button>
        </div>
      ) : null}
      <div className="av-dossier-hero__meta">
        {d.owner ? <><span className="av-avatar" aria-hidden>{initialOf(d.owner.display_name)}</span>
          <span>{t("{{name}} 负责", { name: d.owner.display_name })}</span><span aria-hidden>·</span></> : null}
        <span>{t("{{count}} 个字段", { count })}</span>
      </div>
    </div>
  );
}

function Usage({ d }: { d: AssetDossier }) {
  const tz = useTimeZone();
  const u = usage(d);
  const metrics = d.metrics ?? [];
  const downstream = d.downstream ?? [];
  const last = latestAsker(d.asked.people ?? []);
  return (
    <div className="av-dossier-flow__col" aria-label={t("被这些地方用")} role="group">
      <span className="av-dossier-flow__cap">{t("被这些地方用")}</span>
      {u.metrics ? <div className="av-dossier-node">
        <Tile icon="chart" tone="catalog" size="sm" />
        <span className="av-dossier-row__main"><span>{t("{{count}} 个口径", { count: u.metrics })}</span>
          <span>{metrics.slice(0, 3).map((m) => m.name).join(t("、"))}{metrics.length > 3 ? t(" 等") : ""}</span></span>
      </div> : null}
      {u.asked ? <div className="av-dossier-node">
        <Tile icon="ask" tone="ask" size="sm" />
        <span className="av-dossier-row__main"><span>{t("近 {{days}} 天被问 {{count}} 次", { days: d.asked.days ?? 30, count: u.asked })}</span>
          {last ? <span>{t("最近：{{name}} · {{time}}", { name: last.user.display_name, time: formatShortTime(last.last_asked_at, tz) })}</span> : null}</span>
      </div> : null}
      {u.downstream ? <div className="av-dossier-node">
        <Tile icon="table" tone="collect" size="sm" />
        <span className="av-dossier-row__main"><span>{t("下游 {{count}} 张表", { count: u.downstream })}</span>
          <span className="av-mono" title={downstream.map((x) => x.ref).join("\n")}>{tableName(downstream[0].ref)}{downstream.length > 1 ? t(" 等") : ""}</span></span>
      </div> : null}
      {!u.any ? <span className="av-dossier-flow__none">{t("还没有被口径、提问或下游表用到")}</span> : null}
    </div>
  );
}
