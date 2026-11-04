"""Ossie 交换协议固定在代码中，随映射与回归测试一起升级。"""

OSSIE_SPEC_COMMIT = "48da55a"
OSSIE_SPEC_COMMIT_SHA = "48da55a07e4e8c3a11a662b14727557f6342536d"
OSSIE_SPEC_VERSION = "0.2.0.dev0"

# Four times the 5 MiB upload limit leaves room for per-version governance metadata,
# while bounding duplication of shared catalogue/extension data before any writes.
MAX_RETAINED_METADATA_BYTES = 20 * 1024 * 1024
