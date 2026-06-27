#!/usr/bin/env python3
"""
File Commander MCP Server - v0.2.0

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
import io
import json
import os
import re
import shutil
import subprocess
import sys
import threading
import time
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
        for line in stream:
            with self._lock:
                self._buf.write(line)

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
_SESSION_COUNTER = 0


def _new_session_id() -> str:
    global _SESSION_COUNTER
    _SESSION_COUNTER += 1
    return f"proc_{_SESSION_COUNTER}"


# ---------------------------------------------------------------------------
# SSH session store
# ---------------------------------------------------------------------------

_SSH_SESSIONS: Dict[str, Any] = {}
_SSH_COUNTER = 0


def _new_ssh_id() -> str:
    global _SSH_COUNTER
    _SSH_COUNTER += 1
    return f"ssh_{_SSH_COUNTER}"


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
        self._page_offset = 0  # next unread result index

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
_SEARCH_COUNTER = 0


def _new_search_id() -> str:
    global _SEARCH_COUNTER
    _SEARCH_COUNTER += 1
    return f"search_{_SEARCH_COUNTER}"


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
# Config helpers
# ---------------------------------------------------------------------------

_CONFIG_PATH = Path.home() / ".file-commander-config.json"


def _load_config() -> dict:
    try:
        if _CONFIG_PATH.exists():
            return json.loads(_CONFIG_PATH.read_text(encoding="utf-8"))
    except Exception:
        pass
    return {}


def _save_config(cfg: dict):
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
    url: str = Field(..., description="URL to download from")
    destination: str = Field(..., description="Absolute Windows path to save the downloaded file")
    timeout: int = Field(default=60, description="Download timeout in seconds", ge=1, le=600)


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
        content = p.read_text(encoding="utf-8", errors="replace")
        return _ok({"content": content, "size_bytes": p.stat().st_size})
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
        all_lines = p.read_text(encoding="utf-8", errors="replace").splitlines()
        tail = all_lines[-params.lines:]
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

