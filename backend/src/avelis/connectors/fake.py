"""测试适配器：返回给定的快照，不连接任何数据库。界面与数据中标记为“测试适配器”。"""

from __future__ import annotations

from typing import Any

from avelis.connectors.base import ObservedAsset, ObservedColumn, Snapshot


class FakeConnector:
    kind = "fake"
    dialect = "starrocks"
    is_test_adapter = True

    def __init__(self, options: dict[str, Any] | None = None):
        self.options = options or {}

    def server_identity(self) -> dict[str, Any]:
        return {"adapter": "fake", "endpoint": self.options.get("endpoint", "fake:0")}

    def snapshot(self) -> Snapshot:
        assets = []
        for spec in self.options.get("assets", []):
            columns = [ObservedColumn(**{"ordinal": i + 1, **c}) for i, c in enumerate(spec.get("columns", []))]
            assets.append(ObservedAsset(**{**spec, "columns": columns}))
        return Snapshot(server_identity=self.server_identity(), assets=assets)
