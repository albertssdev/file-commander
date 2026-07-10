# File Commander

Full-featured local file, shell, SSH, and process access for Windows — a drop-in alternative to Desktop Commander with no cloud dependency and no connection drops.

## What it does

Gives Claude direct access to your Windows filesystem and shell: read, write, edit, search, zip, hash, and download files; run one-shot commands; manage persistent background processes; connect to remote hosts via SSH; generate PDFs; and more.

## Requirements

- Windows 10 or 11
- Python 3.9+ on PATH

That's it — the server speaks MCP directly over stdio using only Python's standard library, so there's no `pip install` step and nothing that can go stale or break when a package updates out from under it.

- `pip install reportlab` (optional — enables `write_pdf`)
- `pip install paramiko` (optional — enables SSH tools)

## Components

| Type | Name | Purpose |
|------|------|---------|
| MCP Server | `file-commander` | Exposes 37 file/shell/SSH/process tools to Claude |
| Skill | `file-commander-setup` | Setup guide and troubleshooting reference |
| Skill | `file-ops-guide` | Tool selection best practices |

## Tools (37)

**File I/O:** `read_file`, `read_multiple_files`, `write_file`, `append_to_file`, `edit_file`, `delete_file`, `tail_file`

**Directory:** `list_directory`, `create_directory`, `move_file`, `copy_file`, `get_file_info`

**File Utilities:** `file_hash` (MD5/SHA1/SHA256/SHA512), `download_file`, `zip_files`, `unzip_file`

**Search:** `search_files` (glob + optional content regex), `start_search`, `get_more_search_results`, `stop_search`, `list_searches`

**Shell:** `run_command` (one-shot, timeout-protected)

**Processes:** `start_process`, `read_process_output`, `write_to_process`, `kill_process`, `list_processes`

**PDF:** `write_pdf` *(requires `pip install reportlab`)*

**SSH:** `ssh_connect`, `ssh_run`, `ssh_disconnect`, `list_ssh_sessions` *(requires `pip install paramiko`)*

**Config:** `get_config`, `set_config_value`

**System:** `get_environment`

**Diagnostics:** `get_usage_stats`, `get_recent_tool_calls`

## Setup

1. Install Python 3.9+ from https://python.org (check "Add to PATH")
2. Install this plugin in Claude Desktop → Settings → Plugins
3. Restart Claude Desktop

No `pip install` step — the server runs on the standard library alone.

For PDF support: `pip install reportlab`
For SSH support: `pip install paramiko`

## Usage examples

Just talk to Claude naturally — the tools activate automatically:

- *"Read `C:\project\main.py` and find the bug in the parse function"*
- *"Search `C:\project` for any `.py` file containing `def process_audio`"*
- *"Download the latest release from GitHub and save it to `C:\downloads\release.zip`"*
- *"Unzip `C:\downloads\release.zip` into `C:\project`"*
- *"Get the SHA256 hash of `C:\builds\app.exe`"*
- *"Run `python main.py` in `C:\project` and show me the output"*
- *"Start a local dev server with `npm run dev` and keep it running"*
- *"SSH into `192.168.1.10` as `deploy` and run `git pull`"*
- *"Write a PDF report of my findings to `C:\reports\summary.pdf`"*

## Using on multiple computers

The plugin is fully portable. Copy the `.plugin` file to any Windows machine with Python 3.9+ on PATH, then install from file in plugin settings — no other setup needed.

## License

MIT
