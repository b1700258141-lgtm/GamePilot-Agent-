"""缺陷单和回归规格的数据契约，不包含原始 HTTP body 或模型文本。"""

from typing import Literal

from pydantic import BaseModel, ConfigDict

from gamepilot.testing.models import ActionName, Scenario

DEFECT_SCHEMA_VERSION = "1.0"


class DefectModel(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)


class SourceRef(DefectModel):
    run_id: str
    path: str
    sha256: str


class DefectArtifact(DefectModel):
    """一份文件同时保存人可读缺陷单与可执行回归场景。"""

    schema_version: Literal["1.0"] = DEFECT_SCHEMA_VERSION
    fingerprint: str
    title: str
    rule_id: str
    rules_version: str
    step: int
    expected: str
    actual: str
    seed: int
    actions: list[ActionName]
    reproduction: list[str]
    source_run: SourceRef
    source_replay: SourceRef
    source_agent: SourceRef | None = None
    regression: Scenario
