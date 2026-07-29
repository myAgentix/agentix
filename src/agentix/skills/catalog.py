"""Open-standard skill catalog — scan one or more ``skills_root`` directories.

Selection model: the catalog surfaces every bundle's ``(name, description)``
cheaply at session start; the agent's Cortex pulls the full ``SKILL.md`` body on
demand (progressive disclosure).  Multi-root support (``SkillCatalog(roots=[…])``)
lets a composite agent surface skills from multiple packages.  First-root-wins
on name clash.
"""

from __future__ import annotations

import json
from collections.abc import Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Any

import frontmatter
import structlog

from agentix.skills.loader import register_activated_skills
from agentix.tools.registry import ToolRegistry

if TYPE_CHECKING:
    from agentix.a2a.card import AgentSkill

log = structlog.get_logger(__name__)


@dataclass(frozen=True)
class SkillBundle:
    """One discovered skill bundle."""

    name: str
    description: str
    bundle_dir: Path
    skill_md_path: Path | None = None
    has_tools: bool = False
    reference_only: bool = False
    allowed_tools: tuple[str, ...] = field(default_factory=tuple)
    # A2A AgentSkill fields parsed from SKILL.md frontmatter
    id: str = ""
    tags: tuple[str, ...] = field(default_factory=tuple)
    examples: tuple[str, ...] = field(default_factory=tuple)
    input_modes: tuple[str, ...] = field(default_factory=tuple)
    output_modes: tuple[str, ...] = field(default_factory=tuple)

    def to_agent_skill(self) -> AgentSkill:
        """Project this bundle into an A2A v1.0 ``AgentSkill``."""
        from agentix.a2a.card import AgentSkill

        return AgentSkill(  # type: ignore[call-arg]
            id=self.id or self.name,
            name=self.name,
            description=self.description,
            tags=list(self.tags),
            examples=list(self.examples),
            input_modes=list(self.input_modes),
            output_modes=list(self.output_modes),
        )

    def to_dict(self, root_layers: dict[str, str], *, include_body: bool = False) -> dict[str, Any]:
        """Serialise bundle to a plain dict for the admin API."""
        parent = str(self.bundle_dir.parent)
        layer = root_layers.get(parent, "unknown")
        out: dict[str, Any] = {
            "name": self.name,
            "description": self.description or "",
            "layer": layer,
            "root": parent,
            "skill_md_path": str(self.skill_md_path) if self.skill_md_path else None,
            "has_tools": self.has_tools,
            "reference_only": self.reference_only,
            "allowed_tools": list(self.allowed_tools or []),
            "body": None,
        }
        if include_body and self.skill_md_path and Path(self.skill_md_path).exists():
            out["body"] = Path(self.skill_md_path).read_text()
        return out


class SkillCatalog:
    """Per-agent view over one or more ``skills_root`` directories.

    ``roots`` may be a single ``Path``/``str`` or a sequence of them.
    Bundles are discovered across all roots; when the same name appears in
    multiple roots, the first root wins and a warning is logged.
    """

    def __init__(self, roots: Path | str | Sequence[Path | str]) -> None:
        if isinstance(roots, (Path, str)):
            self._roots: list[Path] = [Path(roots)]
        else:
            self._roots = [Path(r) for r in roots]

    @property
    def root(self) -> Path:
        """Primary root (first in the list) — legacy single-root access."""
        return self._roots[0]

    def bundles(self) -> list[SkillBundle]:
        """Discover every bundle across all roots, first-root-wins on name clash."""
        seen: dict[str, str] = {}  # name -> root str for clash logging
        out: list[SkillBundle] = []
        for root in self._roots:
            if not root.exists():
                log.info("skills.root_missing", root=str(root))
                continue
            for bundle_dir in sorted(p for p in root.iterdir() if p.is_dir()):
                bundle = self._read_bundle(bundle_dir)
                if bundle is None:
                    continue
                if bundle.name in seen:
                    log.warning(
                        "skills.name_clash",
                        name=bundle.name,
                        kept=seen[bundle.name],
                        skipped=str(root),
                    )
                    continue
                seen[bundle.name] = str(root)
                out.append(bundle)
        return out

    def describe(self) -> list[tuple[str, str, str]]:
        """``(name, description, skill_md_path)`` for session-start surfacing.

        Reference templates (``_example_*``) are excluded.
        """
        rows: list[tuple[str, str, str]] = []
        for b in self.bundles():
            if b.reference_only:
                continue
            path = str(b.skill_md_path) if b.skill_md_path is not None else str(b.bundle_dir)
            rows.append((b.name, b.description, path))
        return rows

    def activate(self, names: list[str], registry: ToolRegistry) -> list[str]:
        """Register skill-scoped tools for the named bundles across all roots."""
        activated: list[str] = []
        for root in self._roots:
            activated.extend(register_activated_skills(root, names, registry))
        return list(dict.fromkeys(activated))  # dedupe, preserve order

    # ── internals ────────────────────────────────────────────────────────

    def _read_bundle(self, bundle_dir: Path) -> SkillBundle | None:
        skill_md = bundle_dir / "SKILL.md"
        manifest = bundle_dir / "manifest.json"
        if not skill_md.exists() and not manifest.exists():
            return None

        meta = self._read_skill_md_frontmatter(skill_md) if skill_md.exists() else {}
        man = self._read_manifest(manifest) if manifest.exists() else {}

        name = str(meta.get("name") or man.get("name") or bundle_dir.name)
        description = str(meta.get("description") or man.get("description") or "")
        allowed = meta.get("allowed-tools") or man.get("tools") or []
        allowed_tools = tuple(str(t) for t in allowed) if isinstance(allowed, list) else ()

        # A2A frontmatter fields
        skill_id = str(meta.get("id") or name)
        tags = tuple(str(t) for t in meta.get("tags", [])) if isinstance(meta.get("tags"), list) else ()
        examples = tuple(str(e) for e in meta.get("examples", [])) if isinstance(meta.get("examples"), list) else ()
        input_modes = (
            tuple(str(m) for m in meta.get("input_modes", [])) if isinstance(meta.get("input_modes"), list) else ()
        )
        output_modes = (
            tuple(str(m) for m in meta.get("output_modes", [])) if isinstance(meta.get("output_modes"), list) else ()
        )

        return SkillBundle(
            name=name,
            description=description,
            bundle_dir=bundle_dir,
            skill_md_path=skill_md if skill_md.exists() else None,
            has_tools=bool(man.get("tools")),
            reference_only=bundle_dir.name.startswith("_"),
            allowed_tools=allowed_tools,
            id=skill_id,
            tags=tags,
            examples=examples,
            input_modes=input_modes,
            output_modes=output_modes,
        )

    @staticmethod
    def _read_skill_md_frontmatter(path: Path) -> dict[str, Any]:
        try:
            post = frontmatter.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            log.warning("skill.skill_md_unreadable", path=str(path), error=str(exc))
            return {}
        meta = post.metadata
        return dict(meta) if isinstance(meta, dict) else {}

    @staticmethod
    def _read_manifest(path: Path) -> dict[str, Any]:
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            log.warning("skill.manifest_unreadable", path=str(path), error=str(exc))
            return {}
        return data if isinstance(data, dict) else {}
