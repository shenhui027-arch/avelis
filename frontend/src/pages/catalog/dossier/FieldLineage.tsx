// 每个字段怎么来的：来源字段 → 本表字段 → 一句人话说明（表达式收起）。说明可由模型起草，负责人确认或改写（ADR 0022）。
import { useState } from "react";
import { Link } from "react-router";

import { api, unwrap } from "../../../api/client";
import { errorText, useAction, type AssetDossier } from "../../../api/hooks";
import { Banner, Icon, Pill, RELATION_LABEL, Tile, useMessage } from "../../../components/ui";
import { t } from "../../../i18n";
import { needsConfirm, noteCounts, noteState, sourceLabel, visible } from "./dossier";
import { NoteEditor, noteRevision, useSaveNote, type NoteSeen } from "./notes";

type Field = NonNullable<AssetDossier["fields"]>[number];

const FIELD_LIMIT = 12;

const SKIPPED: Record<string, string> = {
  no_model: t("还没有可用的模型，请管理员在模型配置里登记"),
  policy: t("已关闭向模型发送代码片段，不能生成说明"),
  nothing: t("没有需要补写的字段"),
};

export function FieldLineage({ assetId, d }: { assetId: string; d: AssetDossier }) {
  const fields = d.fields ?? [];
  const [expanded, setExpanded] = useState(false);
  const [editing, setEditing] = useState<string | null>(null);
  const [message, setMessage] = useMessage();
  const save = useSaveNote(assetId);
  const generate = useAction(() => unwrap(api.POST("/api/assets/{asset_id}/lineage-notes/generate", {
    params: { path: { asset_id: assetId } }, body: { summary: true } })), ["dossier"]);
  if (!fields.length) return null;
  const counts = noteCounts(fields);
  const { shown, hidden } = visible(fields, FIELD_LIMIT, expanded);
  const canEdit = !!d.can_edit_notes;

  // 确认的是页面上这一版：带上字段当前显示的来历版本与说明修订，来源在此期间变了后端会拒绝
  const saveNote = (column: string, text: string, confirm: boolean) => save.mutate({ column, text, seen: seenOf(fields, column) }, {
    onSuccess: () => {
      setEditing(null);
      setMessage({ ok: true, text: confirm ? t("已确认 {{name}} 的说明", { name: column }) : t("已保存 {{name}} 的说明", { name: column }) });
    },
    onError: (e) => setMessage({ ok: false, text: errorText(e) }),
  });
  const runGenerate = () => generate.mutate(undefined, {
    onSuccess: (r) => {
      if (r.skipped) setMessage({ ok: r.skipped === "nothing", text: SKIPPED[r.skipped] ?? r.skipped });
      else if (r.notes?.length) setMessage({ ok: true, text: t("模型写了 {{count}} 条草稿，确认后才算数", { count: r.notes.length }) });
      else setMessage({ ok: true, text: SKIPPED.nothing });
    },
    onError: (e) => setMessage({ ok: false, text: errorText(e) }),
  });

  return (
    <section className="av-panel av-stage tone-lineage av-dossier-card av-dossier-lineage" aria-label={t("每个字段怎么来的")}>
      <div className="av-dossier-card__hd">
        <h2><Tile icon="lineage" tone="lineage" size="sm" />{t("每个字段怎么来的")}</h2>
        <span className="av-dossier-card__aside">
          {counts.confirmed ? <Pill tone="ok">{t("{{count}} 已确认", { count: counts.confirmed })}</Pill> : null}
          {counts.pending ? <Pill tone="warn">{t("{{count}} 待确认", { count: counts.pending })}</Pill> : null}
          {canEdit && d.model_available ? (
            <button type="button" className="av-btn av-btn--secondary av-btn--sm" disabled={generate.isPending} onClick={runGenerate}>
              <Icon name="edit" small />{generate.isPending ? t("正在补写…") : t("让模型补写说明")}</button>
          ) : null}
        </span>
      </div>
      <Banner message={message} />
      <table className="av-table av-dossier-lineage__table">
        <thead><tr>
          <th className="av-dossier-lineage__src">{t("来源字段")}</th>
          <th className="av-dossier-lineage__arrow"><span className="av-dossier-sr">→</span></th>
          <th className="av-dossier-lineage__col">{t("本表字段")}</th>
          <th>{t("怎么加工")}</th>
        </tr></thead>
        <tbody>
          {shown.map((f) => (
            <FieldRow key={f.column} f={f} canEdit={canEdit} editing={editing === f.column} busy={save.isPending}
              onEdit={() => setEditing(f.column)} onCancel={() => setEditing(null)} onSave={saveNote} />
          ))}
        </tbody>
      </table>
      {hidden ? (
        <div className="av-dossier-lineage__more">
          <button type="button" className="av-btn av-btn--ghost av-btn--sm" onClick={() => setExpanded(true)}>
            {t("显示全部 {{count}} 个字段", { count: fields.length })}</button>
        </div>
      ) : null}
    </section>
  );
}

function FieldRow({ f, canEdit, editing, busy, onEdit, onCancel, onSave }: {
  f: Field; canEdit: boolean; editing: boolean; busy: boolean;
  onEdit: () => void; onCancel: () => void; onSave: (column: string, text: string, confirm: boolean) => void;
}) {
  const note = f.note ?? null;
  const state = noteState(note);
  const sources = f.sources ?? [];
  return (
    <tr>
      <td className="av-dossier-lineage__src">
        {sources.length ? (
          <span className="av-dossier-src">{sources.map((s, i) => (
            <span key={`${s.ref}.${s.column ?? ""}-${i}`} className="av-dossier-src__item">
              <i aria-hidden />
              {s.asset_id ? <Link className="av-mono" to={`/catalog/assets/${s.asset_id}`} title={s.ref}>{sourceLabel(s)}</Link>
                : <span className="av-mono" title={s.ref}>{sourceLabel(s)}</span>}
            </span>
          ))}</span>
        ) : <span className="av-muted">—</span>}
      </td>
      <td className="av-dossier-lineage__arrow" aria-hidden><Icon name="arrow" small /></td>
      <td className="av-dossier-lineage__col">
        <span className="av-dossier-row__main"><span className="av-mono">{f.column}</span>{f.comment ? <span>{f.comment}</span> : null}</span>
      </td>
      <td className="av-dossier-lineage__how">
        {editing ? (
          <NoteEditor label={t("{{name}} 怎么加工", { name: f.column })} initial={note?.text ?? ""} busy={busy}
            onCancel={onCancel} onSave={(text) => onSave(f.column, text, false)} />
        ) : <>
          {note ? <p className={state === "stale" ? "is-stale" : undefined}>{note.text}</p> : null}
          <div className="av-dossier-lineage__meta">
            {f.relation ? <span className="av-tag">{RELATION_LABEL[f.relation] ?? f.relation}</span> : null}
            {state === "confirmed" ? <Pill tone="ok">{note?.confirmed_by ? t("{{name}} 已确认", { name: note.confirmed_by.display_name }) : t("已确认")}</Pill> : null}
            {state === "draft" ? <Pill tone="warn">{t("模型草稿 · 待确认")}</Pill> : null}
            {state === "stale" ? <Pill tone="warn">{t("来源变了 · 需重新确认")}</Pill> : null}
            {state === "none" ? <span className="av-muted">{t("还没有说明")}</span> : null}
            {canEdit && needsConfirm(note) ? <>
              <button type="button" className="av-btn av-btn--primary av-btn--sm" disabled={busy} onClick={() => onSave(f.column, note!.text, true)}
                aria-label={t("确认 {{name}} 的说明", { name: f.column })}>{t("确认")}</button>
              <button type="button" className="av-btn av-btn--secondary av-btn--sm" onClick={onEdit}
                aria-label={t("改一下 {{name}} 的说明", { name: f.column })}>{t("改一下")}</button>
            </> : null}
            {canEdit && state === "confirmed" ? (
              <button type="button" className="av-btn av-btn--ghost av-btn--sm" onClick={onEdit}
                aria-label={t("改一下 {{name}} 的说明", { name: f.column })}>{t("改一下")}</button>
            ) : null}
            {canEdit && state === "none" ? (
              <button type="button" className="av-btn av-btn--secondary av-btn--sm" onClick={onEdit}
                aria-label={t("给 {{name}} 写说明", { name: f.column })}>{t("写说明")}</button>
            ) : null}
            {f.expression ? (
              <details className="av-dossier-details">
                <summary>{t("看表达式")}</summary>
                <code className="av-dossier-expr">{f.expression}</code>
              </details>
            ) : null}
          </div>
        </>}
      </td>
    </tr>
  );
}

/** 这一行此刻显示的版本（来历与说明修订），随确认请求带回。 */
function seenOf(fields: NonNullable<AssetDossier["fields"]>, column: string): NoteSeen {
  const f = fields.find((item) => item.column === column);
  return { basisHash: f?.basis_hash ?? null, updatedAt: noteRevision(f?.note) };
}
