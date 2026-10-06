"""商业化运营准入评估服务。

公共 API：
- 领域对象：:mod:`readiness_assessment.domain`
- 版本化方案：:class:`~readiness_assessment.scheme.AssessmentScheme` /
  :class:`~readiness_assessment.scheme.SchemeRegistry`
- 评估引擎：:func:`readiness_assessment.engine.evaluate`
- 委员会决定：:class:`~readiness_assessment.decisions.DecisionStore`
- 服务门面：:class:`readiness_assessment.service.AdmissionService`
"""

from .contracts import AssessmentMetric, DecisionStage, MetricWindow
from .decisions import DecisionStore, DecisionView
from .domain import (
    Evidence,
    ExceptionRecord,
    Exemption,
    InfrastructureCondition,
    ItemStatus,
    MetricDefinition,
    MetricDirection,
    Observation,
    OperatingRegion,
    RemediationCommitment,
    SafetyEvent,
    Severity,
    Blocker,
    VehicleCapability,
    VehicleProfile,
)
from .engine import CandidateConclusion, EvaluationInputs, evaluate
from .scheme import AssessmentScheme, SchemeRegistry
from .service import AdmissionService

__all__ = [
    "AssessmentMetric",
    "DecisionStage",
    "MetricWindow",
    "DecisionStore",
    "DecisionView",
    "Evidence",
    "ExceptionRecord",
    "Exemption",
    "InfrastructureCondition",
    "ItemStatus",
    "MetricDefinition",
    "MetricDirection",
    "Observation",
    "OperatingRegion",
    "RemediationCommitment",
    "SafetyEvent",
    "Severity",
    "Blocker",
    "VehicleCapability",
    "VehicleProfile",
    "CandidateConclusion",
    "EvaluationInputs",
    "evaluate",
    "AssessmentScheme",
    "SchemeRegistry",
    "AdmissionService",
]
