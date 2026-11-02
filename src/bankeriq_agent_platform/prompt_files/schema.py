"""Strict authoring models; their exported JSON Schemas are checked into prompts/schemas/."""

from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field

Version = Annotated[str, Field(pattern=r"^[0-9]+\.[0-9]+\.[0-9]+$")]
Identifier = Annotated[str, Field(pattern=r"^[a-z][a-z0-9.-]{0,95}$")]
Text = Annotated[str, Field(min_length=1, max_length=32768)]


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class Reference(StrictModel):
    """Select an exact component; latest/range references would break old-step recovery."""

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


class Fragment(Metadata):
    kind: Literal["fragment"]
    paragraphs: Annotated[tuple[Text, ...], Field(min_length=1, max_length=64)]


class Section(Metadata):
    kind: Literal["section", "system"]
    includes: Annotated[tuple[Reference, ...], Field(min_length=1, max_length=32)]


class Runtime(Metadata):
    kind: Literal["runtime"]
    includes: Annotated[tuple[Reference, ...], Field(min_length=1, max_length=32)]
    runtime_version: Annotated[str, Field(min_length=1, max_length=128)]
    composer_version: Literal["runtime-global-v1", "runtime-system-v1"]
    renderer: Literal["sections-v1", "verbatim-v1"] = "sections-v1"


class Document(StrictModel):
    """A document contains exactly one versioned fragment or composition definition."""

    prompt: Annotated[Fragment | Section | Runtime, Field(discriminator="kind")]


class Defaults(StrictModel):
    runtime: Reference
    system: Reference
    demo: Reference
    demo_input: Reference


class Catalog(StrictModel):
    schema_version: Literal[1]
    defaults: Defaults
    documents: Annotated[tuple[Pin, ...], Field(min_length=1, max_length=256)]
