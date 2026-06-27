# File Commander

Lightweight, reliable local file and shell access for Windows — a drop-in alternative to Desktop Commander with no cloud dependency and no connection drops.

## What it does

Gives Claude direct access to your Windows filesystem and shell: read, write, edit, move, copy, and search files; run one-shot commands; and manage persistent background processes (dev servers, REPLs, watchers).

## Requirements

- Windows 10 or 11
- Python 3.9+ on PATH
- `pip install "mcp[cli]"` (one-time setup)

## Components

| Type | Name | Purpose |
|------|------|---------|
| MCP Server | `file-commander` | Exposes 15 file/shell/process tools to Claude |
| Skill | `file-commander-setup` | Setup guide and troubleshooting reference |

## Tools

**File I/O:** `read_file`, `write_file`, `edit_file`  
**Directory:** `list_directory`, `create_directory`, `move_file`, `copy_file`, `get_file_info`  
**Search:** `search_files` (glob + optional content regex)  
**Shell:** `run_command` (one-shot, timeout-protected)  
**Processes:** `start_process`, `read_process_output`, `write_to_process`, `kill_process`, `list_processes`

## Setup

1. Install Python 3.9+ from https://python.org (check "Add to PATH")
2. `pip install "mcp[cli]"`
3. Install this plugin in Claude Desktop → Settings → Plugins
4. Restart Claude Desktop

## Usage examples

Just talk to Claude naturally — the tools activate automatically:

- *"Read `C:\Users\YourName\project\main.py` and find the bug in the parse function"*
- *"Edit `data.csv` — replace the header `title` with `sermon_title`"*
- *"Search `C:\project` for any `.py` file that contains `def process_audio`"*
- *"Run `python main.py` in `C:\project` and show me the output"*
- *"Start a local server with `python -m http.server 8000` and keep it running"*
- *"List everything in `C:\Users\YourName\project` recursively"*

## Using on multiple computers

The plugin is fully portable. Copy the `.plugin` file to any Windows machine, install Python + `mcp[cli]`, then install from file in plugin settings.

## License

MIT
