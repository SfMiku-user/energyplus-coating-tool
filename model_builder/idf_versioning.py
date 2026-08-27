"""EnergyPlus IDF 版本识别与安全转换。"""

from __future__ import annotations

import re
import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path


_IDF_VERSION_RE = re.compile(
    r"(?:\A|;)\s*Version\s*,\s*(\d+(?:\.\d+){1,2})\s*;",
    re.IGNORECASE | re.DOTALL,
)
_ENERGYPLUS_VERSION_RE = re.compile(
    r"Version\s+(\d+\.\d+(?:\.\d+)?)",
    re.IGNORECASE,
)


class IDFVersionError(RuntimeError):
    """IDF 版本无法识别、转换程序缺失或转换失败。"""


@dataclass(frozen=True, order=True)
class IDFVersion:
    major: int
    minor: int
    patch: int = 0

    @classmethod
    def parse(cls, value: str) -> "IDFVersion":
        parts = value.strip().split(".")
        if len(parts) not in {2, 3} or any(not part.isdigit() for part in parts):
            raise IDFVersionError(f"无法识别版本号：{value!r}")
        numbers = [int(part) for part in parts]
        if len(numbers) == 2:
            numbers.append(0)
        return cls(*numbers)

    @property
    def idf_text(self) -> str:
        return f"{self.major}.{self.minor}"

    @property
    def transition_tag(self) -> str:
        return f"V{self.major}-{self.minor}-{self.patch}"

    def __str__(self) -> str:
        return f"{self.major}.{self.minor}.{self.patch}"


def _strip_idf_comments(text: str) -> str:
    return "\n".join(line.split("!", 1)[0] for line in text.splitlines())


def read_idf_version(path: Path | str) -> IDFVersion:
    """读取 IDF 的 ``Version`` 对象。"""

    idf_path = Path(path)
    if not idf_path.is_file():
        raise IDFVersionError(f"找不到 IDF 文件：{idf_path}")
    try:
        text = idf_path.read_text(encoding="utf-8-sig")
    except UnicodeDecodeError:
        text = idf_path.read_text(encoding="gb18030")
    match = _IDF_VERSION_RE.search(_strip_idf_comments(text))
    if not match:
        raise IDFVersionError(f"IDF 中缺少可识别的 Version 对象：{idf_path}")
    return IDFVersion.parse(match.group(1))


def read_energyplus_version(executable: Path | str) -> IDFVersion:
    """调用 ``energyplus --version`` 并读取主、次、修订版本。"""

    program = Path(executable)
    if not program.is_file():
        raise IDFVersionError(f"找不到 EnergyPlus 程序：{program}")
    try:
        completed = subprocess.run(
            [str(program), "--version"],
            cwd=program.parent,
            capture_output=True,
            text=True,
            errors="replace",
            timeout=20,
        )
    except OSError as exc:
        raise IDFVersionError(f"无法启动 EnergyPlus：{program}") from exc
    output = f"{completed.stdout}\n{completed.stderr}"
    match = _ENERGYPLUS_VERSION_RE.search(output)
    if completed.returncode != 0 or not match:
        raise IDFVersionError(
            "无法读取 EnergyPlus 版本。\n" + output.strip()
        )
    return IDFVersion.parse(match.group(1))


def find_transition_program(
    energyplus_executable: Path | str,
    source_version: IDFVersion,
    target_version: IDFVersion,
) -> Path:
    """定位 EnergyPlus 安装目录中的指定版本转换程序。"""

    energyplus = Path(energyplus_executable)
    updater_dir = energyplus.parent / "PreProcess" / "IDFVersionUpdater"
    filename = (
        f"Transition-{source_version.transition_tag}-to-"
        f"{target_version.transition_tag}.exe"
    )
    program = updater_dir / filename
    if not program.is_file():
        raise IDFVersionError(
            f"未找到 {source_version.idf_text}→{target_version.idf_text} "
            f"转换程序：{program}"
        )
    for version in (source_version, target_version):
        idd = updater_dir / f"{version.transition_tag}-Energy+.idd"
        if not idd.is_file():
            raise IDFVersionError(f"版本转换缺少配套 IDD：{idd}")
    return program


def prepare_idf_for_energyplus(
    source_idf: Path | str,
    energyplus_executable: Path | str,
    work_dir: Path | str,
) -> tuple[Path, dict[str, object]]:
    """返回与目标 EnergyPlus 兼容的 IDF，必要时转换独立副本。

    输入 IDF 永远不会被修改。转换器必须从其所在目录启动，否则官方
    Transition 程序无法找到同目录下的版本 IDD 文件。
    """

    source = Path(source_idf)
    energyplus = Path(energyplus_executable)
    source_version = read_idf_version(source)
    target_version = read_energyplus_version(energyplus)
    details: dict[str, object] = {
        "source_idf": str(source.resolve()),
        "source_version": str(source_version),
        "target_version": str(target_version),
        "converted": False,
    }
    if (source_version.major, source_version.minor) == (
        target_version.major,
        target_version.minor,
    ):
        details["compatible_idf"] = str(source.resolve())
        return source.resolve(), details
    if source_version > target_version:
        raise IDFVersionError(
            f"IDF 版本 {source_version.idf_text} 高于 EnergyPlus "
            f"{target_version.idf_text}，不支持向下转换。"
        )

    transition = find_transition_program(energyplus, source_version, target_version)
    conversion_dir = Path(work_dir)
    conversion_dir.mkdir(parents=True, exist_ok=True)
    converted = conversion_dir / (
        f"source_v{source_version.major}_{source_version.minor}"
        f"_to_v{target_version.major}_{target_version.minor}.idf"
    )
    shutil.copy2(source, converted)
    try:
        completed = subprocess.run(
            [str(transition), str(converted.resolve())],
            cwd=transition.parent,
            capture_output=True,
            text=True,
            errors="replace",
            timeout=180,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise IDFVersionError(
            f"无法执行 IDF 版本转换程序：{transition}"
        ) from exc

    log_path = conversion_dir / "transition.log"
    log_path.write_text(
        "命令：" + " ".join([str(transition), str(converted.resolve())]) + "\n\n"
        + completed.stdout
        + ("\n[stderr]\n" + completed.stderr if completed.stderr else ""),
        encoding="utf-8",
    )
    if completed.returncode != 0:
        raise IDFVersionError(
            f"IDF {source_version.idf_text}→{target_version.idf_text} 转换失败"
            f"（返回码 {completed.returncode}）。详见：{log_path}"
        )
    converted_version = read_idf_version(converted)
    if (converted_version.major, converted_version.minor) != (
        target_version.major,
        target_version.minor,
    ):
        raise IDFVersionError(
            f"转换后的 IDF 版本为 {converted_version.idf_text}，"
            f"预期为 {target_version.idf_text}。详见：{log_path}"
        )

    details.update(
        {
            "converted": True,
            "compatible_idf": str(converted.resolve()),
            "transition_program": str(transition.resolve()),
            "transition_log": str(log_path.resolve()),
        }
    )
    return converted, details
