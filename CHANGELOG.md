# Changelog

All notable changes will be documented here. Follows [semantic versioning](https://semver.org): bump MAJOR for breaking changes, MINOR for new features, PATCH for bug fixes.

## [0.2.0] - 2026-06-27

### Added
- `read_multiple_files` -- batch-read several files in one call
- `append_to_file` -- append text without overwriting
- `delete_file` -- delete files or directories (requires recursive=true for non-empty dirs)
- `tail_file` -- return last N lines of a file (great for logs)
- `file_hash` -- compute MD5/SHA1/SHA256/SHA512 checksum of any file
- `download_file` -- download a file from a URL (no external dependencies)
- `zip_files` -- create a zip archive from files or folders
- `un