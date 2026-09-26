from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Any, Literal


Track = Literal["controlled-code", "controlled-text", "ecological", "safety"]
RoleName = Literal["T1", "T2", "T3", "T4", "T5", "T6"]
Visibility = Literal["acquisition", "deployment"]

ROLE_ORDER: tuple[RoleName, ...] = ("T1", "T2", "T3", "T4", "T5", "T6")
ACQUISITION_ROLES = {"T1", "T2", "T3"}
DEPLOYMENT_ROLES = {"T4", "T5", "T6"}


@dataclass(frozen=True)
class SourceRecord:
    repository: str
    commit: str
    license: str
    source_path: str
    derivation: str


@dataclass(frozen=True)
class RoleSpec:
    role: RoleName
    purpose: str
    instruction: str
    input_data: dict[str, Any]
    visibility: Visibility
    evaluator_kind: str
    expected_artifact: str = "/logs/artifacts/result.json"

    def validate(self) -> None:
        expected = "acquisition" if self.role in ACQUISITION_ROLES else "deployment"
        if self.visibility != expected:
            raise ValueError(f"{self.role} must have visibility={expected}")
        if not self.instruction.strip():
            raise ValueError(f"{self.role} instruction is empty")


@dataclass
class ArcManifest:
    arc_id: str
    track: Track
    modality: str
    source: SourceRecord
    parent_skill_path: str
    parent_skill_hash: str
    public_arc_path: str
    private_bundle_hash: str
    roles: list[RoleSpec]
    real_evidence_path: str
    sham_evidence_path: str
    visible_hashes: dict[str, str]
    hidden_role_hashes: dict[str, str]
    evaluation_level: str
    metadata: dict[str, Any] = field(default_factory=dict)

    def validate(self) -> None:
        if not self.arc_id or any(character.isspace() for character in self.arc_id):
            raise ValueError("arc_id must be non-empty and contain no whitespace")
        role_names = [role.role for role in self.roles]
        if role_names != list(ROLE_ORDER):
            raise ValueError(f"roles must be ordered exactly as {ROLE_ORDER}")
        for role in self.roles:
            role.validate()
        if set(self.hidden_role_hashes) != DEPLOYMENT_ROLES:
            raise ValueError("hidden_role_hashes must expose hashes for T4/T5/T6 only")

    def to_dict(self) -> dict[str, Any]:
        self.validate()
        return asdict(self)

    @classmethod
    def from_dict(cls, payload: dict[str, Any]) -> "ArcManifest":
        source = SourceRecord(**payload["source"])
        roles = [RoleSpec(**item) for item in payload["roles"]]
        result = cls(
            arc_id=payload["arc_id"],
            track=payload["track"],
            modality=payload["modality"],
            source=source,
            parent_skill_path=payload["parent_skill_path"],
            parent_skill_hash=payload["parent_skill_hash"],
            public_arc_path=payload["public_arc_path"],
            private_bundle_hash=payload["private_bundle_hash"],
            roles=roles,
            real_evidence_path=payload["real_evidence_path"],
            sham_evidence_path=payload["sham_evidence_path"],
            visible_hashes=dict(payload["visible_hashes"]),
            hidden_role_hashes=dict(payload["hidden_role_hashes"]),
            evaluation_level=payload["evaluation_level"],
            metadata=dict(payload.get("metadata", {})),
        )
        result.validate()
        return result
