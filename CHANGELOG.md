# Changelog

All notable changes will be documented here. Follows [semantic versioning](https://semver.org): bump MAJOR for breaking changes, MINOR for new features, PATCH for bug fixes.

## [0.3.0] - 2026-07-10

### Changed
- Rewrote the server's MCP transport layer to use only Python's standard library. Removed the `mcp` and `pydantic` dependencies entirely -- `pip install "mcp[cli]"` is no longer required. All 37 tools behave identically; only the protocol/schema plumbing underneath them changed.
  - Reason: `mcp[cli]`'s dependency chain (`pydantic-core`, `rpds-py`) ships compiled per-Python-version binaries with no stable-ABI wheel, so it can't be vendored into a single portable `.plugin` file without either locking to one exact Python version/architecture or bundling dozens of MB of binaries for every version. Speaking the small slice of the MCP JSON-RPC protocol this server actually needs (initialize, tools/list, tools/call) directly avoids that problem altogether.
- Removed the `mcp` package check from the `SessionStart` hook (nothing to check for anymore) -- it now only verifies Python 3.9+.
- `reportlab` and `paramiko` are still optional, install-on-demand dependencies for `write_pdf` and the SSH tools respectively; those are the only two capabilities that genuinely need a compiled third-party library.

## [0.2.0] - 2026-06-27

### Added
- `read_multiple_files` -- batch-read several files in one call
- `append_to_file` -- append text without overwriting
- `delete_file` -- delete files or directories (requires recursive=true for non-empty dirs)
- `tail_file` -- return last N lines of a file (great for logs)
- `file_hash` -- compute MD5/SHA1/SHA256/SHA512 checksum of any file
- `download_file` -- download a file from a URL (no external dependencies)
- `zip_files` -- create a zip archive from files or folders
- `unzip_file` -- extract a zip archive to a destination folder
- `write_pdf` -- create a PDF from text content (requires pip install reportlab)
- `ssh_connect` -- open SSH connection to a remote host (requires pip install paramiko)
- `ssh_run` -- run a command on a remote host via SSH
- `ssh_disconnect` -- close an SSH session
- `list_ssh_sessions` -- list all active SSH connections
- `start_search` -- start a large background search, returns results incrementally
- `get_more_search_results` -- retrieve next page of results from a background search
- `stop_search` -- cancel a running background search
- `list_searches` -- list all active background searches
- `get_config` -- read a persistent configuration value
- `set_config_value` -- store a persistent configuration value (saved to ~/.file-commander-config.json)
- `get_environment` -- read environment variables (single or all)
- `get_usage_stats` -- server uptime and per-tool call counts
- `get_recent_tool_calls` -- log of the last 100 tool calls with timestamps
- Usage tracking added to all tools

### Changed
- Tool count: 15 -> 37
- Updated plugin description to reflect new capabilities

## [0.1.0] - 2026-06-27

### Added
- Initial release
- 15 tools: `read_file`, `write_file`, `edit_file`, `list_directory`, `create_directory`, `move_file`, `copy_file`, `get_file_info`, `search_files`, `run_command`, `start_process`, `read_process_output`, `write_to_process`, `kill_process`, `list_processes`
- `SessionStart` hook checking Python 3.9+ and `mcp` package on startup
- `file-commander-setup` skill for installation guidance and troubleshooting
- `file-ops-guide` skill for tool selection best practices
