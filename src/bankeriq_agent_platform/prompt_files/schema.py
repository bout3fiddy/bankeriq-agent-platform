"""Strict authoring models; their exported JSON Schemas are checked into prompts/schemas/."""

from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field

Version = Annotated[str, Field(pattern=r"^[0-9]+\.[0-9]+\.[0-9]+$")]
Identifier = Annotated[str, Field(pattern=r"^[a-z][a-z0-9.-]{0,95}$")]
Text = Annotated[str, Field(min_length=1, max_length=32768)]


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class Reference(StrictModel):
    """Pin an exact component version for reproducible recovery."""

    id: Identifier
    version: Version


class Pin(Reference):
    path: Annotated[str, Field(pattern=r"^[a-zA-Z0-9_./-]+\.json$", max_length=256)]
    sha256: Annotated[str, Field(pattern=r"^[a-f0-9]{64}$")]


class Metadata(Reference):
    schema_version: Literal[1]
    title: Annotated[str, Field(min_length=1, max_length=160)]
    description: Annotated[str, Field(min_length=1, max_length=1024)]
    owner: Annotated[str, Field(min_length=1, max_length=160)]
    change_summary: Annotated[str, Field(min_length=1, max_length=1024)]


# A nested array wraps one paragraph into source lines. Rendering joins those
# lines with a single space: ["Use permitted tools", "for this request."] becomes
# "Use permitted tools for this request." Separate paragraphs keep blank lines.
Paragraph = Text | Annotated[tuple[Text, ...], Field(min_length=1, max_length=1024)]


class Fragment(Metadata):
    kind: Literal["fragment"]
    paragraphs: Annotated[tuple[Paragraph, ...], Field(min_length=1, max_length=64)]


class Section(Metadata):
    kind: Literal["section", "system"]
    includes: Annotated[tuple[Reference, ...], Field(min_length=1, max_length=32)]


class Runtime(Metadata):
    kind: Literal["runtime"]
    includes: Annotated[tuple[Reference, ...], Field(min_length=1, max_length=32)]
    runtime_version: Annotated[str, Field(min_length=1, max_length=128)]
    composer_version: Literal["runtime-system-v1"]
    renderer: Literal["sections-v1"] = "sections-v1"


class Document(StrictModel):
    """A document contains exactly one versioned fragment or composition definition."""

    prompt: Annotated[Fragment | Section | Runtime, Field(discriminator="kind")]


class Defaults(StrictModel):
    runtime: Reference
    system: Reference


class Catalog(StrictModel):
    schema_version: Literal[1]
    defaults: Defaults
    documents: Annotated[tuple[Pin, ...], Field(min_length=1, max_length=256)]
