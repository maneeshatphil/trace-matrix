"""Orchestrates the full ingest pipeline: upload staging -> Engine 1 -> Engine 2 -> Engine 3.

The engines remain independently runnable from the CLI; this module is the single
caller that chains them together for the Streamlit front end.
"""

import re
import shutil
import sys
import time
import zipfile
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Callable, List, Optional, Sequence

ROOT_DIR = Path(__file__).resolve().parents[1]
if str(ROOT_DIR) not in sys.path:
    sys.path.append(str(ROOT_DIR))

INPUT_DIR = ROOT_DIR / "uploads"


def _force_utf8_console() -> None:
    """The engines log with emoji; a cp1252 stdout (the Windows default) makes those
    prints raise mid-parse and look like a parser failure."""
    for stream in (sys.stdout, sys.stderr):
        reconfigure = getattr(stream, "reconfigure", None)
        if reconfigure is None:
            continue
        try:
            reconfigure(encoding="utf-8", errors="replace")
        except Exception:
            pass


_force_utf8_console()

DOCUMENT_EXTENSIONS = {".docx", ".pdf", ".xlsx", ".xls"}
ARCHIVE_EXTENSIONS = {".zip"}
SUPPORTED_EXTENSIONS = sorted(e.lstrip(".") for e in DOCUMENT_EXTENSIONS | ARCHIVE_EXTENSIONS)

RETRY_ATTEMPTS = 2
RETRY_DELAY_SECONDS = 1.0

_SAFE_NAME = re.compile(r"[^A-Za-z0-9._ -]")


class PipelineError(Exception):
    """Raised when a stage fails after every retry."""


@dataclass
class StageResult:
    name: str
    ok: bool = False
    detail: str = ""
    attempts: int = 0
    error: str = ""


@dataclass
class PipelineReport:
    stages: List[StageResult] = field(default_factory=list)
    parsed_paths: List[str] = field(default_factory=list)
    failed_inputs: List[str] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return bool(self.stages) and all(stage.ok for stage in self.stages)

    @property
    def partial(self) -> bool:
        return self.ok and bool(self.failed_inputs)

    def first_error(self) -> str:
        for stage in self.stages:
            if not stage.ok:
                return f"{stage.name}: {stage.error}"
        return ""


# --- Upload staging ---


def ensure_input_dir() -> Path:
    INPUT_DIR.mkdir(parents=True, exist_ok=True)
    return INPUT_DIR


def _safe_name(name: str) -> str:
    """Strips any directory component and characters that could escape the upload folder."""
    cleaned = _SAFE_NAME.sub("_", Path(name).name).strip(". ")
    return cleaned or "upload"


def _unique_target(directory: Path, name: str) -> Path:
    stem, suffix = Path(name).stem, Path(name).suffix
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    candidate = directory / f"{stem}_{stamp}{suffix}"
    counter = 1
    while candidate.exists():
        candidate = directory / f"{stem}_{stamp}_{counter}{suffix}"
        counter += 1
    return candidate


def _extract_archive(archive_path: Path) -> Path:
    """Unpacks a zipped test-evidence folder, refusing members that escape the target dir."""
    target_dir = archive_path.with_suffix("")
    target_dir.mkdir(parents=True, exist_ok=True)
    resolved_root = target_dir.resolve()

    with zipfile.ZipFile(archive_path) as archive:
        for member in archive.infolist():
            if member.is_dir():
                continue
            destination = (target_dir / member.filename).resolve()
            if not str(destination).startswith(str(resolved_root)):
                raise PipelineError(f"Unsafe path in archive '{archive_path.name}': {member.filename}")
            destination.parent.mkdir(parents=True, exist_ok=True)
            with archive.open(member) as source, open(destination, "wb") as handle:
                shutil.copyfileobj(source, handle)

    archive_path.unlink(missing_ok=True)
    return _normalize_extracted_dir(target_dir)


def _normalize_extracted_dir(directory: Path) -> Path:
    """Descends through single-folder wrappers that zip tools add around the real test folder."""
    current = directory
    for _ in range(3):
        entries = [e for e in current.iterdir() if not e.name.startswith("__MACOSX")]
        if len(entries) == 1 and entries[0].is_dir():
            current = entries[0]
            continue
        break
    return current


def stage_uploads(uploaded_files: Sequence, folder_paths: Optional[Sequence[str]] = None) -> List[Path]:
    """Persists Streamlit uploads under uploads/ and returns every path Engine 1 should parse."""
    ensure_input_dir()
    staged: List[Path] = []

    for uploaded in uploaded_files or []:
        name = _safe_name(getattr(uploaded, "name", "upload"))
        suffix = Path(name).suffix.lower()
        if suffix not in DOCUMENT_EXTENSIONS and suffix not in ARCHIVE_EXTENSIONS:
            raise PipelineError(f"Unsupported file type '{suffix or name}'.")

        destination = _unique_target(INPUT_DIR, name)
        with open(destination, "wb") as handle:
            handle.write(uploaded.getbuffer())

        staged.append(_extract_archive(destination) if suffix in ARCHIVE_EXTENSIONS else destination)

    for raw_path in folder_paths or []:
        candidate = Path(str(raw_path).strip().strip('"'))
        if not candidate.exists():
            raise PipelineError(f"Path does not exist on this machine: {candidate}")
        staged.append(candidate)

    return staged


# --- Stage execution ---


def _with_retry(stage_name: str, action: Callable[[], str], progress_cb: Optional[Callable] = None) -> StageResult:
    """Runs a stage, retrying once before giving up."""
    result = StageResult(name=stage_name)
    last_error: Optional[Exception] = None

    for attempt in range(1, RETRY_ATTEMPTS + 1):
        result.attempts = attempt
        try:
            if progress_cb:
                progress_cb(stage_name, "running", f"Attempt {attempt} of {RETRY_ATTEMPTS}")
            result.detail = action() or ""
            result.ok = True
            result.error = ""
            if progress_cb:
                progress_cb(stage_name, "done", result.detail)
            return result
        except Exception as exc:
            last_error = exc
            result.error = str(exc)
            if attempt < RETRY_ATTEMPTS:
                if progress_cb:
                    progress_cb(stage_name, "retry", str(exc))
                time.sleep(RETRY_DELAY_SECONDS)

    if progress_cb:
        progress_cb(stage_name, "failed", str(last_error))
    return result


def run_pipeline(paths: Sequence[Path], progress_cb: Optional[Callable] = None) -> PipelineReport:
    """Upload -> Engine 1 -> Engine 2 -> Engine 3, sequentially, with one retry per stage."""
    from src.engine1_ingestion.parser import parse_and_save
    from src.engine2_chunking.chunker import run_engine_2
    from src.engine3_linker.linker import run_engine_3

    report = PipelineReport()
    targets = [Path(p) for p in paths]

    def engine_1() -> str:
        report.parsed_paths.clear()
        report.failed_inputs.clear()
        errors = []
        for target in targets:
            try:
                report.parsed_paths.append(parse_and_save(str(target)))
            except Exception as exc:
                report.failed_inputs.append(f"{target.name}: {exc}")
                errors.append(f"{target.name}: {exc}")
        if not report.parsed_paths:
            raise PipelineError("; ".join(errors) or "No input produced any parsable item.")
        return f"Parsed {len(report.parsed_paths)} of {len(targets)} input(s)."

    def engine_2() -> str:
        processed, failures = run_engine_2(raise_on_error=True)
        report.failed_inputs.extend(failures)
        return f"Chunked and embedded {processed} document(s)."

    def engine_3() -> str:
        run_engine_3(raise_on_error=True)
        return "Traceability links generated."

    for stage_name, action in (
        ("Engine 1 · Ingestion", engine_1),
        ("Engine 2 · Chunking", engine_2),
        ("Engine 3 · Linking", engine_3),
    ):
        stage = _with_retry(stage_name, action, progress_cb)
        report.stages.append(stage)
        if not stage.ok:
            break

    return report
