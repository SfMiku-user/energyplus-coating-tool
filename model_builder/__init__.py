"""建筑参数到 EnergyPlus 模型的生成模块。"""

from .excel_reader import (
    ExcelReadError,
    ExcelValidationError,
    convert_excel_to_json,
    load_project_from_excel,
)
from .coating_builder import (
    CoatingBuildError,
    CoatingBuildResult,
    build_coating_scenario,
    validate_coating_pair,
)
from .idf_versioning import (
    IDFVersion,
    IDFVersionError,
    find_transition_program,
    prepare_idf_for_energyplus,
    read_energyplus_version,
    read_idf_version,
)
from .geometry_quality import assess_geometry
from .openstudio_builder import (
    OpenStudioBuildError,
    OpenStudioBuildResult,
    build_openstudio_model,
)
from .project import ProjectData, create_empty_project, load_project, save_project
from .results_builder import (
    ResultsBuildError,
    ResultsBuildResult,
    build_energy_comparison,
)
from .validation import (
    ValidationIssue,
    format_validation_report,
    save_validation_report,
    validate_project,
)
from .workflow import WorkflowError, WorkflowResult, run_project_workflow

__all__ = [
    "ExcelReadError",
    "ExcelValidationError",
    "CoatingBuildError",
    "CoatingBuildResult",
    "IDFVersion",
    "IDFVersionError",
    "OpenStudioBuildError",
    "OpenStudioBuildResult",
    "ProjectData",
    "ResultsBuildError",
    "ResultsBuildResult",
    "ValidationIssue",
    "WorkflowError",
    "WorkflowResult",
    "assess_geometry",
    "convert_excel_to_json",
    "build_openstudio_model",
    "build_coating_scenario",
    "build_energy_comparison",
    "create_empty_project",
    "format_validation_report",
    "find_transition_program",
    "load_project_from_excel",
    "load_project",
    "prepare_idf_for_energyplus",
    "read_energyplus_version",
    "read_idf_version",
    "run_project_workflow",
    "save_project",
    "save_validation_report",
    "validate_project",
    "validate_coating_pair",
]
