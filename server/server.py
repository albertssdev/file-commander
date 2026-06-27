#!/usr/bin/env python3
"""
File Commander MCP Server

Full-featured local file and process access for Windows — a lightweight,
reliable replacement for Desktop Commander.

Tools:
  File I/O   — read_file, write_file, edit_file
  Directory  — list_directory, create_directory, move_file, copy_file, get_file_info
  Search     — search_files
  Commands   — run_command
  Processes  — start_process, read_process_output, write_to_process,
               kill_process, list_processes
"""

from __future__ import annotations

import asyncio
import io
import json
import os
import shutil
import subprocess
import sys
import threading
import time
from datetime import datetime
from pathlib import Path
from typing import Dict, List, Optional

from mcp.server.fastmcp import FastMCP
from pydantic import BaseModel, Field, ConfigDict

# ---------------------------------------------------------------------------
# Server init
# ---------------------------------------------------------------------------

mcp = FastMCP("file_commander_mcp")

# ---------------------------------------------------------------------------
# Process session store  (persists for the lifetime of the server process)
# ---------------------------------------------------------------------------

class _Session:
    """Holds a running subprocess and its captured output."""
    def __init__(self, proc: subprocess.Popen, cmd: str):
        self.proc = proc
        self.cmd = cmd
        self.started = time.time()
        self._buf: io.StringIO = io.StringIO()
        self._lock = threading.Lock()
        # Drain stdout+stderr in background threads
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
# Shared helpers
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
    path: str = Field(..., description="Absolute Windows path (e.g. 'C:\\\\Users\\\\Alber\\\\project\\\\main.py')")


class WriteFileInput(BaseModel):
    model_config = ConfigDict(str_strip_whitespace=True, extra="forbid")
    path: str = Field(..., description="Absolute Windows path of the file to write/overwrite")
    content: str = Field(..., description="Full text content to write")


class EditFileInput(BaseModel):
    model_config = ConfigDict(str_strip_whitespace=True, extra="forbid")
    path: str = Field(..., description="Absolute Windows path of the file to edit")
    old_string: str = Field(..., description="Exact string to find — must be unique in the file")
    new_string: str = Field(..., description="Replacement string")


class ListDirectoryInput(BaseModel):
    model_config = ConfigDict(str_strip_whitespace=True, extra="forbid")
    path: str = Field(..., description="Absolute Windows path of the directory to list")
    recursive: bool = Field(default=False, description="If true, list all files recursively")
    max_entries: int = Field(default=500, description="Max entries to return", ge=1, le=5000)


class SrcDstInput(BaseModel):
    model_config = ConfigDict(str_strip_whitespace=True, extra="forbid")
    src: str = Field(..., description="Absolute Windows path of the source")
    dst: str = Field(..., description="Absolute Windows path of the destination")


class SearchInput(BaseModel):
    model_config = ConfigDict(str_strip_whitespace=True, extra="forbid")
    path: str = Field(..., description="Absolute Windows directory path to search in")
    pattern: str = Field(..., description="File name glob pattern, e.g. '*.py', '*.csv', 'main*'")
    content_pattern: Optional[str] = Field(default=None, description="Optional regex or substring to match inside files")
    max_results: int = Field(default=100, description="Max files to return", ge=1, le=1000)


class RunCommandInput(BaseModel):
    model_config = ConfigDict(str_strip_whitespace=True, extra="forbid")
    cmd: str = Field(..., description="Shell command to run (executed via cmd /c)")
    working_dir: Optional[str] = Field(default=None, description="Working directory for the command (absolute path)")
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


# ---------------------------------------------------------------------------
# FILE I/O TOOLS
# ---------------------------------------------------------------------------

@mcp.tool(name="read_file", annotations={"readOnlyHint": True, "destructiveHint": False, "idempotentHint": True})
async def read_file(params: PathInput) -> str:
    """Read any file by absolute Windows path and return its full text content.

    Args:
        params: path — absolute Windows path to the file.

    Returns:
        JSON: {content, size_bytes} or {error}.

    Examples:
        - path='C:\\\\Users\\\\Alber\\\\project\\\\main.py'
        - path='C:\\\\Users\\\\Alber\\\\project\\\\data.csv'
    """
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


@mcp.tool(name="write_file", annotations={"readOnlyHint": False, "destructiveHint": True, "idempotentHint": True})
async def write_file(params: WriteFileInput) -> str:
    """Write or overwrite a file. Creates parent directories automatically.

    Args:
        params: path, content.

    Returns:
        JSON: {success, path, bytes_written} or {error}.
    """
    try:
        p = _p(params.path)
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(params.content, encoding="utf-8")
        return _ok({"success": True, "path": str(p), "bytes_written": len(params.content.encode())})
    except PermissionError:
        return _err(f"Permission denied: {params.path}")
    except Exception as e:
        return _err(f"{type(e).__name__}: {e}")


@mcp.tool(name="edit_file", annotations={"readOnlyHint": False, "destructiveHint": False, "idempotentHint": False})
async def edit_file(params: EditFileInput) -> str:
    """Replace the first (and only) occurrence of old_string with new_string in a file.

    Fails with a clear error if old_string is not found or appears more than once.

    Args:
        params: path, old_string, new_string.

    Returns:
        JSON: {success, path, replacements} or {error}.
    """
    try:
        p = _p(params.path)
        if not p.exists():
            return _err(f"File not found: {params.path}")
        original = p.read_text(encoding="utf-8", errors="replace")
        count = original.count(params.old_string)
        if count == 0:
            return _err("old_string not found — check whitespace and exact characters.")
        if count > 1:
            return _err(f"old_string appears {count} times — add more surrounding context to make it unique.")
        updated = original.replace(params.old_string, params.new_string, 1)
        p.write_text(updated, encoding="utf-8")
        return _ok({"success": True, "path": str(p), "replacements": 1})
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
        params: path — absolute Windows path of the directory to create.

    Returns:
        JSON: {success, path} or {error}.
    """
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
    try:
        src = _p(params.src)
        dst = _p(params.dst)
        if not src.exists():
            return _err(f"Source not found: {params.src}")
        dst.parent.mkdir(parents=True, exist_ok=True)
        final = shutil.move(str(src), str(dst))
        return _ok({"success": True, "source": str(src), "destination": str(final)})
    except PermissionError:
        return _err(f"Permission denied moving {params.src} → {params.dst}")
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
    try:
        src = _p(params.src)
        dst = _p(params.dst)
        if not src.exists():
            return _err(f"Source not found: {params.src}")
        if not src.is_file():
            return _err(f"Source is not a file: {params.src}")
        dst.parent.mkdir(parents=True, exist_ok=True)
        final = shutil.copy2(src, dst)
        return _ok({"success": True, "source": str(src), "destination": str(final)})
    except PermissionError:
        return _err(f"Permission denied: {params.src} → {params.dst}")
    except Exception as e:
        return _err(f"{type(e).__name__}: {e}")


@mcp.tool(name="get_file_info", annotations={"readOnlyHint": True, "destructiveHint": False, "idempotentHint": True})
async def get_file_info(params: PathInput) -> str:
    """Get metadata for a file or directory: size, type, timestamps, extension.

    Args:
        params: path — absolute Windows path.

    Returns:
        JSON: {path, name, type, size_bytes, modified, created, extension} or {error}.
    """
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
# SEARCH
# ---------------------------------------------------------------------------

@mcp.tool(name="search_files", annotations={"readOnlyHint": True, "destructiveHint": False, "idempotentHint": True})
async def search_files(params: SearchInput) -> str:
    """Search for files by name pattern and optionally match content inside them.

    Uses glob patterns for filenames (e.g. '*.py', '*.csv', 'main*').
    If content_pattern is provided, returns only files whose text contains that substring or regex.

    Args:
        params: path, pattern (glob), content_pattern (optional), max_results (default 100).

    Returns:
        JSON: {path, pattern, matches: [{path, name, size_bytes, modified, match_lines?}]} or {error}.

    Examples:
        - Find all Python files: path='C:\\\\project', pattern='*.py'
        - Find files with a string: path='C:\\\\project', pattern='*.py', content_pattern='def process'
    """
    import re

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

        matches = []
        loop = asyncio.get_event_loop()

        def _scan():
            found = []
            for f in root.rglob(params.pattern):
                if len(found) >= params.max_results:
                    break
                if not f.is_file():
                    continue
                entry = _file_info_dict(f)
                if regex:
                    try:
                        text = f.read_text(encoding="utf-8", errors="replace")
                        hit_lines = [
                            {"line": i + 1, "text": ln.rstrip()}
                            for i, ln in enumerate(text.splitlines())
                            if regex.search(ln)
                        ][:10]  # cap at 10 matching lines per file
                        if not hit_lines:
                            continue
                        entry["match_lines"] = hit_lines
                    except Exception:
                        continue
                found.append(entry)
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
        - List dir:      cmd='dir C:\\\\project'
        - Install pkg:   cmd='pip install pandas'
    """
    try:
        cwd = params.working_dir or None
        loop = asyncio.get_event_loop()
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
        return _ok({
            "stdout": result.stdout,
            "stderr": result.stderr,
            "exit_code": result.returncode,
        })
    except subprocess.TimeoutExpired:
        return _err(f"Command timed out after {params.timeout}s. Use start_process for long-running commands.")
    except FileNotFoundError:
        return _err("cmd.exe not found — this server must run on Windows.")
    except Exception as e:
        return _err(f"{type(e).__name__}: {e}")


# ---------------------------------------------------------------------------
# PERSISTENT PROCESS TOOLS
# ---------------------------------------------------------------------------

@mcp.tool(name="start_process", annotations={"readOnlyHint": False, "destructiveHint": True, "idempotentHint": False})
async def start_process(params: StartProcessInput) -> str:
    """Start a long-running or interactive background process. Returns a session_id for later interaction.

    Use this for processes that need to stay alive (servers, REPLs, watchers).
    Use run_command for simple one-shot commands.

    Args:
        params: cmd, working_dir (optional).

    Returns:
        JSON: {session_id, cmd, initial_output, running} or {error}.

    Examples:
        - Start a Python REPL: cmd='python'
        - Start a dev server:  cmd='npm start', working_dir='C:\\\\project'
    """
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
        # Brief pause to capture startup output
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
        return _err("cmd.exe not found — this server must run on Windows.")
    except Exception as e:
        return _err(f"{type(e).__name__}: {e}")


@mcp.tool(name="read_process_output", annotations={"readOnlyHint": True, "destructiveHint": False, "idempotentHint": False})
async def read_process_output(params: SessionInput) -> str:
    """Read new output from a background process started with start_process.

    Output is buffered since the last read — call repeatedly to drain.

    Args:
        params: session_id.

    Returns:
        JSON: {session_id, output, running, exit_code} or {error}.
    """
    if params.session_id not in _SESSIONS:
        return _err(f"Session not found: {params.session_id}. Use list_processes to see active sessions.")
    sess = _SESSIONS[params.session_id]
    await asyncio.sleep(0.1)  # let the drain thread catch up
    return _ok({
        "session_id": params.session_id,
        "output": sess.read_output(),
        "running": sess.running,
        "exit_code": sess.exit_code,
    })


@mcp.tool(name="write_to_process", annotations={"readOnlyHint": False, "destructiveHint": False, "idempotentHint": False})
async def write_to_process(params: WriteToProcessInput) -> str:
    """Send text input to a running background process (e.g. send a command to a REPL).

    A newline is appended automatically.

    Args:
        params: session_id, input.

    Returns:
        JSON: {session_id, sent, output_after} or {error}.
    """
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
        return _err("Process stdin is closed — the process may have exited.")
    except Exception as e:
        return _err(f"{type(e).__name__}: {e}")


@mcp.tool(name="kill_process", annotations={"readOnlyHint": False, "destructiveHint": True, "idempotentHint": True})
async def kill_process(params: SessionInput) -> str:
    """Terminate a background process started with start_process.

    Args:
        params: session_id.

    Returns:
        JSON: {session_id, success, final_output} or {error}.
    """
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
# Entry point
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    mcp.run()
