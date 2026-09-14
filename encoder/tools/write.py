"""File creation / overwrite."""

from ..checkpoint import record_change
from .base import Tool
from .edit import _changed_files, _unified_diff
from .paths import resolve


class WriteFileTool(Tool):
    name = "write_file"
    description = (
        "Create a new file or completely overwrite an existing one. "
        "For small edits to existing files, prefer edit_file instead."
    )
    parameters = {
        "type": "object",
        "properties": {
            "file_path": {
                "type": "string",
                "description": "Path for the file",
            },
            "content": {
                "type": "string",
                "description": "Full file content to write",
            },
        },
        "required": ["file_path", "content"],
    }

    def execute(self, file_path: str, content: str) -> str:
        try:
            p = resolve(file_path)
            # Read the old body before overwriting it: this tool used to know only
            # how many lines it wrote, so an overwrite left no evidence of what it
            # replaced. One extra read buys the diff, and a new file diffs against
            # nothing (the honest "everything here is new").
            old = ""
            try:
                old = p.read_text(encoding="utf-8")
            except (UnicodeDecodeError, OSError):
                pass          # new file, or binary: nothing to diff against
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_text(content, encoding="utf-8")
            _changed_files.add(str(p))
            full = _unified_diff(old, content, str(p))
            # kind is "write" whether or not the file existed: `kind` is a closed
            # set, and a creation reads off the diff itself (`@@ -0,0 +1,N @@`).
            record_change(file_path, "write", patch=full)
            n_lines = content.count("\n") + (1 if content and not content.endswith("\n") else 0)
            return f"Wrote {n_lines} lines to {file_path}"
        except Exception as e:
            return f"Error: {e}"
