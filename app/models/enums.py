import enum


class EmploymentType(str, enum.Enum):
    fte = "fte"
    contractor = "contractor"
    intern = "intern"


class AvailabilityStatus(str, enum.Enum):
    available = "available"
    away = "away"
    restricted = "restricted"


class SkillCategory(str, enum.Enum):
    technical = "technical"
    language = "language"
    domain = "domain"


class SkillLevel(str, enum.Enum):
    learning = "Learning"
    working = "Working"
    expert = "Expert"


class SkillSource(str, enum.Enum):
    inferred = "inferred"
    self_reported = "self"
    confirmed = "confirmed"
    certified = "certified"


class ProjectType(str, enum.Enum):
    project = "project"
    system = "system"
    function = "function"
    policy = "policy"


class ProjectClassification(str, enum.Enum):
    public = "public"
    internal = "internal"
    confidential = "confidential"
