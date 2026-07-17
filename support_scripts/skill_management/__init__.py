"""Public facade for Agent Kaizen's explicit, project-scoped skill management.

The library is stdlib-only. Inspection and plan builders are read-only; every
mutating function recomputes its plan and requires the matching SHA-256.
"""

from .core import SkillManagementError, plan_sha256
from .discovery import discover_skills
from .indexing import (
    apply_authoring_index_plan,
    apply_index_plan,
    apply_store_index_plan,
    build_authoring_index_plan,
    build_index_plan,
    build_master_text,
    build_store_index_plan,
    derive_covers,
    index_status,
)
from .links import apply_link_plan, build_link_plan, link_status
from .policy import (
    apply_policy_plan,
    build_policy_plan,
    build_policy_restore_plan,
    policy_status,
    restore_policy,
)
from .status import validation_status
from .validation import package_sha256, validate_skill_package

__all__ = [
    "SkillManagementError",
    "apply_index_plan",
    "apply_authoring_index_plan",
    "apply_store_index_plan",
    "apply_link_plan",
    "apply_policy_plan",
    "build_index_plan",
    "build_authoring_index_plan",
    "build_master_text",
    "build_store_index_plan",
    "build_link_plan",
    "build_policy_plan",
    "build_policy_restore_plan",
    "discover_skills",
    "derive_covers",
    "index_status",
    "link_status",
    "package_sha256",
    "plan_sha256",
    "policy_status",
    "restore_policy",
    "validate_skill_package",
    "validation_status",
]
