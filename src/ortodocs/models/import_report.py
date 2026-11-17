"""Structuri de raportare a parsării documentelor Word."""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Literal


ImportStatus = Literal["valid", "valid_with_warnings", "rejected"]


@dataclass(slots=True)
class ImportIssue:
    code: str
    message: str
    part: str | None = None
    paragraph_index: int | None = None
    run_index: int | None = None
    element: str | None = None


@dataclass(slots=True)
class ImportReport:
    status: ImportStatus = "valid"
    paragraph_count: int = 0
    fragment_count: int = 0
    bold_fragment_count: int = 0
    italic_fragment_count: int = 0
    underline_fragment_count: int = 0
    variables: dict[str, int] = field(default_factory=dict)
    formatting_errors: list[ImportIssue] = field(default_factory=list)
    unsupported_elements: list[ImportIssue] = field(default_factory=list)
    variable_errors: list[ImportIssue] = field(default_factory=list)
    content_errors: list[ImportIssue] = field(default_factory=list)
    archive_errors: list[ImportIssue] = field(default_factory=list)
    xml_errors: list[ImportIssue] = field(default_factory=list)
    structure_errors: list[ImportIssue] = field(default_factory=list)
    resource_errors: list[ImportIssue] = field(default_factory=list)
    warnings: list[ImportIssue] = field(default_factory=list)
    parsing_errors: list[ImportIssue] = field(default_factory=list)

    @property
    def result(self) -> ImportStatus:
        """Alias cu nume explicit pentru prezentarea rezultatului importului."""

        return self.status

    @property
    def is_accepted(self) -> bool:
        return self.status in ("valid", "valid_with_warnings")

    def finalize(self) -> None:
        has_errors = any(
            (
                self.formatting_errors,
                self.unsupported_elements,
                self.variable_errors,
                self.content_errors,
                self.archive_errors,
                self.xml_errors,
                self.structure_errors,
                self.resource_errors,
                self.parsing_errors,
            )
        )
        if has_errors:
            self.status = "rejected"
        elif self.warnings:
            self.status = "valid_with_warnings"
        else:
            self.status = "valid"

    def to_dict(self) -> dict[str, object]:
        return asdict(self)
