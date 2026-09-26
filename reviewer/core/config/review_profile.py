"""The review profile: which files are reviewed, and by which reviewer.

Everything that used to be hard-coded about *a particular codebase* — that
`db.go` is SQL, that `pbook.go` is a message catalogue, that `ts.go` is a test —
lives in a YAML profile instead, so a team adopting the reviewer describes its own
layout without touching Python. The code keeps only the mechanism.

Resolution (`REVIEW_PROFILE`):

- unset or empty → the bundled ``profiles/default.yml``;
- a bare name (``acme``) → ``profiles/<name>.yml`` next to this package;
- anything else → a path to a YAML file.

The profile is loaded once per process and **validated at load time**: an unknown
reviewer name or a malformed rule is a startup error, not a file silently skipped
mid-review. A reviewer named in a profile but never matched is fine; a rule naming
a reviewer that does not exist is not.

Schema (version 1)::

    version: 1
    exclude: ["*.md", "vendor/*"]        # never reviewed; REVIEW_EXCLUDE_PATTERNS adds to it
    file_types:                          # first match wins — order matters
      - name: sql
        match: ["*.sql", "db.go"]        # globs, case-insensitive
        reviewers: [sql_reviewer]
    sql_routing:
      scan_file_types: [service]         # types scanned for embedded SQL (empty disables)
    instructions:                        # house rules, added to reviewer prompts
      all: "This codebase is ..."        # every reviewer
      sql_reviewer: |                    # one reviewer (by name)
        We do not use foreign keys.

Instructions are how a team teaches the bundled reviewers its own conventions
without forking a prompt. They land in the prompt's *Additional Instructions*
section; ``.aid/<type>.md`` files in the reviewed repository are read on top of
them, and win where the two conflict.

A pattern without a ``/`` matches the file's basename; one with a ``/`` matches
the whole repository-relative path. A file matching no rule is ``unknown`` and
is skipped — there is deliberately no catch-all default.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from fnmatch import fnmatchcase
from pathlib import Path
from typing import Dict, Iterable, List, Optional, Tuple

import yaml

#: Bundled profiles, shipped inside the reviewer image.
PROFILES_DIR = Path(__file__).resolve().parents[2] / "profiles"
DEFAULT_PROFILE = "default"
UNKNOWN_TYPE = "unknown"
ALL_REVIEWERS = "all"


class ProfileError(ValueError):
    """A review profile that cannot be used as written."""


@dataclass(frozen=True)
class FileTypeRule:
    name: str
    patterns: Tuple[str, ...]
    reviewers: Tuple[str, ...]

    def matches(self, path: str) -> bool:
        path = path.lower().lstrip("/")
        base = path.rsplit("/", 1)[-1]
        return any(
            fnmatchcase(path if "/" in pattern else base, pattern) for pattern in self.patterns
        )


@dataclass(frozen=True)
class ReviewProfile:
    source: str
    file_types: Tuple[FileTypeRule, ...]
    exclude: Tuple[str, ...] = ()
    sql_scan_types: Tuple[str, ...] = ()
    instructions: Dict[str, str] = field(default_factory=dict, compare=False)
    _routing: Dict[str, Tuple[str, ...]] = field(default_factory=dict, compare=False)

    def classify(self, path: str) -> str:
        for rule in self.file_types:
            if rule.matches(path):
                return rule.name
        return UNKNOWN_TYPE

    def reviewers_for(self, file_type: str) -> Tuple[str, ...]:
        return self._routing.get((file_type or "").strip().lower(), ())

    def instructions_for(self, reviewer: str) -> str:
        """The profile's house rules for one reviewer: ``all`` first, then its own."""
        parts = [self.instructions.get(ALL_REVIEWERS, ""), self.instructions.get(reviewer, "")]
        return "\n\n".join(p for p in parts if p)


def _strings(value, where: str) -> Tuple[str, ...]:
    if value is None:
        return ()
    if isinstance(value, str):
        value = [value]
    if not isinstance(value, list) or not all(isinstance(v, str) and v.strip() for v in value):
        raise ProfileError(f"{where} must be a string or a list of non-empty strings")
    return tuple(v.strip() for v in value)


def parse_profile(data, source: str, known_reviewers: Iterable[str]) -> ReviewProfile:
    """Validate a loaded YAML document into a `ReviewProfile`."""
    if not isinstance(data, dict):
        raise ProfileError(f"{source}: the profile must be a mapping")
    version = data.get("version", 1)
    if version != 1:
        raise ProfileError(f"{source}: unsupported profile version {version!r} (expected 1)")

    known = set(known_reviewers)
    raw_types = data.get("file_types")
    if not isinstance(raw_types, list) or not raw_types:
        raise ProfileError(f"{source}: 'file_types' must be a non-empty list")

    rules: List[FileTypeRule] = []
    routing: Dict[str, Tuple[str, ...]] = {}
    for i, entry in enumerate(raw_types):
        where = f"{source}: file_types[{i}]"
        if not isinstance(entry, dict):
            raise ProfileError(f"{where} must be a mapping")
        name = str(entry.get("name") or "").strip().lower()
        if not name or name == UNKNOWN_TYPE:
            raise ProfileError(f"{where} needs a 'name' other than {UNKNOWN_TYPE!r}")
        patterns = tuple(p.lower() for p in _strings(entry.get("match"), f"{where}.match"))
        if not patterns:
            raise ProfileError(f"{where} ({name}) has no 'match' patterns")
        reviewers = _strings(entry.get("reviewers"), f"{where}.reviewers")
        if not reviewers:
            raise ProfileError(f"{where} ({name}) names no reviewers")
        unknown = [r for r in reviewers if r not in known]
        if unknown:
            raise ProfileError(
                f"{where} ({name}) names unknown reviewer(s) {unknown}; "
                f"available: {sorted(known)}"
            )
        # Several rules may share a type name (e.g. two pattern groups that are
        # both 'test'); they must then agree on who reviews it.
        if name in routing and routing[name] != reviewers:
            raise ProfileError(f"{where}: type {name!r} is routed to two different reviewer sets")
        routing[name] = reviewers
        rules.append(FileTypeRule(name=name, patterns=patterns, reviewers=reviewers))

    sql = data.get("sql_routing") or {}
    if not isinstance(sql, dict):
        raise ProfileError(f"{source}: 'sql_routing' must be a mapping")
    scan = tuple(t.lower() for t in _strings(sql.get("scan_file_types"), f"{source}: sql_routing.scan_file_types"))
    undeclared = [t for t in scan if t not in routing]
    if undeclared:
        raise ProfileError(f"{source}: sql_routing scans undeclared file type(s) {undeclared}")

    raw_instructions = data.get("instructions") or {}
    if not isinstance(raw_instructions, dict):
        raise ProfileError(f"{source}: 'instructions' must be a mapping of reviewer name to text")
    instructions: Dict[str, str] = {}
    for key, text in raw_instructions.items():
        key = str(key).strip()
        if key != ALL_REVIEWERS and key not in known:
            raise ProfileError(
                f"{source}: instructions for unknown reviewer {key!r}; "
                f"use {ALL_REVIEWERS!r} or one of {sorted(known)}"
            )
        if not isinstance(text, str):
            raise ProfileError(f"{source}: instructions.{key} must be text")
        if text.strip():
            instructions[key] = text.strip()

    return ReviewProfile(
        source=source,
        instructions=instructions,
        file_types=tuple(rules),
        exclude=_strings(data.get("exclude"), f"{source}: exclude"),
        sql_scan_types=scan,
        _routing=routing,
    )


def resolve_profile_path(value: Optional[str]) -> Path:
    value = (value or "").strip() or DEFAULT_PROFILE
    if "/" not in value and not value.endswith((".yml", ".yaml")):
        return PROFILES_DIR / f"{value}.yml"
    return Path(value).expanduser()


def load_profile(value: Optional[str], known_reviewers: Iterable[str]) -> ReviewProfile:
    path = resolve_profile_path(value)
    try:
        text = path.read_text(encoding="utf-8")
    except OSError as err:
        raise ProfileError(f"cannot read review profile {path}: {err}") from err
    try:
        data = yaml.safe_load(text)
    except yaml.YAMLError as err:
        raise ProfileError(f"review profile {path} is not valid YAML: {err}") from err
    return parse_profile(data, str(path), known_reviewers)


_cached: Optional[ReviewProfile] = None


def get_profile() -> ReviewProfile:
    """The process-wide profile named by ``REVIEW_PROFILE``, loaded once."""
    global _cached
    if _cached is None:
        # Imported here: deep_reviewer imports this module for routing.
        from core.agents.deep_reviewer import REVIEWER_SPECS

        _cached = load_profile(os.getenv("REVIEW_PROFILE"), (s.name for s in REVIEWER_SPECS))
    return _cached


def reset_profile() -> None:
    """Drop the cached profile (tests, and a web process whose env changed)."""
    global _cached
    _cached = None
