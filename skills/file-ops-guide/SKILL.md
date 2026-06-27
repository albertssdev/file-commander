---
name: file-ops-guide
description: >
  This skill should be used when Claude is about to perform file operations using
  File Commander tools — editing code, updating config files, reading CSVs, searching
  a project, running scripts, or managing background processes. Load this skill to
  apply correct tool selection, safe editing patterns, and search-before-edit discipline.
metadata:
  version: "0.1.0"
---

# File Commander — Tool Selection & Best Practices

## Choosing the right tool

### Editing an existing file

Use `edit_file` (not `write_file`) whenever changing part of a file. `edit_file` does a targeted string replacement and will fail loudly if the match is absent or ambiguous — this prevents silently editing the wrong spot.

- **Always read the file first** with `read_file`, then supply a unique `old_string` from the actual content.
- If `old_string` appears more than once, include more surrounding lines until it is unique.
- If the entire file needs replacing (e.g. generating from scratch), then `write_file` is appropriate.

### Creating a new file

Use `write_file`. It creates parent directories automatically.

### Checking a file before editing

Run `read_file` first. Never assume file contents — stale assumptions cause `edit_file` to fail or, worse, edit the wrong location.

### Finding files

Use `search_files` with a glob pattern before navigating manually:

```
pattern: "*.py"                         → all Python files under path
pattern: "*.csv", content_pattern: "sermon_id"  → CSVs containing that column
pattern: "config*"                      → any config file
```

`search_files` returns matching lines alongside each file path, so it doubles as a grep tool.

### Running a script or command

- **One-shot, fast**: use `run_command`. Returns stdout/stderr immediately. Default timeout is 30s; set higher for installs or long builds.
- **Long-running / interactive**: use `start_process`. Returns a `session_id`. Poll with `read_process_output`, send input with `write_to_process`, terminate with `kill_process`.

Example — run a Python script and capture output:
```
run_command: cmd="python C:\project\main.py", working_dir="C:\project"
```

Example — start a dev server in the background:
```
start_process: cmd="python -m http.server 8000", working_dir="C:\project"
→ returns session_id: "proc_1"
read_process_output: session_id="proc_1"
```

## Path rules

- Always use absolute Windows paths: `C:\Users\Alber\project\main.py`
- Never use relative paths — the server's working directory is unpredictable.
- Prefer forward slashes in tool arguments if the user's system allows it; both work on Windows.

## Safe editing workflow

1. `read_file` — confirm current content
2. `edit_file` — targeted replacement with a unique `old_string`
3. `read_file` again if verification is important (optional but recommended for critical files)

## Working with CSV / data files

- Use `read_file` to load the full CSV text.
- Parse and reason over it in context rather than shelling out to Python unless transformation is complex.
- Write back with `write_file` (full overwrite) — CSVs rarely benefit from partial edits.

## Process hygiene

- Always call `kill_process` when a background session is no longer needed — processes survive as long as the MCP server is running.
- Call `list_processes` to audit what is still running before starting new long-lived processes.
- If a process exits unexpectedly, `read_process_output` will show the final output and `exit_code` will be non-zero.

## Error recovery

| Error message | Fix |
|---|---|
| `old_string not found` | Re-read the file; the content may have changed since you last read it |
| `old_string appears N times` | Add more lines above/below to make the match unique |
| `File not found` | Verify the path with `list_directory` or `search_files` |
| `Command timed out` | Use `start_process` instead of `run_command` for long operations |
| `Session not found` | The server may have restarted; use `start_process` to create a new session |
