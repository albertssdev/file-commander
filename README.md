# File Commander

Full-featured local file, shell, SSH, and process access for Windows — a drop-in alternative to Desktop Commander with no cloud dependency and no connection drops.

## What it does

Gives Claude direct access to your Windows filesystem and shell: read, write, edit, search, zip, hash, and download files; run one-shot commands; manage persistent background processes; connect to remote hosts via SSH; generate PDFs; and more.

## Requirements

- Windows 10 or 11
- Python 3.9+ on PATH
- `pip install "mcp[cli]"` (required)
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

**Processes:** `start_process`, `read_proces