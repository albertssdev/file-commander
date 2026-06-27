---
name: file-commander-setup
description: >
  This skill should be used when the user asks "how do I set up File Commander",
  "how do I install this plugin", "does File Commander work on another computer",
  "what can File Commander do", or needs to troubleshoot the MCP server connection.
metadata:
  version: "0.1.0"
---

# File Commander Setup & Reference

## Requirements

- Windows 10 or 11
- Python 3.9+ on PATH (`python --version` to verify)
- The `mcp` package: `pip install "mcp[cli]"`

No other dependencies. The server uses only Python's standard library plus FastMCP.

## First-Time Setup

1. Install Python from https://python.org if not already installed (check "Add to PATH" during setup).
2. Open a terminal and run: `pip install "mcp[cli]"`
3. In Claude Desktop, go to Settings → Plugins and install this plugin.
4. Restart Claude Desktop. The File Commander tools become available automatically.

## Installing on a Second Computer

Same three steps above — install Python, install `mcp[cli]`, install the plugin. The `.plugin` file is self-contained; copy it to the new machine and install from file in plugin settings.

## Available Tools

| Tool | What it does |
|------|-------------|
| `read_file` | Read any file by absolute path |
| `write_file` | Create or overwrite a file |
| `edit_file` | Targeted string replacement (fails if match is ambiguous) |
| `list_directory` | List files in a folder, optionally recursive |
| `create_directory` | Create a folder and any missing parents |
| `move_file` | Move or rename a file or folder |
| `copy_file` | Copy a file to a new location |
| `get_file_info` | Get size, timestamps, type for a path |
| `search_files` | Find files by glob pattern, optionally matching content |
| `run_command` | Run a one-shot shell command (cmd /c), returns output |
| `start_process` | Launch a persistent background process, returns session_id |
| `read_process_output` | Read buffered output from a background process |
| `write_to_process` | Send input to a background process stdin |
| `kill_process` | Terminate a background process |
| `list_processes` | List all active background process sessions |

## Path Format

Always use absolute Windows paths: `C:\Users\Alber\project\main.py`

In JSON tool calls, backslashes must be doubled: `C:\\Users\\Alber\\project\\main.py`

## Troubleshooting

**"python not found"** — Python is not on PATH. Reinstall Python with "Add to PATH" checked, or specify the full path in the MCP config: `"command": "C:\\Python311\\python.exe"`.

**"mcp module not found"** — Run `pip install "mcp[cli]"` in a terminal, then restart Claude Desktop.

**Tools not appearing** — Restart Claude Desktop after installing the plugin. Check Settings → Plugins to confirm File Commander shows as connected.

**Process session lost after restart** — Background processes (start_process sessions) live only while the MCP server process is running. They reset when Claude Desktop restarts.
