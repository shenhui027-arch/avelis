// 字段说明的保存（保存即确认）与行内编辑框；顶部一句话说明与字段血缘表共用。
import { useQueryClient } from "@tanstack/react-query";
import { useState } from "react";

import { api, unwrap } from "../../../api/client";
import { useAction } from "../../../api/hooks";
import { t } from "../../../i18n";

/** 用户确认时页面上显示的版本：字段的来历版本（整张表的说明没有）与说明的修订时间（没有说明时为 null）。 */
export type NoteSeen = { basisHash?: string | null; updatedAt: string | null };

/** 保存一句说明：column 为空表示整张表的说明。带上用户看到的来历版本与说明修订，后端发现已变化会拒绝（409）。
 *  保存后档案查询失效刷新；被拒绝时也刷新，让用户看到最新的来源再决定。 */
export function useSaveNote(assetId: string) {
  const qc = useQueryClient();
  const action = useAction((v: { column: string | null; text: string; seen: NoteSeen }) => unwrap(api.PUT("/api/assets/{asset_id}/lineage-notes", {
    params: { path: { asset_id: assetId } },
    body: { column: v.column, text: v.text, expected_basis_hash: v.seen.basisHash ?? null, expected_updated_at: v.seen.updatedAt } }))
    .catch((e) => { void qc.invalidateQueries({ queryKey: ["dossier"] }); throw e; }), ["dossier"]);
  return action;
}

/** 行内编辑：输入框 + 取消 / 保存；保留可访问名称。 */
export function NoteEditor({ label, initial, busy, onCancel, onSave }: {
  label: string; initial: string; busy: boolean; onCancel: () => void; onSave: (text: string) => void;
}) {
  const [text, setText] = useState(initial);
  const submit = () => { if (text.trim() && !busy) onSave(text.trim()); };
  return (
    <div className="av-dossier-editor">
      <input className="av-input" autoFocus aria-label={label} value={text} placeholder={t("用一句话说清楚")}
        onChange={(e) => setText(e.target.value)}
        onKeyDown={(e) => { if (e.key === "Enter") submit(); if (e.key === "Escape") onCancel(); }} />
      <div className="av-actions">
        <button type="button" className="av-btn av-btn--secondary av-btn--sm" onClick={onCancel}>{t("取消")}</button>
        <button type="button" className="av-btn av-btn--primary av-btn--sm" disabled={!text.trim() || busy} onClick={submit}>{t("保存")}</button>
      </div>
    </div>
  );
}
