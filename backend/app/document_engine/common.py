"""Shared helpers for the OOXML analyzers."""
import hashlib

EMU_PER_INCH = 914400
EMU_PER_PT = 12700


def emu_to_in(v: int | None) -> float | None:
    return None if v is None else round(int(v) / EMU_PER_INCH, 3)


def twips_to_in(v: int | None) -> float | None:
    return None if v is None else round(int(v) / 1440, 3)


def sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def clean(d: dict) -> dict:
    """Drop None values so profiles stay compact for LLM prompts."""
    return {k: v for k, v in d.items() if v is not None}
