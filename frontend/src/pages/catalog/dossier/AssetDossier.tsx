// 资产档案页签（ADR 0022）：一排等高卡片（口径、提问、结构变化）+ 字段血缘表 + 产出方式。没有数据的部分整块不显示。
import type { UseQueryResult } from "@tanstack/react-query";

import { useAssetChangeEvents, type AssetDetail, type AssetDossier as Dossier } from "../../../api/hooks";
import { Empty, QueryState } from "../../../components/ui";
import { t } from "../../../i18n";
import { DossierCards } from "./DossierCards";
import { FieldLineage } from "./FieldLineage";
import { ProducedBy } from "./ProducedBy";

export function AssetDossier({ a, dossier }: { a: AssetDetail; dossier: UseQueryResult<Dossier> }) {
  const changes = useAssetChangeEvents(a.id);
  return (
    <QueryState query={dossier} rows={4}>{(d) => {
      const items = changes.data?.items ?? [];
      const empty = !(d.metrics?.length || d.asked.people?.length || items.length || d.fields?.length || d.produced);
      return (
        <div className="av-dossier">
          <DossierCards d={d} changes={items} />
          <FieldLineage assetId={a.id} d={d} />
          <ProducedBy a={a} d={d} />
          {empty && !changes.isPending ? <Empty compact text={t("还没有采集到这张表的字段来源、口径或提问记录")} /> : null}
        </div>
      );
    }}</QueryState>
  );
}
