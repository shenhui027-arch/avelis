// 档案页的一排等高卡片：在用的口径、最近谁问过、最近结构变化。没有数据的卡片整张不渲染。
import type { ReactNode } from "react";
import { Link } from "react-router";

import type { Schemas } from "../../../api/client";
import { useTimeZone, type AssetDossier } from "../../../api/hooks";
import { Pill, Status, Tile, type KitTone } from "../../../components/ui";
import { formatShortTime } from "../../../format";
import { t } from "../../../i18n";
import { Actor, EVENT_LABEL, recordDetail, recordLook } from "../../changes/ChangeRecords";
import { initialOf } from "./dossier";

type ChangeRecord = Schemas["ChangeRecordOut"];

/** 全部变更：变更页的“记录”视图；带上 asset_id 时记录页以这张表为初始筛选。 */
const CHANGE_RECORDS = "/changes?view=records";

export function DossierCards({ d, changes }: { d: AssetDossier; changes: ChangeRecord[] }) {
  const metrics = d.metrics ?? [];
  const people = d.asked.people ?? [];
  if (!metrics.length && !people.length && !changes.length) return null;
  return (
    <div className="av-dossier-cards">
      {metrics.length ? <MetricsCard d={d} /> : null}
      {people.length ? <AskedCard d={d} /> : null}
      {changes.length ? <ChangesCard items={changes} /> : null}
    </div>
  );
}

function Card({ tone, icon, title, aside, children }: { tone: KitTone; icon: string; title: string; aside?: ReactNode; children: ReactNode }) {
  return (
    <section className={`av-panel av-stage tone-${tone} av-dossier-card`} aria-label={title}>
      <div className="av-dossier-card__hd">
        <h2><Tile icon={icon} tone={tone} size="sm" />{title}</h2>
        {aside ? <span className="av-dossier-card__aside">{aside}</span> : null}
      </div>
      <div className="av-dossier-list">{children}</div>
    </section>
  );
}

function MetricsCard({ d }: { d: AssetDossier }) {
  const metrics = d.metrics ?? [];
  return (
    <Card tone="catalog" icon="chart" title={t("在用的口径")} aside={t("{{count}} 个", { count: metrics.length })}>
      {metrics.map((m) => (
        <Link key={m.version_id} className="av-dossier-row av-dossier-row--link" to={`/catalog/metrics/${m.id}`}>
          <span className="av-dossier-row__main"><span>{m.name}</span><span>{m.description || m.code}</span></span>
          <Status value={m.status} />
        </Link>
      ))}
    </Card>
  );
}

function AskedCard({ d }: { d: AssetDossier }) {
  const tz = useTimeZone();
  const people = d.asked.people ?? [];
  return (
    <Card tone="ask" icon="ask" title={t("最近谁问过")} aside={t("近 {{days}} 天 {{count}} 次", { days: d.asked.days ?? 30, count: d.asked.total ?? 0 })}>
      {people.map((p) => (
        <div key={p.user.id} className="av-dossier-row">
          <span className="av-avatar tone-ask av-dossier-avatar" aria-hidden>{initialOf(p.user.display_name)}</span>
          <span className="av-dossier-row__main"><span>{p.user.display_name}</span>
            {p.last_question ? <span title={t("问过 {{count}} 次", { count: p.count })}>{p.last_question}</span>
              : <span>{t("问过 {{count}} 次", { count: p.count })}</span>}</span>
          <span className="av-dossier-row__time">{formatShortTime(p.last_asked_at, tz)}</span>
        </div>
      ))}
    </Card>
  );
}

function ChangesCard({ items }: { items: ChangeRecord[] }) {
  const tz = useTimeZone();
  return (
    <Card tone="change" icon="change" title={t("最近结构变化")}
      aside={<Link className="av-btn av-btn--ghost av-btn--sm"
        to={items[0]?.asset_id ? `${CHANGE_RECORDS}&asset_id=${items[0].asset_id}` : CHANGE_RECORDS}>{t("全部变更")}</Link>}>
      {items.map((e) => {
        const [icon, tone] = recordLook(e.kind);
        const detail = recordDetail(e);
        return (
          <div key={e.id} className="av-dossier-row">
            <Tile icon={icon} tone={tone} size="sm" />
            <span className="av-dossier-row__main">
              <span className="av-dossier-row__title"><Pill>{EVENT_LABEL[e.kind] ?? e.kind}</Pill>{e.column ? <span className="av-mono">{e.column}</span> : null}</span>
              <span className="av-dossier-row__sub">{detail ? <span className="av-mono">{detail}</span> : null}<Actor e={e} /></span>
            </span>
            <span className="av-dossier-row__time">{formatShortTime(e.detected_at, tz)}</span>
          </div>
        );
      })}
    </Card>
  );
}
