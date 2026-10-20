"""本地战斗策略目录。上传的 Markdown 只是模型输入，不作为代码执行。"""

from __future__ import annotations

import json
import os
from pathlib import Path
from uuid import uuid4

from pydantic import BaseModel, ConfigDict, Field, field_validator

DEFAULT_SKILL_ID = "injured-potion-then-attack"
DEFAULT_SKILL_NAME = "开局喝药、战技优先、伤害魔法循环"
DEFAULT_SKILL_INSTRUCTIONS = (
    "开局满血时先使用一瓶药水；药水固定效力为25，实际回血受生命上限截断。"
    "之后仅在生命不超过上限一半且还有药水时先补药；其余 active 战斗回合按以下优先级选择伤害动作："
    "重击 power_strike 冷却为 0 时优先使用；冷却中依次施放陨石 cast_meteor、"
    "火球 cast_flame、魔法飞弹 cast_spark。三种伤害魔法走完一轮后再从陨石开始，"
    "重击和喝药不重置魔法顺序，进入下一场也沿用顺序。"
    "当前 EP 不够支付轮到的魔法时跳过它，尝试本轮下一种；三种都不能施放时普通攻击 attack。"
    "伤害魔法没有冷却，重击需完成两次其他成功战斗动作才恢复。"
    "当前场获胜且还有下一场时使用 next_battle，继续按当前状态战斗。"
    "最后一场结束或玩家阵亡后再尝试一次攻击，以验证终态不能继续行动；"
    "只试一次，然后结束。"
)
DEFAULT_SKILL_ROOT = Path("artifacts/agent-skills")


class Skill(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    skill_id: str
    name: str = Field(min_length=1, max_length=80)
    instructions: str = Field(min_length=1, max_length=4000)
    built_in: bool = False

    @field_validator("name", "instructions")
    @classmethod
    def non_blank(cls, value: str) -> str:
        value = value.strip()
        if not value or "\x00" in value:
            raise ValueError("名称和内容不得为空，也不能含 NUL 字符")
        return value


DEFAULT_SKILL = Skill(
    skill_id=DEFAULT_SKILL_ID,
    name=DEFAULT_SKILL_NAME,
    instructions=DEFAULT_SKILL_INSTRUCTIONS,
    built_in=True,
)


class SkillStore:
    def __init__(self, root: Path | None = None) -> None:
        self.root = root or Path(os.environ.get("GAMEPILOT_SKILL_ROOT", DEFAULT_SKILL_ROOT))

    def list(self) -> list[Skill]:
        skills = [DEFAULT_SKILL]
        if self.root.exists():
            for path in sorted(self.root.glob("*.json")):
                try:
                    skills.append(Skill.model_validate_json(path.read_text(encoding="utf-8")))
                except (OSError, ValueError):
                    continue
        return skills

    def get(self, skill_id: str) -> Skill | None:
        if skill_id == DEFAULT_SKILL_ID:
            return DEFAULT_SKILL
        try:
            normalized = str(uuid4_from_hex(skill_id))
        except ValueError:
            return None
        path = self.root / f"{normalized}.json"
        if not path.is_file():
            return None
        try:
            skill = Skill.model_validate_json(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return None
        return skill if skill.skill_id == normalized and not skill.built_in else None

    def add(self, name: str, instructions: str) -> Skill:
        skill = Skill(skill_id=str(uuid4()), name=name, instructions=instructions)
        self.root.mkdir(parents=True, exist_ok=True)
        path = self.root / f"{skill.skill_id}.json"
        with path.open("x", encoding="utf-8") as target:
            json.dump(skill.model_dump(), target, ensure_ascii=False)
        return skill


def uuid4_from_hex(value: str):
    """只接收本目录生成的 UUID 文件名，拒绝路径片段。"""
    from uuid import UUID

    parsed = UUID(value)
    if parsed.version != 4 or str(parsed) != value:
        raise ValueError("非法 skill ID")
    return parsed
