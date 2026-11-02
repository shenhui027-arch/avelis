"""Ossie 导入结果与请求契约；保留逐条校验信息供前端展示。"""

from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field


class OssieImportResult(BaseModel):
    model_config = ConfigDict(extra="allow")

    name: str
    action: Literal["created", "new_version", "skipped", "failed"]
    metric_id: str | None = None
    version_id: str | None = None
    version_no: int | None = None
    reason: str | None = None


class OssieImportError(BaseModel):
    layer: str
    path: str
    reason: str


class OssieImportWarning(BaseModel):
    metric: str
    path: str
    reason: str


class OssieImportCounts(BaseModel):
    created: int = Field(default=0, ge=0)
    new_version: int = Field(default=0, ge=0)
    skipped: int = Field(default=0, ge=0)
    failed: int = Field(default=0, ge=0)


class OssieImportReport(BaseModel):
    model_config = ConfigDict(extra="allow")

    ok: bool
    dry_run: bool
    results: list[OssieImportResult] = Field(default_factory=list)
    errors: list[OssieImportError] = Field(default_factory=list)
    warnings: list[OssieImportWarning] = Field(default_factory=list)
    counts: OssieImportCounts = Field(default_factory=OssieImportCounts)


class OssieImportRequest(BaseModel):
    """JSON 包装请求；也接受以查询参数指定选项的根模型和上传文件。"""

    domain_id: str
    dry_run: bool = True
    document: dict[str, Any] | str
