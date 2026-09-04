"""Shell command execution with safety checks.

Claude Code's BashTool is 1,143 lines. This is the distilled version:
- Output capture with truncation (head+tail preserved)
- Timeout support
- Layered command safety (see review.md Item 2):
    * hard-blocked patterns are refused unconditionally (no confirm possible);
    * risky-but-sometimes-legit patterns are refused unless ``confirm=true``,
      and only agents with an interactive user may confirm (teammates can't);
    * when a worktree root is set, obvious writes to an absolute path outside
      that root are refused (a teammate can't leak writes out of its branch).
- Working directory tracking (cd awareness)
"""

import os
import re
import subprocess
import threading
from .base import Tool
from .paths import get_cwd, get_root

# Track cwd across commands (Claude Code does this too). Thread-local, so that
# when the agent executes tools in parallel two bash calls never race on one
# shared global: each worker thread carries its own cwd. See article 05.
_local = threading.local()

# --------------------------------------------------------------------------- #
# Tier 1 - hard-blocked: could wreck the filesystem or leak secrets. These are
# refused unconditionally, even with confirm=true (never appropriate).
# --------------------------------------------------------------------------- #
_DANGEROUS_PATTERNS = [
    # recursive delete aimed at root/home (force flag optional)
    (r"\brm\s+(-\w*)?-r\w*\s+(/|~|\$HOME)", "recursive delete on home/root"),
    # recursive (-r/-R) and force (-f) flags together, in any order or spacing
    (r"\brm\b(?=(?:.*\s)?-\w*[rR])(?=(?:.*\s)?-\w*f)", "force recursive delete"),
    # the same, written with long-form flags
    (r"\brm\b.*--recursive\b.*--force\b|\brm\b.*--force\b.*--recursive\b", "force recursive delete"),
    # Windows drive-wide deletion (del /s /q, rd /s /q, rmdir)
    (r"\b(del|erase|rd|rmdir)\b.*/\s*[sq]+\s*[A-Za-z]:\\", "recursive delete of a Windows drive root"),
    (r"\bformat\s+[A-Za-z]:", "format a drive"),
    (r"\bmkfs\b", "format filesystem"),
    (r"\bdd\s+.*of=/dev/", "raw disk write"),
    (r">\s*/dev/sd[a-z]", "overwrite block device"),
    (r"\bchmod\s+(-R\s+)?777\s+/", "chmod 777 on root"),
    (r":\(\)\s*\{.*:\|:.*\}", "fork bomb"),
    (r"\bcurl\b.*\|\s*(sudo\s+)?(ba)?sh\b", "pipe curl to shell"),
    (r"\bwget\b.*\|\s*(sudo\s+)?(ba)?sh\b", "pipe wget to shell"),
]

# --------------------------------------------------------------------------- #
# Tier 2 - risky but sometimes intended: refused unless the caller passes
# confirm=true AND the agent is allowed to confirm (an interactive user is
# present). Teammates / sub-agents never are, so these are auto-refused there.
# --------------------------------------------------------------------------- #
_RISKY_PATTERNS = [
    # irreversible git history/worktree discards
    (r"\bgit\s+reset\s+--hard\b", "git reset --hard discards committed work"),
    (r"\bgit\s+push\b.*?(?:--force(?=[^\w-]|$)|-f(?=[\s-]|$))",
     "force-push rewrites remote history"),
    (r"\bgit\s+checkout\s+--\s*\.\b", "git checkout -- . discards all local edits"),
    (r"\bgit\s+restore\s+\.\b", "git restore . discards all local edits"),
    # OS-level control
    (r"\b(shutdown|reboot|halt|poweroff)\b", "shut down or reboot the machine"),
    # uploading local content to a remote host
    (r"\bcurl\b[^|;&]*\s(-T|--upload-file)\s", "upload a local file (curl -T)"),
    (r"\bscp\b[^|;&]*\s\S+\s+\S+:", "upload/copy a file to a remote host (scp)"),
    (r"\brsync\b[^|;&]*\s(?:[^\s@]+@)?[^\s:/]+:\S", "copy files to a remote host (rsync)"),
    # network command that also references a secret / credential file
    (r"\b(curl|wget)\b.*\.env\b", "network command referencing a .env file"),
    (r"\b(curl|wget|nc|ncat|socat)\b.*\b(api[_-]?key|api[_-]?secret|client[_-]?secret|access[_-]?token)\b",
     "network command referencing a credential"),
]


class BashTool(Tool):
    name = "bash"
    description = (
        "Execute a shell command. Returns stdout, stderr, and exit code. "
        "Use this for running tests, installing packages, git operations, etc. "
        "Some commands look high-risk and are refused pending your confirmation: "
        "the tool replies 'Needs your confirmation'. In that case stop, explain "
        "the command to the user, and rerun it with confirm=true only once the "
        "user approves."
    )
    parameters = {
        "type": "object",
        "properties": {
            "command": {
                "type": "string",
                "description": "The shell command to run",
            },
            "timeout": {
                "type": "integer",
                "description": "Timeout in seconds (default 120)",
            },
            "confirm": {
                "type": "boolean",
                "description": "Set true ONLY after the user has approved a "
                               "command the tool flagged as high-risk",
            },
        },
        "required": ["command"],
    }

    # whether this tool instance may honour confirm=true. The interactive Lead's
    # bash tool keeps this True; teammate / sub-agent clones have it disabled by
    # ``disable_confirmation()`` so an unattended agent can never self-approve.
    can_confirm = True

    def execute(self, command: str, timeout: int = 120, confirm: bool = False) -> str:
        # tier 1: unconditional block (never confirmable)
        warning = _check_dangerous(command)
        if warning:
            return f"⚠ Blocked: {warning}\nCommand: {command}\nIf intentional, modify the command to be more specific."

        # worktree confinement (add3.0 / review.md): when this thread runs inside
        # a teammate worktree, refuse clear writes to an absolute path outside it
        root = get_root()
        if root is not None:
            target = _escapes_root_write(command, root)
            if target:
                return (f"⚠ Blocked: writes outside this teammate's worktree "
                        f"root are not allowed (target: {target})\nCommand: {command}")

        # tier 2: risky but sometimes intended -> require an interactive confirm
        reason = _check_risky(command)
        if reason:
            if confirm and self.can_confirm:
                return self._run(command, timeout)
            if not self.can_confirm:
                return (f"⛔ Refused: {reason} (no interactive user here to "
                        f"approve it)\nCommand: {command}")
            return (f"⛔ Needs your confirmation: {reason}\nCommand: {command}\n"
                    f"Ask the user, and only if they approve rerun with confirm=true.")

        return self._run(command, timeout)

    def _run(self, command: str, timeout: int) -> str:
        # use this thread's own tracked working directory; fall back to the
        # thread-local cwd set for worktree teammates (add3.0)
        cwd = getattr(_local, "cwd", None) or get_cwd()

        try:
            proc = subprocess.run(
                command,
                shell=True,
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=timeout,
                cwd=cwd,
            )

            # track cd commands so next command runs in the right place
            if proc.returncode == 0:
                _update_cwd(command, cwd)
            out = proc.stdout
            if proc.stderr:
                out += f"\n[stderr]\n{proc.stderr}"
            if proc.returncode != 0:
                out += f"\n[exit code: {proc.returncode}]"
            # keep head + tail to preserve the most useful info
            if len(out) > 15_000:
                out = (
                    out[:6000]
                    + f"\n\n... truncated ({len(out)} chars total) ...\n\n"
                    + out[-3000:]
                )
            return out.strip() or "(no output)"
        except subprocess.TimeoutExpired:
            return f"Error: timed out after {timeout}s"
        except Exception as e:
            return f"Error running command: {e}"


def disable_confirmation(tools) -> None:
    """Make every BashTool in ``tools`` refuse high-risk commands (no confirm).

    Called when building agents that have no interactive user - teammates,
    the one-shot Integration Agent, and sub-agents. Unattended agents must not
    be able to self-approve a command the human never saw.
    """
    for t in tools:
        if isinstance(t, BashTool):
            t.can_confirm = False


def _check_dangerous(cmd: str) -> str | None:
    """Return a warning string if the command looks destructive, else None."""
    for pattern, reason in _DANGEROUS_PATTERNS:
        if re.search(pattern, cmd):
            return reason
    # git clean -fdx deletes untracked AND ignored files - and .worktrees/,
    # .venv/, .TASK/ are ignored, so it can destroy other agents' live state.
    if _git_clean_wipes_ignored(cmd):
        return "git clean -fdx: deletes ignored files (.venv/.worktrees/...)"
    return None


def _check_risky(cmd: str) -> str | None:
    """Return a reason string for risky-but-maybe-intended commands, else None."""
    for pattern, reason in _RISKY_PATTERNS:
        if re.search(pattern, cmd):
            return reason
    # force git clean that stops short of -x (untracked files only) is risky;
    # the -fdx variant is already hard-blocked above.
    if re.search(r"\bgit\s+clean\b", cmd) and re.search(
        r"(?:-\w*f\w*|--force)", cmd):
        return "git clean -f: deletes untracked files"
    return None


def _git_clean_wipes_ignored(cmd: str) -> bool:
    """True for a ``git clean`` that combines force with -x/--ignored."""
    m = re.search(r"\bgit\s+clean\b([^|;&]*)", cmd)
    if not m:
        return False
    opts = m.group(1) or ""
    has_force = bool(re.search(r"(?<!-)-\w*f\w*|--force", opts))
    has_x = bool(re.search(r"-\w*x\w*|--ignored", opts))
    return has_force and has_x


# --------------------------------------------------------------------------- #
# worktree root-escape write guard (teammate confinement in the bash tool)
# --------------------------------------------------------------------------- #

def _escapes_root_write(command: str, root) -> str | None:
    """Return an offending target when ``command`` clearly writes to an absolute
    path outside ``root``.

    Deliberately narrow - only unambiguous absolute-path targets are refused
    (a redirection destination, a rm target, or the destination of mv/cp).
    Anything uncertain is allowed, so normal worktree commands never trip it.
    """
    root_abs = os.path.abspath(str(root))

    def _outside(raw: str) -> bool:
        p = raw.strip().strip("'\"")
        if not p:
            return False
        if p.lower() in ("/dev/null", "nul"):
            return False                      # discard device, not a write
        p = os.path.expanduser(p)
        if not os.path.isabs(p):
            return False
        p_abs = os.path.abspath(p)
        try:
            return os.path.commonpath([p_abs, root_abs]) != root_abs
        except ValueError:                    # different drive -> escape
            return True

    # redirection targets: > file, >> file
    for m in re.finditer(r">>?\s*([^\s;&|<>]+)", command):
        if _outside(m.group(1)):
            return m.group(1)
    # file removal of a single absolute path (force-recursive rm is already blocked)
    for m in re.finditer(r"\b(?:rm|rmdir|del|erase)\s+(?:-\S+\s+)*([^\s;&|<>]+)",
                         command):
        if _outside(m.group(1)):
            return m.group(1)
    # mv / cp destination (last operand)
    for m in re.finditer(r"\b(?:mv|cp)\s+(?:-\S+\s+)*(\S+)\s+(\S+)", command):
        if _outside(m.group(2)):
            return m.group(2)
    return None


def _update_cwd(command: str, current_cwd: str):
    """Track directory changes from cd commands, per thread."""
    # walk each cd in a && chain, resolving relative targets against the dir the
    # previous cd landed in (not the original cwd) so `cd a && cd b` ends in a/b
    running = current_cwd
    changed = False
    for part in command.split("&&"):
        part = part.strip()
        if part.startswith("cd "):
            target = part[3:].strip().strip("'\"")
            if target:
                new_dir = os.path.normpath(os.path.join(running, os.path.expanduser(target)))
                if os.path.isdir(new_dir):
                    # worktree confinement (add3.0): refuse any cd that escapes root
                    root = get_root()
                    if root is not None:
                        root_abs = os.path.abspath(str(root))
                        new_abs = os.path.abspath(new_dir)
                        try:
                            inside = os.path.commonpath([new_abs, root_abs]) == root_abs
                        except ValueError:
                            inside = False          # different drives -> escape
                        if not inside:
                            continue
                    running = new_dir
                    changed = True
    if changed:
        _local.cwd = running
