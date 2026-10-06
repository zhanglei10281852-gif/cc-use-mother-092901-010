"""准入评估服务的异常类型。"""


class AdmissionDomainError(ValueError):
    """领域规则违反。"""


class SchemeConflictError(AdmissionDomainError):
    """方案发布与既有版本冲突。"""


class UnknownSchemeError(AdmissionDomainError):
    """引用了不存在的方案或版本。"""


class ProposalBlockedError(AdmissionDomainError):
    """候选结论未达到可提出阶段决定的条件。"""


class InvalidCommitteeActionError(AdmissionDomainError):
    """委员会动作本身非法（无权、重复会签等）。"""


class DecisionClosedError(InvalidCommitteeActionError):
    """决定已终态（已签发/已驳回/已撤销），不可再追加动作。"""


class DuplicateCosignError(InvalidCommitteeActionError):
    """同一委员重复会签。"""
