"""Edit Plan: the only thing the LLM produces. Deterministic renderers apply it to the ORIGINAL file.

Targets use the indices reported by the analyzer's profile (paragraph index = top-level body paragraph,
slide numbers are 1-based, shapes are addressed by name, sheets by name). All targets are resolved against
the untouched original *before* any mutation, so ops never invalidate each other's indices.
"""
from typing import Annotated, Literal, Union

from pydantic import BaseModel, ConfigDict, Field, model_validator


class _Op(BaseModel):
    model_config = ConfigDict(extra="forbid")


# ------------------------------------------------------------------ DOCX
class DocxReplaceText(_Op):
    """Replace the whole text of one paragraph, keeping its dominant run formatting."""
    op: Literal["docx.replace_text"]
    paragraph: int = Field(ge=0)
    text: str


class DocxReplaceAll(_Op):
    """Find/replace everywhere (body, tables, headers, footers), across run boundaries."""
    op: Literal["docx.replace_all"]
    find: str = Field(min_length=1)
    replace: str


class DocxInsertParagraph(_Op):
    """Insert a new paragraph after `after`, cloning the formatting of paragraph `like` (default: `after`)."""
    op: Literal["docx.insert_paragraph"]
    after: int = Field(ge=0)
    text: str
    like: int | None = Field(default=None, ge=0)


class DocxDeleteParagraph(_Op):
    op: Literal["docx.delete_paragraph"]
    paragraph: int = Field(ge=0)


class DocxSetCell(_Op):
    op: Literal["docx.set_cell"]
    table: int = Field(ge=0)
    row: int = Field(ge=0)
    col: int = Field(ge=0)
    text: str


class DocxAddRow(_Op):
    """Append (or insert after `after_row`) a row cloned from `like_row`, with new cell values."""
    op: Literal["docx.add_row"]
    table: int = Field(ge=0)
    values: list[str]
    like_row: int = Field(default=-1)  # -1 = last row
    after_row: int = Field(default=-1)  # -1 = at the end


class DocxDeleteRow(_Op):
    op: Literal["docx.delete_row"]
    table: int = Field(ge=0)
    row: int = Field(ge=0)


# ------------------------------------------------------------------ PPTX
class PptxSetText(_Op):
    """Set a shape's text (one paragraph per line), keeping run formatting. Shape = name or 'idx:<n>' placeholder."""
    op: Literal["pptx.set_text"]
    slide: int = Field(ge=1)
    shape: str
    text: str


class PptxReplaceAll(_Op):
    op: Literal["pptx.replace_all"]
    find: str = Field(min_length=1)
    replace: str


class PptxAddSlide(_Op):
    """New slide from a layout of the template; `texts` maps placeholder idx -> text."""
    op: Literal["pptx.add_slide"]
    layout: str
    texts: dict[str, str] = Field(default_factory=dict)
    position: int | None = Field(default=None, ge=1)  # 1-based final position; default = end


class PptxDuplicateSlide(_Op):
    op: Literal["pptx.duplicate_slide"]
    slide: int = Field(ge=1)
    position: int | None = Field(default=None, ge=1)


class PptxDeleteSlide(_Op):
    op: Literal["pptx.delete_slide"]
    slide: int = Field(ge=1)


class PptxMoveSlide(_Op):
    op: Literal["pptx.move_slide"]
    slide: int = Field(ge=1)
    to: int = Field(ge=1)


class PptxSetCell(_Op):
    op: Literal["pptx.set_cell"]
    slide: int = Field(ge=1)
    shape: str
    row: int = Field(ge=0)
    col: int = Field(ge=0)
    text: str


# ------------------------------------------------------------------ XLSX
Scalar = Union[str, int, float, bool, None]


class XlsxSetCell(_Op):
    """Set a value; the cell's existing style (font, fill, border, number format) is kept."""
    op: Literal["xlsx.set_cell"]
    sheet: str
    cell: str
    value: Scalar


class XlsxSetRange(_Op):
    op: Literal["xlsx.set_range"]
    sheet: str
    top_left: str
    values: list[list[Scalar]]


class XlsxAppendRow(_Op):
    """Add a row below the last data row, copying styles from `like_row` (default: the last data row)."""
    op: Literal["xlsx.append_row"]
    sheet: str
    values: list[Scalar]
    like_row: int | None = Field(default=None, ge=1)


class XlsxCopySheet(_Op):
    op: Literal["xlsx.copy_sheet"]
    sheet: str
    new_name: str = Field(min_length=1, max_length=31)


DocxOp = Annotated[Union[DocxReplaceText, DocxReplaceAll, DocxInsertParagraph, DocxDeleteParagraph, DocxSetCell,
                         DocxAddRow, DocxDeleteRow], Field(discriminator="op")]
PptxOp = Annotated[Union[PptxSetText, PptxReplaceAll, PptxAddSlide, PptxDuplicateSlide, PptxDeleteSlide,
                         PptxMoveSlide, PptxSetCell], Field(discriminator="op")]
XlsxOp = Annotated[Union[XlsxSetCell, XlsxSetRange, XlsxAppendRow, XlsxCopySheet], Field(discriminator="op")]
AnyOp = Annotated[Union[DocxOp, PptxOp, XlsxOp], Field(discriminator="op")]


class EditPlan(BaseModel):
    model_config = ConfigDict(extra="forbid")

    format: Literal["docx", "pptx", "xlsx"]
    summary: str = ""
    ops: list[AnyOp] = Field(max_length=200)
    allow_lossy: bool = False  # permit edits that drop parts the library cannot round-trip (e.g. xlsx charts)

    @model_validator(mode="after")
    def _ops_match_format(self):
        bad = [o.op for o in self.ops if not o.op.startswith(self.format + ".")]
        if bad:
            raise ValueError(f"ops {bad} do not belong to format '{self.format}'")
        return self


def plan_model_for(fmt: str) -> type[BaseModel]:
    """Format-specific plan schema for the LLM: a smaller JSON schema (fewer tokens) in which operations of
    other formats cannot even be expressed. Convert the result with `EditPlan.model_validate(x.model_dump())`."""
    from pydantic import create_model

    union = {"docx": DocxOp, "pptx": PptxOp, "xlsx": XlsxOp}[fmt]
    return create_model(
        f"{fmt.capitalize()}Plan", __config__=ConfigDict(extra="forbid"), __doc__=f"Edit plan for a {fmt} document.",
        format=(Literal[fmt], fmt),  # type: ignore[valid-type]
        summary=(str, Field(default="", description="One or two sentences describing the change, or why nothing could be done")),
        ops=(list[union], Field(default_factory=list, max_length=200)),  # type: ignore[valid-type]
    )


class RenderResult(BaseModel):
    applied: int = 0
    warnings: list[str] = Field(default_factory=list)
    details: list[str] = Field(default_factory=list)


class PlanError(Exception):
    """Raised when a plan cannot be applied; carries LLM-readable messages."""

    def __init__(self, errors: list[str]):
        super().__init__("; ".join(errors))
        self.errors = errors
