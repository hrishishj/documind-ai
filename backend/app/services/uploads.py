"""Upload validation. The extension is only a hint: content must match, macros are refused, archives are bounded."""
import re
import zipfile
from dataclasses import dataclass
from io import BytesIO
from pathlib import PurePosixPath

from app.core.errors import UnsafeFileError

# extension -> (logical format, mime)
ALLOWED = {
    "docx": ("docx", "application/vnd.openxmlformats-officedocument.wordprocessingml.document"),
    "pptx": ("pptx", "application/vnd.openxmlformats-officedocument.presentationml.presentation"),
    "potx": ("pptx", "application/vnd.openxmlformats-officedocument.presentationml.template"),
    "xlsx": ("xlsx", "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"),
    "xls": ("xlsx", "application/vnd.ms-excel"),
    "pdf": ("pdf", "application/pdf"),
    "txt": ("txt", "text/plain"),
    "md": ("txt", "text/markdown"),
}
MACRO_EXT = {"docm", "dotm", "pptm", "potm", "ppsm", "xlsm", "xltm", "xlam", "xlsb"}
MAIN_PART = {  # substring that the package's main part content type must contain
    "docx": b"wordprocessingml.document.main+xml",
    "pptx": b"presentationml.presentation.main+xml",
    "potx": b"presentationml.template.main+xml",
    "xlsx": b"spreadsheetml.sheet.main+xml",
}
MAX_ENTRIES = 5000
MAX_UNCOMPRESSED = 400 * 1024 * 1024
MAX_RATIO = 200          # per-entry compression ratio considered a bomb ...
RATIO_MIN_SIZE = 10 * 1024 * 1024  # ... once the entry is at least this big
MAX_TEXT_BYTES = 2 * 1024 * 1024
OLE2 = bytes.fromhex("D0CF11E0A1B11AE1")


@dataclass
class ValidatedUpload:
    display_name: str
    ext: str
    format: str
    mime: str
    size: int


def clean_name(name: str) -> str:
    """Display name only. It never reaches the filesystem, but it is shown in the UI and logs."""
    base = re.split(r"[\\/]", name or "")[-1]
    base = re.sub(r"[\x00-\x1f\x7f]", "", base).strip().strip(".")
    return (base[:150] or "file")


def _check_zip(data: bytes, ext: str) -> None:
    if not data.startswith(b"PK\x03\x04"):
        raise UnsafeFileError(f"this is not a valid .{ext} file (not an Office Open XML package)")
    try:
        z = zipfile.ZipFile(BytesIO(data))
        infos = z.infolist()
    except zipfile.BadZipFile:
        raise UnsafeFileError(f"this .{ext} file is corrupt (bad zip structure)")
    if len(infos) > MAX_ENTRIES:
        raise UnsafeFileError("package has too many parts")
    total = 0
    names = set()
    for i in infos:
        p = PurePosixPath(i.filename)
        if p.is_absolute() or ".." in p.parts or "\\" in i.filename:
            raise UnsafeFileError("package contains an unsafe part name")
        names.add(i.filename)
        total += i.file_size
        if total > MAX_UNCOMPRESSED:
            raise UnsafeFileError("package expands to an unreasonable size (possible zip bomb)")
        if i.file_size >= RATIO_MIN_SIZE and i.compress_size and i.file_size / i.compress_size > MAX_RATIO:
            raise UnsafeFileError("package contains a suspiciously compressed part (possible zip bomb)")
    if "[Content_Types].xml" not in names:
        raise UnsafeFileError(f"this is not a valid .{ext} file (missing [Content_Types].xml)")
    if any(n.lower().endswith("vbaproject.bin") or "/activex/" in n.lower() for n in names):
        raise UnsafeFileError("macros / ActiveX controls are not allowed")
    ct = z.read("[Content_Types].xml")
    if MAIN_PART[ext] not in ct:
        if b"macroEnabled" in ct:
            raise UnsafeFileError("macro-enabled Office files are not allowed")
        raise UnsafeFileError(f"the content of this file is not a {ext.upper()} document (extension and content disagree)")
    # XML External Entity defence: legitimate Office XML never declares a DOCTYPE/ENTITY
    for i in infos:
        if i.filename.lower().endswith((".xml", ".rels")) and i.file_size:
            head = z.open(i).read(4096)
            if b"<!DOCTYPE" in head or b"<!ENTITY" in head:
                raise UnsafeFileError("XML part declares a DOCTYPE/ENTITY (not allowed)")


def validate_upload(filename: str, data: bytes, max_bytes: int) -> ValidatedUpload:
    name = clean_name(filename)
    ext = name.rsplit(".", 1)[-1].lower() if "." in name else ""
    if ext in MACRO_EXT:
        raise UnsafeFileError("macro-enabled Office files are not allowed")
    if ext not in ALLOWED:
        raise UnsafeFileError(f"file type '.{ext}' is not supported; allowed: {', '.join(sorted(ALLOWED))}")
    if not data:
        raise UnsafeFileError("the file is empty")
    if len(data) > max_bytes:
        raise UnsafeFileError(f"file is larger than the {max_bytes // (1024 * 1024)} MB limit")
    fmt, mime = ALLOWED[ext]
    if ext in MAIN_PART:
        _check_zip(data, ext)
    elif ext == "xls":
        if not data.startswith(OLE2):
            raise UnsafeFileError("this is not a valid .xls file (missing OLE2 header)")
        # OLE directory entry names are UTF-16LE, so the ASCII spelling would never match
        if "_VBA_PROJECT".encode("utf-16-le") in data or b"_VBA_PROJECT" in data:
            raise UnsafeFileError("macros are not allowed")
    elif ext == "pdf":
        if not data.startswith(b"%PDF-"):
            raise UnsafeFileError("this is not a valid PDF (missing %PDF header)")
    else:  # txt / md
        if len(data) > MAX_TEXT_BYTES:
            raise UnsafeFileError("text file is larger than 2 MB")
        if b"\x00" in data:
            raise UnsafeFileError("this does not look like a text file")
        try:
            data.decode("utf-8")
        except UnicodeDecodeError:
            raise UnsafeFileError("text files must be UTF-8")
    return ValidatedUpload(display_name=name, ext=ext, format=fmt, mime=mime, size=len(data))
