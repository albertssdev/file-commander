---
name: file-commander-setup
description: >
  This skill should be used when the user asks "how do I set up File Commander",
  "how do I install this plugin", "does File Commander work on another computer",
  "what can File Commander do", or needs to troubleshoot the MCP server connection.
metadata:
  version: "0.3.0"
---

# File Commander Setup & Reference

## Requirements

- Windows 10 or 11
- Python 3.9+ on PATH (`python --version` to verify)

That's it. The server speaks MCP directly over stdio using only Python's standard
library -- no `pip install` step, and nothing to break when a package updates out
from under it.

- `pip install reportlab` (optional -- enables `write_pdf`)
- `pip install paramiko` (optional -- enables the SSH tools)

## First-Time Setup

1. Install Python from https://python.org if not already installed (check "Add to PATH" during setup).
2. In Claude Desktop, go to Settings → Plugins and install this plugin.
3. Restart Claude Desktop. The File Commander tools become available automatically.

## Installing on a Second Computer

Same two steps above — install Python, install the plugin. The `.plugin` file is
self-contained; copy it to the new machine and install from file in plugin settings.
No other setup needed.

## Available Tools

| Tool | What it does |
|------|-------------|
| `read_file` | Read any file by absolute path |
| `read_multiple_files` | Read several files in one call |
| `write_file` | Create or overwrite a file |
| `append_to_file` | Append text without overwriting |
| `edit_file` | Targeted string replacement (fails if match is ambiguous) |
| `delete_file` | Delete a file or directory |
| `tail_file` | Return the last N lines of a file |
| `list_directory` | List files in a folder, optionally recursive |
| `create_directory` | Create a folder and any missing parents |
| `move_file` | Move or rename a file or folder |
| `copy_file` | Copy a file to a new location |
| `get_file_info` | Get size, timestamps, type for a path |
| `file_hash` | Compute MD5/SHA1/SHA256/SHA512 checksum |
| `download_file` | Download a file from a URL |
| `zip_files` | Create a zip archive from files or folders |
| `unzip_file` | Extract a zip archive |
| `search_files` | Find files by glob pattern, optionally matching content |
| `start_search` | Start a large background search |
| `get_more_search_results` | Page through results from a background search |
| `stop_search` | Cancel a running background search |
| `list_searches` | List active background searches |
| `run_command` | Run a one-shot shell command (cmd /c), returns output |
| `start_process` | Launch a persistent background process, returns session_id |
| `read_process_output` | Read buffered output from a background process |
| `write_to_process` | Send input to a background process stdin |
| `kill_process` | Terminate a background process |
| `list_processes` | List all active background process sessions |
| `write_pdf` | Create a PDF from text content (requires `pip install reportlab`) |
| `ssh_connect` | Open an SSH connection (requires `pip install paramiko`) |
| `ssh_run` | Run a command on a remote host via SSH |
| `ssh_disconnect` | Close an SSH session |
| `list_ssh_sessions` | List active SSH connections |
| `get_config` | Read a persistent configuration value |
| `set_config_value` | Store a persistent configuration value |
| `get_environment` | Read environment variables |
| `get_usage_stats` | Server uptime and per-tool call counts |
| `get_recent_tool_calls` | Log of the last 100 tool calls |

## Path Format

Always use absolute Windows paths: `C:\Users\Alber\project\main.py`

In JSON tool calls, backslashes must be doubled: `C:\\Users\\Alber\\project\\main.py`

## Troubleshooting

**"python not found"** — Python is not on PATH. Reinstall Python with "Add to PATH" checked, or specify the full path in the MCP config: `"command": "C:\\Python311\\python.exe"`.

**Tools not appearing** — Restart Claude Desktop after installing the plugin. Check Settings → Plugins to confirm File Commander shows as connected.

**"write_pdf" fails with an import error** — Run `pip install reportlab`, then restart Claude Desktop.

**SSH tools fail with an import error** — Run `pip install paramiko`, then restart Claude Desktop.

**Process session lost after restart** — Background processes (start_process sessions) live only while the MCP server process is running. They reset when Claude Desktop restarts.
