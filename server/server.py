#!/usr/bin/env python3
"""
File Commander MCP Server - v0.2.1

Full-featured local file, shell, SSH, and process access for Windows.

Tools:
  File I/O       -- read_file, read_multiple_files, write_file, append_to_file,
                    edit_file, delete_file, tail_file
  Directory      -- list_directory, create_directory, move_file, copy_file, get_file_info
  File Utils     -- file_hash, download_file, zip_files, unzip_file
  Search         -- search_files, start_search, get_more_search_results,
                    stop_search, list_searches
  Commands       -- run_command
  Processes      -- start_process, read_process_output, write_to_process,
                    kill_process, list_processes
  PDF            -- write_pdf
  SSH            -- ssh_connect, ssh_run, ssh_disconnect, list_ssh_sessions
  Config         -- get_config, set_config_value
  System         -- get_environment
  Diagnostics    -- get_usage_stats, get_recent_tool_calls
"""

from __future__ import annotations

import asyncio
import hashlib
import html as _html
import io
import itertools
import json
import os
import re
import shutil
import subprocess
import sys
import threading
import time
import urllib.parse
import urllib.request
import zipfile
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional

from mcp.server.fastmcp import FastMCP
from pydantic import BaseModel, Field, ConfigDict

# ---------------------------------------------------------------------------
# Server init
# ---------------------------------------------------------------------------

mcp = FastMCP("file_commander_mcp")

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

_MAX_BUF_CHARS  = 5_000_000        # per-process output buffer cap (~5 MB of text)
_SESSION_TTL    = 600              # seconds before dead process sessions are auto-purged
_SEARCH_TTL     = 600              # seconds before completed searches are auto-purged
_MAX_READ_BYTES = 50 * 1024 * 1024 # read_file refuses files larger than 50 MB

# ---------------------------------------------------------------------------
# Process session store
# ---------------------------------------------------------------------------

class _Session:
    """Holds a running subprocess and its captured output."""
    def __init__(self, proc: subprocess.Popen, cmd: str):
        self.proc = proc
        self.cmd = cmd
        self.started = time.time()
        self._buf: io.StringIO = io.StringIO()
        self._lock = threading.Lock()
        self._t_out = threading.Thread(target=self._drain, args=(proc.stdout,), daemon=True)
        self._t_err = threading.Thread(target=self._drain, args=(proc.stderr,), daemon=True)
        self._t_out.start()
        self._t_err.start()

    def _drain(self, stream):
        # stream is None when stderr=subprocess.STDOUT merges it into stdout
        if stream is None:
            return
        for line in stream:
            with self._lock:
                self._buf.write(line)
                # Cap buffer to prevent unbounded memory growth from verbose processes
                if self._buf.tell() > _MAX_BUF_CHARS:
                    overflow = self._buf.getvalue()
                    self._buf = io.StringIO("[...output truncated...]\n" + overflow[-(_MAX_BUF_CHARS // 2):])
                    self._buf.seek(0, 2)

    def read_output(self) -> str:
        with self._lock:
            data = self._buf.getvalue()
            self._buf = io.StringIO()
        return data

    @property
    def running(self) -> bool:
        return self.proc.poll() is None

    @property
    def exit_code(self) -> Optional[int]:
        return self.proc.poll()


_SESSIONS: Dict[str, _Session] = {}
_SESSION_IDS = itertools.count(1)


def _new_session_id() -> str:
    return f"proc_{next(_SESSION_IDS)}"


# ---------------------------------------------------------------------------
# SSH session store
# ---------------------------------------------------------------------------

_SSH_SESSIONS: Dict[str, Any] = {}
_SSH_IDS = itertools.count(1)


def _new_ssh_id() -> str:
    return f"ssh_{next(_SSH_IDS)}"


# ---------------------------------------------------------------------------
# Async search store
# ---------------------------------------------------------------------------

class _SearchJob:
    def __init__(self, path: str, pattern: str, content_pattern: Optional[str], max_results: int):
        self.path = path
        self.pattern = pattern
        self.content_pattern = content_pattern
        self.max_results = max_results
        self.started = time.time()
        self.results: List[dict] = []
        self._lock = threading.Lock()
        self.done = False
        self.cancelled = False
        self.error: Optional[str] = None
        self._page_offset = 0

    def append(self, entry: dict):
        with self._lock:
            self.results.append(entry)

    def next_page(self, page_size: int) -> List[dict]:
        with self._lock:
            page = self.results[self._page_offset: self._page_offset + page_size]
            self._page_offset += len(page)
        return page

    @property
    def total_so_far(self) -> int:
        with self._lock:
            return len(self.results)


_SEARCHES: Dict[str, _SearchJob] = {}
_SEARCH_IDS = itertools.count(1)


def _new_search_id() -> str:
    return f"search_{next(_SEARCH_IDS)}"


def _run_search_job(job: _SearchJob):
    """Background thread: runs the search and populates job.results."""
    try:
        root = Path(job.path)
        regex = None
        if job.content_pattern:
            regex = re.compile(job.content_pattern, re.IGNORECASE | re.MULTILINE)
        for f in root.rglob(job.pattern):
            if job.cancelled or job.total_so_far >= job.max_results:
                break
            if not f.is_file():
                continue
            try:
                entry = _file_info_dict(f)
                if regex:
                    text = f.read_text(encoding="utf-8", errors="replace")
                    hit_lines = [
                        {"line": i + 1, "text": ln.rstrip()}
                        for i, ln in enumerate(text.splitlines())
                        if regex.search(ln)
                    ][:10]
                    if not hit_lines:
                        continue
                    entry["match_lines"] = hit_lines
                job.append(entry)
            except Exception:
                continue
    except Exception as exc:
        job.error = f"{type(exc).__name__}: {exc}"
    finally:
        job.done = True


# ---------------------------------------------------------------------------
# Background cleanup — purges dead processes and completed searches after TTL
# ---------------------------------------------------------------------------

def _cleanup_loop():
    while True:
        time.sleep(60)
        now = time.time()
        dead_procs = [sid for sid, s in list(_SESSIONS.items())
                      if not s.running and (now - s.started) > _SESSION_TTL]
        for sid in dead_procs:
            _SESSIONS.pop(sid, None)
        old_searches = [sid for sid, j in list(_SEARCHES.items())
                        if j.done and (now - j.started) > _SEARCH_TTL]
        for sid in old_searches:
            _SEARCHES.pop(sid, None)


threading.Thread(target=_cleanup_loop, daemon=True, name="fc-cleanup").start()


# ---------------------------------------------------------------------------
# Config helpers
# ---------------------------------------------------------------------------

_CONFIG_PATH = Path.home() / ".file-commander-config.json"
_CONFIG_LOCK = threading.Lock()


def _load_config() -> dict:
    with _CONFIG_LOCK:
        try:
            if _CONFIG_PATH.exists():
                return json.loads(_CONFIG_PATH.read_text(encoding="utf-8"))
        except Exception:
            pass
        return {}


def _save_config(cfg: dict):
    with _CONFIG_LOCK:
        _CONFIG_PATH.write_text(json.dumps(cfg, indent=2, ensure_ascii=False), encoding="utf-8")


# ---------------------------------------------------------------------------
# Usage tracking
# ---------------------------------------------------------------------------

_STATS: Dict[str, int] = {}
_CALL_LOG: List[dict] = []
_STATS_LOCK = threading.Lock()
_SERVER_START = time.time()


def _track(tool: str):
    with _STATS_LOCK:
        _STATS[tool] = _STATS.get(tool, 0) + 1
        _CALL_LOG.append({"tool": tool, "timestamp": datetime.now().isoformat()})
        if len(_CALL_LOG) > 100:
            _CALL_LOG.pop(0)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _p(path: str) -> Path:
    return Path(path)


def _ok(data: dict) -> str:
    return json.dumps(data, ensure_ascii=False, default=str)


def _err(msg: str) -> str:
    return json.dumps({"error": msg})


def _file_info_dict(p: Path) -> dict:
    st = p.stat()
    return {
        "path": str(p),
        "name": p.name,
        "type": "directory" if p.is_dir() else "file",
        "size_bytes": st.st_size if p.is_file() else None,
        "modified": datetime.fromtimestamp(st.st_mtime).isoformat(),
        "created": datetime.fromtimestamp(st.st_ctime).isoformat(),
        "extension": p.suffix.lstrip(".") if p.is_file() else None,
    }


# ---------------------------------------------------------------------------
# Input models
# ---------------------------------------------------------------------------

class PathInput(BaseModel):
    model_config = ConfigDict(str_strip_whitespace=True, extra="forbid")
    path: str = Field(..., description="Absolute Windows path (e.g. 'C:\\\\Users\\\\You\\\\project\\\\main.py')")


class WriteFileInput(BaseModel):
    model_config = ConfigDict(str_strip_whitespace=True, extra="forbid")
    path: str = Field(..., description="Absolute Windows path of the file to write/overwrite")
    content: str = Field(..., description="Full text content to write")


class AppendFileInput(BaseModel):
    model_config = ConfigDict(str_strip_whitespace=True, extra="forbid")
    path: str = Field(..., description="Absolute Windows path of the file to append to (created if missing)")
    content: str = Field(..., description="Text to append")


class ReadMultipleFilesInput(BaseModel):
    model_config = ConfigDict(str_strip_whitespace=True, extra="forbid")
    paths: List[str] = Field(..., description="List of absolute Windows paths to read in one call")


class EditFileInput(BaseModel):
    model_config = ConfigDict(str_strip_whitespace=True, extra="forbid")
    path: str = Field(..., description="Absolute Windows path of the file to edit")
    old_string: str = Field(..., description="Exact string to find -- must be unique in the file")
    new_string: str = Field(..., description="Replacement string")


class DeleteInput(BaseModel):
    model_config = ConfigDict(str_strip_whitespace=True, extra="forbid")
    path: str = Field(..., description="Absolute Windows path of the file or directory to delete")
    recursive: bool = Field(default=False, description="Required to delete a non-empty directory")


class TailFileInput(BaseModel):
    model_config = ConfigDict(str_strip_whitespace=True, extra="forbid")
    path: str = Field(..., description="Absolute Windows path of the file to tail")
    lines: int = Field(default=50, description="Number of lines from the end to return", ge=1, le=10000)


class ListDirectoryInput(BaseModel):
    model_config = ConfigDict(str_strip_whitespace=True, extra="forbid")
    path: str = Field(..., description="Absolute Windows path of the directory to list")
    recursive: bool = Field(default=False, description="If true, list all files recursively")
    max_entries: int = Field(default=500, description="Max entries to return", ge=1, le=5000)


class SrcDstInput(BaseModel):
    model_config = ConfigDict(str_strip_whitespace=True, extra="forbid")
    src: str = Field(..., description="Absolute Windows path of the source")
    dst: str = Field(..., description="Absolute Windows path of the destination")


class HashFileInput(BaseModel):
    model_config = ConfigDict(str_strip_whitespace=True, extra="forbid")
    path: str = Field(..., description="Absolute Windows path of the file to hash")
    algorithm: str = Field(default="sha256", description="Hash algorithm: md5, sha1, sha256, sha512")


class DownloadFileInput(BaseModel):
    model_config = ConfigDict(str_strip_whitespace=True, extra="forbid")
    url: str = Field(..., description="URL to download from (must be http:// or https://)")
    destination: str = Field(..., description="Absolute Windows path to save the downloaded file")
    timeout: int = Field(default=60, description="Download timeout in seconds", ge=1, le=600)
    max_mb: int = Field(default=500, description="Maximum file size to download in MB", ge=1, le=10000)


class ZipInput(BaseModel):
    model_config = ConfigDict(str_strip_whitespace=True, extra="forbid")
    paths: List[str] = Field(..., description="Absolute Windows paths to zip (files or folders)")
    destination: str = Field(..., description="Absolute Windows path for the output .zip file")


class UnzipInput(BaseModel):
    model_config = ConfigDict(str_strip_whitespace=True, extra="forbid")
    path: str = Field(..., description="Absolute Windows path of the .zip file to extract")
    destination: str = Field(..., description="Absolute Windows path of the folder to extract into")


class SearchInput(BaseModel):
    model_config = ConfigDict(str_strip_whitespace=True, extra="forbid")
    path: str = Field(..., description="Absolute Windows directory path to search in")
    pattern: str = Field(..., description="File name glob pattern, e.g. '*.py', '*.csv', 'main*'")
    content_pattern: Optional[str] = Field(default=None, description="Optional regex to match inside files")
    max_results: int = Field(default=100, description="Max files to return", ge=1, le=1000)


class SearchPageInput(BaseModel):
    model_config = ConfigDict(str_strip_whitespace=True, extra="forbid")
    search_id: str = Field(..., description="Search ID returned by start_search")
    page_size: int = Field(default=20, description="Results per page", ge=1, le=200)


class SearchIdInput(BaseModel):
    model_config = ConfigDict(str_strip_whitespace=True, extra="forbid")
    search_id: str = Field(..., description="Search ID returned by start_search")


class RunCommandInput(BaseModel):
    model_config = ConfigDict(str_strip_whitespace=True, extra="forbid")
    cmd: str = Field(..., description="Shell command to run (executed via cmd /c)")
    working_dir: Optional[str] = Field(default=None, description="Working directory for the command")
    timeout: int = Field(default=30, description="Timeout in seconds", ge=1, le=300)


class StartProcessInput(BaseModel):
    model_config = ConfigDict(str_strip_whitespace=True, extra="forbid")
    cmd: str = Field(..., description="Command to run as a persistent background process")
    working_dir: Optional[str] = Field(default=None, description="Working directory (absolute path)")


class SessionInput(BaseModel):
    model_config = ConfigDict(str_strip_whitespace=True, extra="forbid")
    session_id: str = Field(..., description="Process session ID returned by start_process")


class WriteToProcessInput(BaseModel):
    model_config = ConfigDict(str_strip_whitespace=True, extra="forbid")
    session_id: str = Field(..., description="Process session ID returned by start_process")
    input: str = Field(..., description="Text to send to the process stdin (newline appended automatically)")


class WritePdfInput(BaseModel):
    model_config = ConfigDict(str_strip_whitespace=True, extra="forbid")
    path: str = Field(..., description="Absolute Windows path for the output .pdf file")
    title: str = Field(default="Document", description="Document title shown at the top of the PDF")
    content: str = Field(..., description="Full text content to write into the PDF")


class SshConnectInput(BaseModel):
    model_config = ConfigDict(str_strip_whitespace=True, extra="forbid")
    host: str = Field(..., description="Hostname or IP address of the SSH server")
    username: str = Field(..., description="SSH username")
    password: Optional[str] = Field(default=None, description="SSH password (omit if using key_path)")
    key_path: Optional[str] = Field(default=None, description="Absolute path to SSH private key file")
    port: int = Field(default=22, description="SSH port", ge=1, le=65535)
    trust_host_key: bool = Field(
        default=False,
        description="Auto-accept unknown host keys (skips MITM protection). Only use on trusted private networks.",
    )


class SshCommandInput(BaseModel):
    model_config = ConfigDict(str_strip_whitespace=True, extra="forbid")
    session_id: str = Field(..., description="SSH session ID returned by ssh_connect")
    command: str = Field(..., description="Shell command to run on the remote host")
    timeout: int = Field(default=30, description="Command timeout in seconds", ge=1, le=300)


class SshSessionInput(BaseModel):
    model_config = ConfigDict(str_strip_whitespace=True, extra="forbid")
    session_id: str = Field(..., description="SSH session ID returned by ssh_connect")


class ConfigKeyInput(BaseModel):
    model_config = ConfigDict(str_strip_whitespace=True, extra="forbid")
    key: str = Field(..., description="Configuration key name")


class SetConfigInput(BaseModel):
    model_config = ConfigDict(str_strip_whitespace=True, extra="forbid")
    key: str = Field(..., description="Configuration key name")
    value: str = Field(..., description="Configuration value (stored as string)")


class GetEnvInput(BaseModel):
    model_config = ConfigDict(str_strip_whitespace=True, extra="forbid")
    variable: Optional[str] = Field(default=None, description="Specific env var name, or omit to list all")


# ---------------------------------------------------------------------------
# FILE I/O TOOLS
# ---------------------------------------------------------------------------

@mcp.tool(name="read_file", annotations={"readOnlyHint": True, "destructiveHint": False, "idempotentHint": True})
async def read_file(params: PathInput) -> str:
    """Read any file by absolute Windows path and return its full text content.

    Args:
        params: path -- absolute Windows path to the file.

    Returns:
        JSON: {content, size_bytes} or {error}.
    """
    _track("read_file")
    try:
        p = _p(params.path)
        if not p.exists():
            return _err(f"File not found: {params.path}")
        if not p.is_file():
            return _err(f"Path is a directory, not a file: {params.path}")
        size = p.stat().st_size
        if size > _MAX_READ_BYTES:
            return _err(
                f"File too large to read as text ({size // (1024 * 1024)} MB). "
                "Use tail_file to read the end, or read a specific section."
            )
        content = p.read_text(encoding="utf-8", errors="replace")
        return _ok({"content": content, "size_bytes": size})
    except PermissionError:
        return _err(f"Permission denied: {params.path}")
    except Exception as e:
        return _err(f"{type(e).__name__}: {e}")


@mcp.tool(name="read_multiple_files", annotations={"readOnlyHint": True, "destructiveHint": False, "idempotentHint": True})
async def read_multiple_files(params: ReadMultipleFilesInput) -> str:
    """Read several files in a single call. Returns each file's content or an individual error.

    More efficient than calling read_file repeatedly when you need multiple files at once.

    Args:
        params: paths -- list of absolute Windows paths.

    Returns:
        JSON: {files: [{path, content, size_bytes} or {path, error}], total, succeeded, failed}.
    """
    _track("read_multiple_files")
    results = []
    for raw_path in params.paths:
        try:
            p = _p(raw_path)
            if not p.exists():
                results.append({"path": raw_path, "error": "File not found"})
            elif not p.is_file():
                results.append({"path": raw_path, "error": "Path is a directory"})
            else:
                content = p.read_text(encoding="utf-8", errors="replace")
                results.append({"path": raw_path, "content": content, "size_bytes": p.stat().st_size})
        except PermissionError:
            results.append({"path": raw_path, "error": "Permission denied"})
        except Exception as exc:
            results.append({"path": raw_path, "error": f"{type(exc).__name__}: {exc}"})

    succeeded = sum(1 for r in results if "error" not in r)
    return _ok({
        "total": len(results),
        "succeeded": succeeded,
        "failed": len(results) - succeeded,
        "files": results,
    })


@mcp.tool(name="write_file", annotations={"readOnlyHint": False, "destructiveHint": True, "idempotentHint": True})
async def write_file(params: WriteFileInput) -> str:
    """Write or overwrite a file with the given content. Creates parent directories automatically.

    Args:
        params: path, content.

    Returns:
        JSON: {success, path, bytes_written} or {error}.
    """
    _track("write_file")
    try:
        p = _p(params.path)
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(params.content, encoding="utf-8")
        return _ok({"success": True, "path": str(p), "bytes_written": len(params.content.encode())})
    except PermissionError:
        return _err(f"Permission denied: {params.path}")
    except Exception as e:
        return _err(f"{type(e).__name__}: {e}")


@mcp.tool(name="append_to_file", annotations={"readOnlyHint": False, "destructiveHint": False, "idempotentHint": False})
async def append_to_file(params: AppendFileInput) -> str:
    """Append text to the end of a file without overwriting it. Creates the file if it does not exist.

    Args:
        params: path, content.

    Returns:
        JSON: {success, path, bytes_appended, total_size_bytes} or {error}.
    """
    _track("append_to_file")
    try:
        p = _p(params.path)
        p.parent.mkdir(parents=True, exist_ok=True)
        with p.open("a", encoding="utf-8") as fh:
            fh.write(params.content)
        return _ok({
            "success": True,
            "path": str(p),
            "bytes_appended": len(params.content.encode()),
            "total_size_bytes": p.stat().st_size,
        })
    except PermissionError:
        return _err(f"Permission denied: {params.path}")
    except Exception as e:
        return _err(f"{type(e).__name__}: {e}")


@mcp.tool(name="edit_file", annotations={"readOnlyHint": False, "destructiveHint": False, "idempotentHint": False})
async def edit_file(params: EditFileInput) -> str:
    """Replace the first (and only) occurrence of old_string with new_string in a file.

    Fails with a clear error if old_string is not found or appears more than once.
    Always read_file first to confirm the exact content before calling this.

    Args:
        params: path, old_string, new_string.

    Returns:
        JSON: {success, path, replacements} or {error}.
    """
    _track("edit_file")
    try:
        p = _p(params.path)
        if not p.exists():
            return _err(f"File not found: {params.path}")
        original = p.read_text(encoding="utf-8", errors="replace")
        count = original.count(params.old_string)
        if count == 0:
            return _err("old_string not found -- check whitespace and exact characters.")
        if count > 1:
            return _err(f"old_string appears {count} times -- add more surrounding context to make it unique.")
        updated = original.replace(params.old_string, params.new_string, 1)
        p.write_text(updated, encoding="utf-8")
        return _ok({"success": True, "path": str(p), "replacements": 1})
    except PermissionError:
        return _err(f"Permission denied: {params.path}")
    except Exception as e:
        return _err(f"{type(e).__name__}: {e}")


@mcp.tool(name="delete_file", annotations={"readOnlyHint": False, "destructiveHint": True, "idempotentHint": False})
async def delete_file(params: DeleteInput) -> str:
    """Delete a file or directory permanently. Directories require recursive=true to delete non-empty ones.

    Args:
        params: path, recursive (default False).

    Returns:
        JSON: {success, path, type} or {error}.
    """
    _track("delete_file")
    try:
        p = _p(params.path)
        if not p.exists():
            return _err(f"Path not found: {params.path}")
        if p.is_file():
            p.unlink()
            return _ok({"success": True, "path": str(p), "type": "file"})
        if p.is_dir():
            if not params.recursive and any(p.iterdir()):
                return _err(
                    f"Directory is not empty: {params.path}. "
                    "Set recursive=true to delete it and all its contents."
                )
            shutil.rmtree(str(p))
            return _ok({"success": True, "path": str(p), "type": "directory"})
        return _err(f"Unknown path type: {params.path}")
    except PermissionError:
        return _err(f"Permission denied: {params.path}")
    except Exception as e:
        return _err(f"{type(e).__name__}: {e}")


@mcp.tool(name="tail_file", annotations={"readOnlyHint": True, "destructiveHint": False, "idempotentHint": True})
async def tail_file(params: TailFileInput) -> str:
    """Return the last N lines of a file. Ideal for log files and large output files.

    Uses a seek-based approach so large files are not fully loaded into memory.

    Args:
        params: path, lines (default 50, max 10000).

    Returns:
        JSON: {path, lines_returned, total_lines, content} or {error}.
    """
    _track("tail_file")
    try:
        p = _p(params.path)
        if not p.exists():
            return _err(f"File not found: {params.path}")
        if not p.is_file():
            return _err(f"Path is not a file: {params.path}")

        chunk = 1 << 16  # 64 KB per read
        lines_wanted = params.lines
        collected = []
        with p.open("rb") as fh:
            fh.seek(0, 2)
            remaining = fh.tell()
            while remaining > 0 and len(collected) < lines_wanted + 1:
                read_size = min(chunk, remaining)
                remaining -= read_size
                fh.seek(remaining)
                collected.insert(0, fh.read(read_size))
        raw = b"".join(collected).decode("utf-8", errors="replace")
        all_lines = raw.splitlines()
        tail = all_lines[-lines_wanted:]
        return _ok({
            "path": str(p),
            "total_lines": len(all_lines),
            "lines_returned": len(tail),
            "content": "\n".join(tail),
        })
    except PermissionError:
        return _err(f"Permission denied: {params.path}")
    except Exception as e:
        return _err(f"{type(e).__name__}: {e}")


# ---------------------------------------------------------------------------
# DIRECTORY TOOLS
# ---------------------------------------------------------------------------


@mcp.tool(name="list_directory", annotations={"readOnlyHint": True, "destructiveHint": False, "idempotentHint": True})
async def list_directory(params: ListDirectoryInput) -> str:
    """List files and folders in a directory, with optional recursive listing.

    Args:
        params: path, recursive (default False), max_entries (default 500).

    Returns:
        JSON: {path, entry_count, entries: [{path, name, type, size_bytes, modified}]} or {error}.
    """
    _track("list_directory")
    try:
        p = _p(params.path)
        if not p.exists():
            return _err(f"Directory not found: {params.path}")
        if not p.is_dir():
            return _err(f"Path is a file, not a directory: {params.path}")

        iterator = p.rglob("*") if params.recursive else p.iterdir()
        entries = []
        for item in iterator:
            if len(entries) >= params.max_entries:
                break
            try:
                entries.append(_file_info_dict(item))
            except (PermissionError, OSError):
                entries.append({"path": str(item), "name": item.name, "error": "inaccessible"})

        return _ok({"path": str(p), "entry_count": len(entries), "entries": entries})
    except PermissionError:
        return _err(f"Permission denied: {params.path}")
    except Exception as e:
        return _err(f"{type(e).__name__}: {e}")


@mcp.tool(name="create_directory", annotations={"readOnlyHint": False, "destructiveHint": False, "idempotentHint": True})
async def create_directory(params: PathInput) -> str:
    """Create a directory and any missing parent directories.

    Args:
        params: path -- absolute Windows path of the directory to create.

    Returns:
        JSON: {success, path} or {error}.
    """
    _track("create_directory")
    try:
        p = _p(params.path)
        p.mkdir(parents=True, exist_ok=True)
        return _ok({"success": True, "path": str(p)})
    except PermissionError:
        return _err(f"Permission denied: {params.path}")
    except Exception as e:
        return _err(f"{type(e).__name__}: {e}")


@mcp.tool(name="move_file", annotations={"readOnlyHint": False, "destructiveHint": False, "idempotentHint": False})
async def move_file(params: SrcDstInput) -> str:
    """Move or rename a file or directory. Creates destination parent directories automatically.

    Args:
        params: src, dst (both absolute Windows paths).

    Returns:
        JSON: {success, source, destination} or {error}.
    """
    _track("move_file")
    try:
        src = _p(params.src)
        dst = _p(params.dst)
        if not src.exists():
            return _err(f"Source not found: {params.src}")
        dst.parent.mkdir(parents=True, exist_ok=True)
        final = shutil.move(str(src), str(dst))
        return _ok({"success": True, "source": str(src), "destination": str(final)})
    except PermissionError:
        return _err(f"Permission denied moving {params.src} -> {params.dst}")
    except Exception as e:
        return _err(f"{type(e).__name__}: {e}")


@mcp.tool(name="copy_file", annotations={"readOnlyHint": False, "destructiveHint": False, "idempotentHint": True})
async def copy_file(params: SrcDstInput) -> str:
    """Copy a file to a destination path or directory. Creates destination parent directories automatically.

    Args:
        params: src (absolute source path), dst (absolute destination path or directory).

    Returns:
        JSON: {success, source, destination} or {error}.
    """
    _track("copy_file")
    try:
        src = _p(params.src)
        dst = _p(params.dst)
        if not src.exists():
            return _err(f"Source not found: {params.src}")
        if src.is_file():
            dst.parent.mkdir(parents=True, exist_ok=True)
            final = shutil.copy2(src, dst)
            return _ok({"success": True, "source": str(src), "destination": str(final), "type": "file"})
        if src.is_dir():
            if dst.exists():
                return _err(
                    f"Destination already exists: {params.dst}. "
                    "Remove it first or choose a different path."
                )
            dst.parent.mkdir(parents=True, exist_ok=True)
            final = shutil.copytree(str(src), str(dst))
            return _ok({"success": True, "source": str(src), "destination": str(final), "type": "directory"})
        return _err(f"Unknown path type: {params.src}")
    except PermissionError:
        return _err(f"Permission denied: {params.src} -> {params.dst}")
    except Exception as e:
        return _err(f"{type(e).__name__}: {e}")


@mcp.tool(name="get_file_info", annotations={"readOnlyHint": True, "destructiveHint": False, "idempotentHint": True})
async def get_file_info(params: PathInput) -> str:
    """Get metadata for a file or directory: size, type, timestamps, extension.

    Args:
        params: path -- absolute Windows path.

    Returns:
        JSON: {path, name, type, size_bytes, modified, created, extension} or {error}.
    """
    _track("get_file_info")
    try:
        p = _p(params.path)
        if not p.exists():
            return _err(f"Path not found: {params.path}")
        return _ok(_file_info_dict(p))
    except PermissionError:
        return _err(f"Permission denied: {params.path}")
    except Exception as e:
        return _err(f"{type(e).__name__}: {e}")


# ---------------------------------------------------------------------------
# FILE UTILITIES
# ---------------------------------------------------------------------------

@mcp.tool(name="file_hash", annotations={"readOnlyHint": True, "destructiveHint": False, "idempotentHint": True})
async def file_hash(params: HashFileInput) -> str:
    """Compute the hash (checksum) of a file for integrity verification.

    Args:
        params: path, algorithm (md5, sha1, sha256, sha512 -- default sha256).

    Returns:
        JSON: {path, algorithm, hash} or {error}.
    """
    _track("file_hash")
    ALGORITHMS = {"md5", "sha1", "sha256", "sha512"}
    algo = params.algorithm.lower()
    if algo not in ALGORITHMS:
        return _err(f"Unsupported algorithm '{params.algorithm}'. Choose from: {', '.join(sorted(ALGORITHMS))}")
    try:
        p = _p(params.path)
        if not p.exists():
            return _err(f"File not found: {params.path}")
        if not p.is_file():
            return _err(f"Path is not a file: {params.path}")
        h = hashlib.new(algo)
        loop = asyncio.get_running_loop()

        def _hash():
            with p.open("rb") as fh:
                for chunk in iter(lambda: fh.read(65536), b""):
                    h.update(chunk)
            return h.hexdigest()

        digest = await loop.run_in_executor(None, _hash)
        return _ok({"path": str(p), "algorithm": algo, "hash": digest})
    except PermissionError:
        return _err(f"Permission denied: {params.path}")
    except Exception as e:
        return _err(f"{type(e).__name__}: {e}")


@mcp.tool(name="download_file", annotations={"readOnlyHint": False, "destructiveHint": False, "idempotentHint": True})
async def download_file(params: DownloadFileInput) -> str:
    """Download a file from a URL and save it to disk. No external dependencies required.

    Args:
        params: url, destination (absolute Windows path), timeout (default 60s).

    Returns:
        JSON: {success, url, destination, size_bytes} or {error}.
    """
    _track("download_file")
    parsed = urllib.parse.urlparse(params.url)
    if parsed.scheme not in ("http", "https"):
        return _err(f"Unsupported URL scheme '{parsed.scheme}'. Only http and https are allowed.")
    try:
        dst = _p(params.destination)
        dst.parent.mkdir(parents=True, exist_ok=True)
        max_bytes = params.max_mb * 1024 * 1024
        loop = asyncio.get_running_loop()

        def _download():
            req = urllib.request.Request(params.url, headers={"User-Agent": "File-Commander/0.2"})
            with urllib.request.urlopen(req, timeout=params.timeout) as resp:
                cl = resp.headers.get("Content-Length")
                if cl and int(cl) > max_bytes:
                    raise ValueError(
                        f"File too large: server reports {int(cl) // (1024 * 1024)} MB, "
                        f"limit is {params.max_mb} MB."
                    )
                downloaded = 0
                with dst.open("wb") as fh:
                    while True:
                        chunk = resp.read(65536)
                        if not chunk:
                            break
                        downloaded += len(chunk)
                        if downloaded > max_bytes:
                            dst.unlink(missing_ok=True)
                            raise ValueError(
                                f"Download exceeded size limit of {params.max_mb} MB."
                            )
                        fh.write(chunk)

        await loop.run_in_executor(None, _download)
        return _ok({
            "success": True,
            "url": params.url,
            "destination": str(dst),
            "size_bytes": dst.stat().st_size,
        })
    except ValueError as e:
        return _err(str(e))
    except Exception as e:
        return _err(f"{type(e).__name__}: {e}")


@mcp.tool(name="zip_files", annotations={"readOnlyHint": False, "destructiveHint": False, "idempotentHint": True})
async def zip_files(params: ZipInput) -> str:
    """Create a zip archive containing the specified files or folders.

    Args:
        params: paths (list of absolute Windows paths to include), destination (output .zip path).

    Returns:
        JSON: {success, destination, entries_added, size_bytes} or {error}.
    """
    _track("zip_files")
    try:
        dst = _p(params.destination)
        dst.parent.mkdir(parents=True, exist_ok=True)
        loop = asyncio.get_running_loop()

        def _zip():
            count = 0
            with zipfile.ZipFile(str(dst), "w", compression=zipfile.ZIP_DEFLATED) as zf:
                for raw in params.paths:
                    src = Path(raw)
                    if not src.exists():
                        raise FileNotFoundError(f"Not found: {raw}")
                    if src.is_file():
                        zf.write(str(src), src.name)
                        count += 1
                    elif src.is_dir():
                        for f in src.rglob("*"):
                            if f.is_file():
                                zf.write(str(f), str(f.relative_to(src.parent)))
                                count += 1
            return count

        added = await loop.run_in_executor(None, _zip)
        return _ok({
            "success": True,
            "destination": str(dst),
            "entries_added": added,
            "size_bytes": dst.stat().st_size,
        })
    except FileNotFoundError as e:
        return _err(str(e))
    except Exception as e:
        return _err(f"{type(e).__name__}: {e}")


@mcp.tool(name="unzip_file", annotations={"readOnlyHint": False, "destructiveHint": False, "idempotentHint": True})
async def unzip_file(params: UnzipInput) -> str:
    """Extract a zip archive to a destination folder. Blocks zip-slip path traversal attacks.

    Args:
        params: path (absolute path to .zip), destination (folder to extract into).

    Returns:
        JSON: {success, source, destination, entries_extracted} or {error}.
    """
    _track("unzip_file")
    try:
        src = _p(params.path)
        dst = _p(params.destination)
        if not src.exists():
            return _err(f"Zip file not found: {params.path}")
        dst.mkdir(parents=True, exist_ok=True)
        loop = asyncio.get_running_loop()

        def _unzip():
            with zipfile.ZipFile(str(src), "r") as zf:
                names = zf.namelist()
                dst_real = os.path.realpath(str(dst))
                for member in names:
                    member_real = os.path.realpath(os.path.join(dst_real, member))
                    if not member_real.startswith(dst_real + os.sep) and member_real != dst_real:
                        raise ValueError(
                            f"Zip slip blocked: entry '{member}' would write outside destination."
                        )
                zf.extractall(str(dst))
                return len(names)

        count = await loop.run_in_executor(None, _unzip)
        return _ok({
            "success": True,
            "source": str(src),
            "destination": str(dst),
            "entries_extracted": count,
        })
    except zipfile.BadZipFile:
        return _err(f"Not a valid zip file: {params.path}")
    except ValueError as e:
        return _err(str(e))
    except Exception as e:
        return _err(f"{type(e).__name__}: {e}")


# ---------------------------------------------------------------------------
# SEARCH
# ---------------------------------------------------------------------------

@mcp.tool(name="search_files", annotations={"readOnlyHint": True, "destructiveHint": False, "idempotentHint": True})
async def search_files(params: SearchInput) -> str:
    """Search for files by name pattern and optionally match content inside them.

    Returns all results immediately. For very large searches, use start_search instead.

    Args:
        params: path, pattern (glob), content_pattern (optional regex), max_results (default 100).

    Returns:
        JSON: {path, pattern, match_count, matches: [{path, name, size_bytes, modified, match_lines?}]} or {error}.

    Examples:
        - All Python files:        path='C:\\\\project', pattern='*.py'
        - Files with a function:   path='C:\\\\project', pattern='*.py', content_pattern='def process'
    """
    _track("search_files")
    try:
        root = _p(params.path)
        if not root.exists():
            return _err(f"Directory not found: {params.path}")
        if not root.is_dir():
            return _err(f"Path is not a directory: {params.path}")

        regex = None
        if params.content_pattern:
            try:
                regex = re.compile(params.content_pattern, re.IGNORECASE | re.MULTILINE)
            except re.error as exc:
                return _err(f"Invalid content_pattern regex: {exc}")

        loop = asyncio.get_running_loop()

        def _scan():
            found = []
            for f in root.rglob(params.pattern):
                if len(found) >= params.max_results:
                    break
                if not f.is_file():
                    continue
                try:
                    entry = _file_info_dict(f)
                    if regex:
                        text = f.read_text(encoding="utf-8", errors="replace")
                        hit_lines = [
                            {"line": i + 1, "text": ln.rstrip()}
                            for i, ln in enumerate(text.splitlines())
                            if regex.search(ln)
                        ][:10]
                        if not hit_lines:
                            continue
                        entry["match_lines"] = hit_lines
                    found.append(entry)
                except Exception:
                    continue
            return found

        matches = await loop.run_in_executor(None, _scan)
        return _ok({
            "path": str(root),
            "pattern": params.pattern,
            "content_pattern": params.content_pattern,
            "match_count": len(matches),
            "matches": matches,
        })
    except Exception as e:
        return _err(f"{type(e).__name__}: {e}")


@mcp.tool(name="start_search", annotations={"readOnlyHint": True, "destructiveHint": False, "idempotentHint": False})
async def start_search(params: SearchInput) -> str:
    """Start a large background search and return a search_id immediately.

    Use get_more_search_results to retrieve results as they come in.
    Better than search_files for very large directory trees.

    Args:
        params: path, pattern, content_pattern (optional), max_results.

    Returns:
        JSON: {search_id, path, pattern, status} or {error}.
    """
    _track("start_search")
    try:
        root = _p(params.path)
        if not root.exists():
            return _err(f"Directory not found: {params.path}")
        if not root.is_dir():
            return _err(f"Path is not a directory: {params.path}")
        if params.content_pattern:
            try:
                re.compile(params.content_pattern)
            except re.error as exc:
                return _err(f"Invalid content_pattern regex: {exc}")

        sid = _new_search_id()
        job = _SearchJob(params.path, params.pattern, params.content_pattern, params.max_results)
        _SEARCHES[sid] = job
        t = threading.Thread(target=_run_search_job, args=(job,), daemon=True)
        t.start()
        return _ok({"search_id": sid, "path": params.path, "pattern": params.pattern, "status": "running"})
    except Exception as e:
        return _err(f"{type(e).__name__}: {e}")


@mcp.tool(name="get_more_search_results", annotations={"readOnlyHint": True, "destructiveHint": False, "idempotentHint": False})
async def get_more_search_results(params: SearchPageInput) -> str:
    """Retrieve the next page of results from a running or completed background search.

    Call repeatedly until has_more is false and done is true.

    Args:
        params: search_id, page_size (default 20).

    Returns:
        JSON: {search_id, results, done, has_more, total_so_far, error?} or {error}.
    """
    _track("get_more_search_results")
    if params.search_id not in _SEARCHES:
        return _err(f"Search not found: {params.search_id}. Use list_searches to see active searches.")
    job = _SEARCHES[params.search_id]
    page = job.next_page(params.page_size)
    with job._lock:
        remaining = len(job.results) - job._page_offset
    return _ok({
        "search_id": params.search_id,
        "results": page,
        "results_in_page": len(page),
        "total_so_far": job.total_so_far,
        "done": job.done,
        "has_more": not job.done or remaining > 0,
        "error": job.error,
    })


@mcp.tool(name="stop_search", annotations={"readOnlyHint": False, "destructiveHint": False, "idempotentHint": True})
async def stop_search(params: SearchIdInput) -> str:
    """Cancel a running background search and remove it from memory.

    Args:
        params: search_id.

    Returns:
        JSON: {search_id, success, total_results_collected} or {error}.
    """
    _track("stop_search")
    if params.search_id not in _SEARCHES:
        return _err(f"Search not found: {params.search_id}.")
    job = _SEARCHES.pop(params.search_id)
    job.cancelled = True
    return _ok({
        "search_id": params.search_id,
        "success": True,
        "total_results_collected": job.total_so_far,
    })


@mcp.tool(name="list_searches", annotations={"readOnlyHint": True, "destructiveHint": False, "idempotentHint": True})
async def list_searches() -> str:
    """List all active background searches started with start_search.

    Returns:
        JSON: {count, searches: [{search_id, path, pattern, done, total_so_far, elapsed_seconds}]}.
    """
    _track("list_searches")
    searches = []
    for sid, job in list(_SEARCHES.items()):
        searches.append({
            "search_id": sid,
            "path": job.path,
            "pattern": job.pattern,
            "content_pattern": job.content_pattern,
            "done": job.done,
            "cancelled": job.cancelled,
            "total_so_far": job.total_so_far,
            "elapsed_seconds": round(time.time() - job.started, 1),
            "error": job.error,
        })
    return _ok({"count": len(searches), "searches": searches})


# ---------------------------------------------------------------------------
# COMMAND RUNNER
# ---------------------------------------------------------------------------

@mcp.tool(name="run_command", annotations={"readOnlyHint": False, "destructiveHint": True, "idempotentHint": False})
async def run_command(params: RunCommandInput) -> str:
    """Run a shell command synchronously via cmd /c and return stdout, stderr, and exit code.

    For long-running or interactive processes, use start_process instead.

    Args:
        params: cmd, working_dir (optional), timeout (default 30s, max 300s).

    Returns:
        JSON: {stdout, stderr, exit_code} or {error}.

    Examples:
        - Run script:    cmd='python main.py', working_dir='C:\\\\project'
        - Install pkg:   cmd='pip install pandas'
        - List dir:      cmd='dir C:\\\\project'
    """
    _track("run_command")
    try:
        cwd = params.working_dir or None
        loop = asyncio.get_running_loop()
        result = await loop.run_in_executor(
            None,
            lambda: subprocess.run(
                ["cmd", "/c", params.cmd],
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=params.timeout,
                cwd=cwd,
            ),
        )
        return _ok({"stdout": result.stdout, "stderr": result.stderr, "exit_code": result.returncode})
    except subprocess.TimeoutExpired:
        return _err(f"Command timed out after {params.timeout}s. Use start_process for long-running commands.")
    except FileNotFoundError:
        return _err("cmd.exe not found -- this server must run on Windows.")
    except Exception as e:
        return _err(f"{type(e).__name__}: {e}")


# ---------------------------------------------------------------------------
# PERSISTENT PROCESS TOOLS
# ---------------------------------------------------------------------------

@mcp.tool(name="start_process", annotations={"readOnlyHint": False, "destructiveHint": True, "idempotentHint": False})
async def start_process(params: StartProcessInput) -> str:
    """Start a long-running or interactive background process. Returns a session_id for later interaction.

    Use this for processes that need to stay alive: servers, REPLs, watchers.
    Use run_command for simple one-shot commands.

    Args:
        params: cmd, working_dir (optional).

    Returns:
        JSON: {session_id, cmd, pid, initial_output, running} or {error}.
    """
    _track("start_process")
    try:
        cwd = params.working_dir or None
        proc = subprocess.Popen(
            ["cmd", "/c", params.cmd],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            encoding="utf-8",
            errors="replace",
            cwd=cwd,
            creationflags=subprocess.CREATE_NO_WINDOW if sys.platform == "win32" else 0,
        )
        sid = _new_session_id()
        _SESSIONS[sid] = _Session(proc, params.cmd)
        await asyncio.sleep(0.5)
        initial = _SESSIONS[sid].read_output()
        return _ok({
            "session_id": sid,
            "cmd": params.cmd,
            "pid": proc.pid,
            "initial_output": initial,
            "running": _SESSIONS[sid].running,
        })
    except FileNotFoundError:
        return _err("cmd.exe not found -- this server must run on Windows.")
    except Exception as e:
        return _err(f"{type(e).__name__}: {e}")


@mcp.tool(name="read_process_output", annotations={"readOnlyHint": True, "destructiveHint": False, "idempotentHint": False})
async def read_process_output(params: SessionInput) -> str:
    """Read new output from a background process started with start_process.

    Output is buffered since the last read -- call repeatedly to drain.

    Args:
        params: session_id.

    Returns:
        JSON: {session_id, output, running, exit_code} or {error}.
    """
    _track("read_process_output")
    if params.session_id not in _SESSIONS:
        return _err(f"Session not found: {params.session_id}. Use list_processes to see active sessions.")
    sess = _SESSIONS[params.session_id]
    await asyncio.sleep(0.1)
    return _ok({
        "session_id": params.session_id,
        "output": sess.read_output(),
        "running": sess.running,
        "exit_code": sess.exit_code,
    })


@mcp.tool(name="write_to_process", annotations={"readOnlyHint": False, "destructiveHint": False, "idempotentHint": False})
async def write_to_process(params: WriteToProcessInput) -> str:
    """Send text input to a running background process (e.g. send a command to a Python REPL).

    A newline is appended automatically.

    Args:
        params: session_id, input.

    Returns:
        JSON: {session_id, sent, output_after} or {error}.
    """
    _track("write_to_process")
    if params.session_id not in _SESSIONS:
        return _err(f"Session not found: {params.session_id}.")
    sess = _SESSIONS[params.session_id]
    if not sess.running:
        return _err(f"Process is no longer running (exit code {sess.exit_code}).")
    try:
        sess.proc.stdin.write(params.input + "\n")
        sess.proc.stdin.flush()
        await asyncio.sleep(0.3)
        output = sess.read_output()
        return _ok({"session_id": params.session_id, "sent": params.input, "output_after": output})
    except BrokenPipeError:
        return _err("Process stdin is closed -- the process may have exited.")
    except Exception as e:
        return _err(f"{type(e).__name__}: {e}")


@mcp.tool(name="kill_process", annotations={"readOnlyHint": False, "destructiveHint": True, "idempotentHint": True})
async def kill_process(params: SessionInput) -> str:
    """Terminate a background process started with start_process. Tries graceful termination first, then force-kills.

    Args:
        params: session_id.

    Returns:
        JSON: {session_id, success, final_output} or {error}.
    """
    _track("kill_process")
    if params.session_id not in _SESSIONS:
        return _err(f"Session not found: {params.session_id}.")
    sess = _SESSIONS[params.session_id]
    try:
        if sess.running:
            sess.proc.terminate()
            try:
                sess.proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                sess.proc.kill()
        final = sess.read_output()
        del _SESSIONS[params.session_id]
        return _ok({"session_id": params.session_id, "success": True, "final_output": final})
    except Exception as e:
        return _err(f"{type(e).__name__}: {e}")


@mcp.tool(name="list_processes", annotations={"readOnlyHint": True, "destructiveHint": False, "idempotentHint": True})
async def list_processes() -> str:
    """List all active background process sessions started with start_process.

    Returns:
        JSON: {count, sessions: [{session_id, cmd, pid, running, exit_code, uptime_seconds}]}.
    """
    _track("list_processes")
    sessions = []
    for sid, sess in list(_SESSIONS.items()):
        sessions.append({
            "session_id": sid,
            "cmd": sess.cmd,
            "pid": sess.proc.pid,
            "running": sess.running,
            "exit_code": sess.exit_code,
            "uptime_seconds": round(time.time() - sess.started, 1),
        })
    return _ok({"count": len(sessions), "sessions": sessions})


# ---------------------------------------------------------------------------
# PDF
# ---------------------------------------------------------------------------

@mcp.tool(name="write_pdf", annotations={"readOnlyHint": False, "destructiveHint": True, "idempotentHint": True})
async def write_pdf(params: WritePdfInput) -> str:
    """Create a PDF file from text content. Requires reportlab: pip install reportlab.

    Args:
        params: path (output .pdf), title (document title), content (text to write).

    Returns:
        JSON: {success, path, size_bytes} or {error}.
    """
    _track("write_pdf")
    try:
        from reportlab.lib.pagesizes import letter
        from reportlab.lib.styles import getSampleStyleSheet
        from reportlab.lib.units import inch
        from reportlab.platypus import Paragraph, SimpleDocTemplate, Spacer
    except ImportError:
        return _err(
            "reportlab is not installed. Run: pip install reportlab  "
            "then restart Claude Desktop so File Commander picks up the new package."
        )
    try:
        p = _p(params.path)
        p.parent.mkdir(parents=True, exist_ok=True)
        loop = asyncio.get_running_loop()

        def _build():
            doc = SimpleDocTemplate(
                str(p), pagesize=letter,
                leftMargin=inch, rightMargin=inch,
                topMargin=inch, bottomMargin=inch,
            )
            styles = getSampleStyleSheet()
            safe_title = _html.escape(params.title)
            story = [Paragraph(safe_title, styles["Title"]), Spacer(1, 12)]
            for para in params.content.split("\n\n"):
                para = para.strip()
                if para:
                    safe_para = _html.escape(para).replace("\n", "<br/>")
                    story.append(Paragraph(safe_para, styles["Normal"]))
                    story.append(Spacer(1, 8))
            doc.build(story)

        await loop.run_in_executor(None, _build)
        return _ok({"success": True, "path": str(p), "size_bytes": p.stat().st_size})
    except Exception as e:
        return _err(f"{type(e).__name__}: {e}")


# ---------------------------------------------------------------------------
# SSH TOOLS
# ---------------------------------------------------------------------------

@mcp.tool(name="ssh_connect", annotations={"readOnlyHint": False, "destructiveHint": False, "idempotentHint": False})
async def ssh_connect(params: SshConnectInput) -> str:
    """Open an SSH connection to a remote host. Requires paramiko: pip install paramiko.

    By default uses the system known_hosts file and rejects unknown host keys.
    Set trust_host_key=true to auto-accept unknown keys (disables MITM protection).

    Args:
        params: host, username, password (or key_path), port (default 22), trust_host_key (default false).

    Returns:
        JSON: {session_id, host, username, port, warning?} or {error}.
    """
    _track("ssh_connect")
    try:
        import paramiko
    except ImportError:
        return _err(
            "paramiko is not installed. Run: pip install paramiko  "
            "then restart Claude Desktop so File Commander picks up the new package."
        )
    try:
        client = paramiko.SSHClient()
        if params.trust_host_key:
            client.set_missing_host_key_policy(paramiko.AutoAddPolicy())
        else:
            client.load_system_host_keys()
            client.set_missing_host_key_policy(paramiko.RejectPolicy())

        connect_kwargs: dict = {
            "hostname": params.host,
            "port": params.port,
            "username": params.username,
            "timeout": 15,
        }
        if params.key_path:
            connect_kwargs["key_filename"] = params.key_path
        elif params.password:
            connect_kwargs["password"] = params.password
        else:
            return _err("Provide either password or key_path.")

        loop = asyncio.get_running_loop()
        await loop.run_in_executor(None, lambda: client.connect(**connect_kwargs))

        sid = _new_ssh_id()
        _SSH_SESSIONS[sid] = {
            "client": client,
            "host": params.host,
            "username": params.username,
            "port": params.port,
            "connected": time.time(),
        }
        result: dict = {"session_id": sid, "host": params.host, "username": params.username, "port": params.port}
        if params.trust_host_key:
            result["warning"] = "Host key accepted via trust_host_key=true (MITM protection disabled)."
        return _ok(result)
    except Exception as e:
        return _err(f"{type(e).__name__}: {e}")


@mcp.tool(name="ssh_run", annotations={"readOnlyHint": False, "destructiveHint": True, "idempotentHint": False})
async def ssh_run(params: SshCommandInput) -> str:
    """Run a command on a remote host via an established SSH session.

    Args:
        params: session_id, command, timeout (default 30s).

    Returns:
        JSON: {session_id, command, stdout, stderr, exit_code} or {error}.
    """
    _track("ssh_run")
    if params.session_id not in _SSH_SESSIONS:
        return _err(f"SSH session not found: {params.session_id}. Use ssh_connect first.")
    sess = _SSH_SESSIONS[params.session_id]
    client = sess["client"]
    try:
        loop = asyncio.get_running_loop()

        def _exec():
            _, stdout, stderr = client.exec_command(params.command, timeout=params.timeout)
            out = stdout.read().decode("utf-8", errors="replace")
            err = stderr.read().decode("utf-8", errors="replace")
            code = stdout.channel.recv_exit_status()
            return out, err, code

        out, err, code = await loop.run_in_executor(None, _exec)
        return _ok({
            "session_id": params.session_id,
            "command": params.command,
            "stdout": out,
            "stderr": err,
            "exit_code": code,
        })
    except Exception as e:
        return _err(f"{type(e).__name__}: {e}")


@mcp.tool(name="ssh_disconnect", annotations={"readOnlyHint": False, "destructiveHint": False, "idempotentHint": True})
async def ssh_disconnect(params: SshSessionInput) -> str:
    """Close an SSH connection and remove it from memory.

    Args:
        params: session_id.

    Returns:
        JSON: {session_id, success} or {error}.
    """
    _track("ssh_disconnect")
    if params.session_id not in _SSH_SESSIONS:
        return _err(f"SSH session not found: {params.session_id}.")
    sess = _SSH_SESSIONS.pop(params.session_id)
    try:
        sess["client"].close()
    except Exception:
        pass
    return _ok({"session_id": params.session_id, "success": True})


@mcp.tool(name="list_ssh_sessions", annotations={"readOnlyHint": True, "destructiveHint": False, "idempotentHint": True})
async def list_ssh_sessions() -> str:
    """List all active SSH connections opened with ssh_connect.

    Returns:
        JSON: {count, sessions: [{session_id, host, username, port, connected_seconds}]}.
    """
    _track("list_ssh_sessions")
    sessions = []
    for sid, info in list(_SSH_SESSIONS.items()):
        sessions.append({
            "session_id": sid,
            "host": info["host"],
            "username": info["username"],
            "port": info["port"],
            "connected_seconds": round(time.time() - info["connected"], 1),
        })
    return _ok({"count": len(sessions), "sessions": sessions})


# ---------------------------------------------------------------------------
# CONFIG TOOLS
# ---------------------------------------------------------------------------

@mcp.tool(name="get_config", annotations={"readOnlyHint": True, "destructiveHint": False, "idempotentHint": True})
async def get_config(params: ConfigKeyInput) -> str:
    """Read a persistent configuration value stored by set_config_value.

    Config is saved to ~/.file-commander-config.json and persists across restarts.

    Args:
        params: key.

    Returns:
        JSON: {key, value, set} or {error}.
    """
    _track("get_config")
    try:
        cfg = _load_config()
        if params.key not in cfg:
            return _ok({"key": params.key, "value": None, "set": False})
        return _ok({"key": params.key, "value": cfg[params.key], "set": True})
    except Exception as e:
        return _err(f"{type(e).__name__}: {e}")



@mcp.tool(name="set_config_value", annotations={"readOnlyHint": False, "destructiveHint": False, "idempotentHint": True})
async def set_config_value(params: SetConfigInput) -> str:
    """Store a persistent configuration value. Useful for default paths, preferences, or server addresses.

    Config is saved to ~/.file-commander-config.json and persists across server restarts.

    Args:
        params: key, value (string).

    Returns:
        JSON: {success, key, value} or {error}.
    """
    _track("set_config_value")
    try:
        cfg = _load_config()
        cfg[params.key] = params.value
        _save_config(cfg)
        return _ok({"success": True, "key": params.key, "value": params.value})
    except Exception as e:
        return _err(f"{type(e).__name__}: {e}")


# ---------------------------------------------------------------------------
# SYSTEM TOOLS
# ---------------------------------------------------------------------------

@mcp.tool(name="get_environment", annotations={"readOnlyHint": True, "destructiveHint": False, "idempotentHint": True})
async def get_environment(params: GetEnvInput) -> str:
    """Read environment variables. Provide a variable name to get one value, or omit to list all.

    Note: listing all variables may expose API keys or passwords stored in the environment.

    Args:
        params: variable (optional name, omit to list all).

    Returns:
        JSON: {variable, value, set} for a single var, or {count, variables, note} for all.
    """
    _track("get_environment")
    try:
        if params.variable:
            val = os.environ.get(params.variable)
            return _ok({"variable": params.variable, "value": val, "set": val is not None})
        env = dict(os.environ)
        return _ok({
            "count": len(env),
            "variables": env,
            "note": "This includes all environment variables. Be cautious if sharing output -- API keys and passwords are commonly stored here.",
        })
    except Exception as e:
        return _err(f"{type(e).__name__}: {e}")


# ---------------------------------------------------------------------------
# DIAGNOSTICS
# ---------------------------------------------------------------------------

@mcp.tool(name="get_usage_stats", annotations={"readOnlyHint": True, "destructiveHint": False, "idempotentHint": True})
async def get_usage_stats() -> str:
    """Return server uptime and a breakdown of how many times each tool has been called this session.

    Returns:
        JSON: {uptime_seconds, total_calls, active_processes, active_searches,
               active_ssh_sessions, calls_per_tool}.
    """
    _track("get_usage_stats")
    with _STATS_LOCK:
        stats_copy = dict(_STATS)
    return _ok({
        "uptime_seconds": round(time.time() - _SERVER_START, 1),
        "total_calls": sum(stats_copy.values()),
        "active_processes": len(_SESSIONS),
        "active_searches": len(_SEARCHES),
        "active_ssh_sessions": len(_SSH_SESSIONS),
        "calls_per_tool": stats_copy,
    })


@mcp.tool(name="get_recent_tool_calls", annotations={"readOnlyHint": True, "destructiveHint": False, "idempotentHint": True})
async def get_recent_tool_calls() -> str:
    """Return a log of the last 100 tool calls made this session, with timestamps.

    Useful for debugging or auditing what File Commander has done during a session.

    Returns:
        JSON: {count, calls: [{tool, timestamp}]}.
    """
    _track("get_recent_tool_calls")
    with _STATS_LOCK:
        log_copy = list(_CALL_LOG)
    return _ok({"count": len(log_copy), "calls": log_copy})


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    mcp.run()
