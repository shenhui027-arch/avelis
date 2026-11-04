// 资产选择：输入即按服务端搜索（防抖），只取前 20 个匹配项；已选资产按 id 单独取名称。
// 资产随数据规模增长，选择器不整表加载（v1.0.4）。
import { useEffect, useState } from "react";

import { useAssetSearch, useAssetsByIds } from "../api/hooks";
import { SearchSelect } from "./controls";
import { t } from "../i18n";

const LIMIT = 20;
const DEBOUNCE_MS = 250;

/** 输入停顿后再搜索，避免每个按键都请求。 */
export function useDebounced<T>(value: T, ms = DEBOUNCE_MS): T {
  const [settled, setSettled] = useState(value);
  useEffect(() => {
    const timer = setTimeout(() => setSettled(value), ms);
    return () => clearTimeout(timer);
  }, [value, ms]);
  return settled;
}

export function AssetPicker({ value, onChange, queryable, datasourceId, domainId, label, id, invalid, describedBy, disabled,
  placeholder, clearLabel, short }: {
  value: string; onChange: (id: string) => void; queryable?: boolean; datasourceId?: string; domainId?: string;
  label?: string; id?: string; invalid?: boolean; describedBy?: string; disabled?: boolean; placeholder?: string;
  clearLabel?: string; short?: (qualified: string) => string;
}) {
  const [q, setQ] = useState("");
  const term = useDebounced(q.trim());
  const filters = { queryable, datasource_id: datasourceId || undefined, domain_id: domainId || undefined };
  const search = useAssetSearch({ ...filters, q: term || undefined, limit: LIMIT }, !disabled);
  const picked = useAssetsByIds(value ? [value] : []);
  const name = (qualified: string) => (short ? short(qualified) : qualified);
  const current = picked.data?.find((a) => a.id === value);
  return (
    <SearchSelect id={id} value={value} label={label} invalid={invalid} describedBy={describedBy} disabled={disabled} mono
      placeholder={placeholder ?? t("搜索资产")} clearLabel={clearLabel}
      selected={current ? name(current.qualified) : value && picked.isSuccess ? t("资产不可用") : undefined}
      options={(search.data ?? []).map((a) => ({ value: a.id, label: name(a.qualified), hint: a.datasource.name }))}
      loading={search.isFetching} onSearch={setQ} onChange={onChange}
      emptyText={term ? t("没有匹配的资产") : t("暂无资产")} />
  );
}
