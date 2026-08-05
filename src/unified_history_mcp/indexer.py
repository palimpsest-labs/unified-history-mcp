"""FST indexer subprocess wrapper.

Interfaces with the `fst-indexer` binary (https://github.com/jmars/fst-indexer).
"""

import json
import subprocess
from pathlib import Path
from typing import Optional

from .config import DomainConfig


def _iter_domain_files(cfg: DomainConfig) -> list[Path]:
    """Return domain files/dirs, newest first."""
    root = cfg.dir
    if not root.is_dir():
        return []

    if cfg.type == "dirs":
        import fnmatch

        items = [p for p in root.iterdir() if p.is_dir()]
        items = [p for p in items if fnmatch.fnmatch(p.name, cfg.pattern)]
    else:
        items = []
        for p in root.rglob(cfg.pattern):
            if p.is_dir():
                continue
            if cfg.extensions and p.suffix.lower() not in cfg.extensions:
                continue
            items.append(p)

    return sorted(
        items, key=lambda p: p.stat().st_mtime if p.exists() else 0, reverse=True
    )


def _load_manifest(index_dir: Path) -> Optional[list[dict]]:
    """Load the Rust indexer's manifest.json to resolve file_idx -> filename.

    The Rust binary writes manifest.json in the same order it assigns file_idx,
    so files[N] in the manifest corresponds to file_idx=N in search results.
    """
    manifest_path = index_dir / "manifest.json"
    if not manifest_path.exists():
        return None
    try:
        data = json.loads(manifest_path.read_text(encoding="utf-8", errors="replace"))
        return data.get("files", [])
    except (json.JSONDecodeError, OSError):
        return None


def build_index(cfg: DomainConfig, index_dir: Optional[str] = None) -> tuple[bool, str]:
    """Run fst-indexer build for a domain. Returns (success, message)."""
    binary = cfg.fst_binary or "fst-indexer"
    out_dir = Path(index_dir).expanduser().resolve() if index_dir else cfg.effective_index_dir

    files = _iter_domain_files(cfg)
    if not files:
        return False, f"No files found for domain '{cfg.name}' in {cfg.dir}"

    # For "dirs" domains the content lives in per-dir files (e.g. messages.jsonl);
    # fst_pattern overrides the dir-glob so fst-indexer indexes those files.
    fst_pattern = cfg.fst_pattern or cfg.pattern

    try:
        cmd = [
            binary,
            "build",
            "--dir", str(cfg.dir.resolve()),
            "--pattern", fst_pattern,
            "--extractor", cfg.extractor,
            "--output", str(out_dir),
        ]
        result = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=120,
        )
        if result.returncode != 0:
            return False, f"Index build failed for '{cfg.name}': {result.stderr.strip()}"

        # For "dirs" domains the manifest filename is now "<dir>/<file>"; rewrite
        # it to the parent directory name so the server can resolve
        # {domain_dir}/{parent}/{message_file} when reading hits.
        if cfg.type == "dirs":
            _fix_dirs_manifest(out_dir)

        return True, f"Index built for '{cfg.name}' ({len(files)} files) at {out_dir}"
    except FileNotFoundError:
        return False, f"fst-indexer binary not found: {binary}. Install it from the fst-indexer project."
    except subprocess.TimeoutExpired:
        return False, f"Index build timed out for '{cfg.name}'"
    except OSError as e:
        return False, f"Index build error for '{cfg.name}': {e}"


def _fix_dirs_manifest(index_dir: Path) -> None:
    """Rewrite a dirs-domain manifest so each filename is its parent directory name."""
    manifest_path = Path(index_dir) / "manifest.json"
    try:
        data = json.loads(manifest_path.read_text(encoding="utf-8", errors="replace"))
    except (OSError, json.JSONDecodeError):
        return
    for fe in data.get("files", []):
        parent = Path(str(fe.get("filename", ""))).parent.name
        if parent and parent != ".":
            fe["filename"] = parent
    try:
        manifest_path.write_text(json.dumps(data), encoding="utf-8")
    except OSError:
        pass


def search_fst(
    cfg: DomainConfig,
    query: str,
    max_results: int = 100,
    index_dir: Optional[str] = None,
) -> Optional[list[dict]]:
    """Search via FST. Returns list of {file_idx, entry_idx} or None on failure.

    The Rust binary always writes index files as 'index.fst' in the output directory.
    File resolution uses the Rust-generated manifest.json for correct file_idx mapping.
    """
    binary = cfg.fst_binary or "fst-indexer"
    idx_dir = Path(index_dir).expanduser().resolve() if index_dir else cfg.effective_index_dir
    idx_file = idx_dir / "index.fst"

    if not idx_file.exists():
        return None

    try:
        cmd = [
            binary,
            "search",
            "-i", str(idx_dir),
            query,
            "--max", str(max_results * 20),  # Fetch extra for post-filtering
        ]
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=10)
        if result.returncode != 0:
            return None
        data = json.loads(result.stdout)
        return data.get("results", [])
    except (subprocess.TimeoutExpired, json.JSONDecodeError, OSError):
        return None


def resolve_file_idx(index_dir: Path, file_idx: int) -> Optional[str]:
    """Resolve a file_idx to an actual filename using the Rust manifest.json."""
    files = _load_manifest(index_dir)
    if files is None or file_idx < 0 or file_idx >= len(files):
        return None
    return files[file_idx].get("filename")
