"""项目数据的创建、读取和保存。"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .schema import new_project_dict


class ProjectFileError(ValueError):
    """项目文件不存在、无法解析或根节点类型错误。"""


@dataclass
class ProjectData:
    """统一的建筑项目数据容器。"""

    data: dict[str, Any]
    source_path: Path | None = None

    @classmethod
    def empty(cls) -> "ProjectData":
        return cls(new_project_dict())

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "ProjectData":
        if not isinstance(data, dict):
            raise ProjectFileError("项目数据的根节点必须是对象。")
        return cls(dict(data))

    @classmethod
    def from_json(cls, path: Path | str) -> "ProjectData":
        project_path = Path(path)
        if not project_path.is_file():
            raise ProjectFileError(f"找不到项目文件：{project_path}")
        try:
            raw = json.loads(project_path.read_text(encoding="utf-8-sig"))
        except UnicodeDecodeError as exc:
            raise ProjectFileError("项目文件必须使用UTF-8编码。") from exc
        except json.JSONDecodeError as exc:
            raise ProjectFileError(
                f"项目JSON格式错误：第{exc.lineno}行，第{exc.colno}列。"
            ) from exc
        if not isinstance(raw, dict):
            raise ProjectFileError("项目数据的根节点必须是对象。")
        return cls(raw, project_path.resolve())

    def to_json(self, path: Path | str) -> Path:
        output_path = Path(path)
        output_path.parent.mkdir(parents=True, exist_ok=True)
        output_path.write_text(
            json.dumps(self.data, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
        self.source_path = output_path.resolve()
        return output_path


def create_empty_project() -> ProjectData:
    return ProjectData.empty()


def load_project(path: Path | str) -> ProjectData:
    return ProjectData.from_json(path)


def save_project(project: ProjectData, path: Path | str) -> Path:
    return project.to_json(path)
