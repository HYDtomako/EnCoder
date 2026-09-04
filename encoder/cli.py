"""Interactive REPL - the user-facing terminal interface."""

import argparse
import os
import sys

from prompt_toolkit import prompt as pt_prompt
from prompt_toolkit.history import FileHistory
from prompt_toolkit.key_binding import KeyBindings
from rich.console import Console
from rich.markdown import Markdown
from rich.panel import Panel

from . import __version__
from .agent import Agent
from .config import Config
from .cron_scheduler import get_scheduler
from .llm import LLM, LiteLLM
from .session import list_sessions, load_session, save_session
from .task import PRIORITIES, STATES

console = Console()


def _parse_args():
    p = argparse.ArgumentParser(
        prog="encoder",
        description="Minimal AI coding agent. Works with any OpenAI-compatible LLM.",
    )
    p.add_argument("-m", "--model", help="Model name (default: $ENCODER_MODEL or gpt-5.5)")
    p.add_argument("--base-url", help="API base URL (default: $OPENAI_BASE_URL)")
    p.add_argument("--api-key", help="API key (default: $OPENAI_API_KEY)")
    p.add_argument("-p", "--prompt", help="One-shot prompt (non-interactive mode)")
    p.add_argument("--demo", action="store_true", help="Run the offline scripted demo (no API key needed)")
    p.add_argument("-r", "--resume", metavar="ID", help="Resume a saved session")
    p.add_argument("--daemon", action="store_true", help="Run as a background task daemon: fires scheduled tasks with no interactive REPL")
    p.add_argument("--tui", action="store_true", help="Launch the full-screen Textual TUI as the interactive layer (default is the classic REPL)")
    p.add_argument("-v", "--version", action="version", version=f"%(prog)s {__version__}")
    return p.parse_args()


def main():
    args = _parse_args()

    if args.demo and args.tui:
        # 离线验证 TUI:`--tui --demo` 用脚本化 LLM 自动播放真实 Agent loop
        import tempfile
        from pathlib import Path

        from .demo import _script
        from .llm import ScriptedLLM
        from .tui import run_tui

        workdir = Path(tempfile.mkdtemp(prefix="encoder-demo-"))
        agent = Agent(llm=ScriptedLLM(_script(workdir)), memory_enabled=False)
        raise SystemExit(run_tui(agent, Config.from_env(), demo=True))

    if args.demo:
        from .demo import run_demo
        raise SystemExit(run_demo())

    config = Config.from_env()

    # CLI args override env vars
    if args.model:
        config.model = args.model
    if args.base_url:
        config.base_url = args.base_url
    if args.api_key:
        config.api_key = args.api_key

    if not config.api_key:
        console.print("[red bold]No API key found.[/]")
        console.print(
            "Set one of: OPENAI_API_KEY, DEEPSEEK_API_KEY, or ENCODER_API_KEY\n"
            "\nExamples:\n"
            "  # OpenAI\n"
            "  export OPENAI_API_KEY=sk-...\n"
            "\n"
            "  # DeepSeek\n"
            "  export OPENAI_API_KEY=sk-... OPENAI_BASE_URL=https://api.deepseek.com\n"
            "\n"
            "  # Ollama (local)\n"
            "  export OPENAI_API_KEY=ollama OPENAI_BASE_URL=http://localhost:11434/v1 ENCODER_MODEL=qwen2.5-coder\n"
        )
        sys.exit(1)

    llm_cls = LiteLLM if config.provider == "litellm" else LLM
    llm = llm_cls(
        model=config.model,
        api_key=config.api_key,
        base_url=config.base_url,
        temperature=config.temperature,
        max_tokens=config.max_tokens,
    )
    agent = Agent(
        llm=llm,
        max_context_tokens=config.max_context_tokens,
        memory_enabled=config.memory_enabled,
        memory_llm=config.memory_llm,
        team_enabled=config.team_enabled,
        team_worktrees=config.team_worktrees,
        team_max=config.team_max,
        team_model=config.team_model,
        team_api_key=config.team_api_key,
        team_base_url=config.team_base_url,
        integration_model=config.integration_model,
        integration_api_key=config.integration_api_key,
        integration_base_url=config.integration_base_url,
    )

    # resume saved session
    if args.resume:
        loaded = load_session(args.resume)
        if loaded:
            agent.messages, loaded_model = loaded
            # restore the model from the saved session unless overridden by CLI
            if not args.model:
                agent.llm.model = loaded_model
                config.model = loaded_model
            console.print(f"[green]Resumed session: {args.resume} (model: {agent.llm.model})[/green]")
        else:
            console.print(f"[red]Session '{args.resume}' not found.[/red]")
            sys.exit(1)

    # one-shot mode
    if args.prompt:
        _run_once(agent, args.prompt)
        return

    # background task daemon: fire scheduled tasks, no REPL
    if args.daemon:
        _daemon(agent)
        return

    # full-screen Textual TUI interactive layer (default stays the classic REPL)
    if args.tui:
        from .tui import run_tui
        raise SystemExit(run_tui(agent, config))

    # interactive REPL
    _repl(agent, config)


def _run_once(agent: Agent, prompt: str):
    """Non-interactive: run one prompt and exit."""
    def on_token(tok):
        print(tok, end="", flush=True)

    def on_tool(name, kwargs):
        console.print(f"\n[dim]> {name}({_brief(kwargs)})[/dim]")

    try:
        agent.chat(prompt, on_token=on_token, on_tool=on_tool)
    except KeyboardInterrupt:
        console.print("\n[yellow]Interrupted.[/yellow]")
        sys.exit(130)
    except Exception as e:
        console.print(f"\n[red]Error: {e}[/red]")
        sys.exit(1)
    print()


def _run_scheduled(agent: Agent, task, on_tool=None):
    """Run one scheduled task through the agent and log the result."""
    console.print(f"\n[cyan]⚡ Scheduled task fired: {task.time}[/cyan] {task.content}")
    chunks: list[str] = []

    def on_token(tok):
        chunks.append(tok)
        print(tok, end="", flush=True)

    try:
        agent.chat(task.content, on_token=on_token, on_tool=on_tool)
    except KeyboardInterrupt:
        console.print("\n[yellow]Task interrupted.[/yellow]")
        chunks.append("\n[interrupted]")
    except Exception as e:
        console.print(f"\n[red]Task error: {e}[/red]")
        chunks.append(f"\n[error: {e}]")
    if chunks:
        print()

    # append to ~/.encoder/tasks.log for review when nobody watched
    _append_task_log(task, "".join(chunks))


def _append_task_log(task, output: str):
    import time as _time
    from pathlib import Path
    try:
        log = Path.home() / ".encoder" / "tasks.log"
        log.parent.mkdir(parents=True, exist_ok=True)
        stamp = _time.strftime("%Y-%m-%d %H:%M:%S")
        with log.open("a", encoding="utf-8") as f:
            f.write(f"[{stamp}] {task.task_id} {task.time} {task.content}\n")
            if output:
                f.write(output.strip() + "\n")
            f.write("=" * 60 + "\n")
    except OSError:
        pass


def _daemon(agent: Agent):
    """常驻后台：不进入 REPL，只执行到点的定时任务。

    配合系统开机自启 / 计划任务可将 agent 作为离线任务的常驻进程。
    """
    scheduler = agent.scheduler
    scheduler.start()

    if not scheduler.list_tasks():
        console.print("[yellow]No scheduled tasks. Add one in a REPL first.[/yellow]")
        scheduler.stop()
        return

    console.print(f"[bold]Encoder daemon[/bold] running with [cyan]{len(scheduler.list_tasks())}[/cyan] "
                  f"scheduled task(s). Ctrl+C to stop.")
    try:
        while True:
            task = scheduler.queue.get()
            _run_scheduled(agent, task)
    except KeyboardInterrupt:
        console.print("\n[green]Daemon stopped.[/green]")
    finally:
        scheduler.stop()


def _repl(agent: Agent, config: Config):
    """Interactive read-eval-print loop."""
    scheduler = agent.scheduler
    scheduler.start()

    console.print(Panel(
        f"[bold]Encoder[/bold] v{__version__}\n"
        f"Model: [cyan]{config.model}[/cyan]"
        + (f"  Base: [dim]{config.base_url}[/dim]" if config.base_url else "")
        + "\nType [bold]/help[/bold] for commands, [bold]Ctrl+C[/bold] to cancel, [bold]quit[/bold] to exit.",
        border_style="blue",
    ))

    hist_path = os.path.expanduser("~/.encoder_history")
    history = FileHistory(hist_path)

    # Enter submits, Escape+Enter inserts a newline (for pasting code blocks etc.)
    kb = KeyBindings()

    @kb.add("enter")
    def _submit(event):
        event.current_buffer.validate_and_handle()

    @kb.add("escape", "enter")
    def _newline(event):
        event.current_buffer.insert_text("\n")

    while True:
        # fire any scheduled tasks that came due while we were idle.  Done
        # before prompt so a triggered task never interrupts an active round
        # (add.md: run triggers in the next loop, not mid-request).
        try:
            while True:
                task = scheduler.queue.get_nowait()
                _run_scheduled(agent, task)
        except Exception:
            pass  # queue.Empty

        try:
            user_input = pt_prompt(
                "You > ",
                history=history,
                multiline=True,
                key_bindings=kb,
                prompt_continuation="...  ",
            ).strip()
        except (EOFError, KeyboardInterrupt):
            console.print("\nBye!")
            scheduler.stop()
            _release_team(agent)
            break

        if not user_input:
            continue

        # built-in commands
        if user_input.lower() in ("quit", "exit", "/quit", "/exit"):
            scheduler.stop()
            _release_team(agent)
            break
        if user_input == "/help":
            _show_help()
            continue
        if user_input == "/reset":
            agent.reset()
            console.print("[yellow]Conversation reset.[/yellow]")
            continue
        if user_input == "/tokens":
            p = agent.llm.total_prompt_tokens
            c = agent.llm.total_completion_tokens
            line = f"Tokens: [cyan]{p}[/cyan] prompt + [cyan]{c}[/cyan] completion = [bold]{p+c}[/bold] total"
            cost = agent.llm.estimated_cost
            if cost is not None:
                line += f"  (~${cost:.4f})"
            console.print(line)
            continue
        if user_input == "/model" or user_input.startswith("/model "):
            new_model = user_input[7:].strip() if user_input.startswith("/model ") else ""
            if new_model:
                agent.llm.model = new_model
                config.model = new_model
                console.print(f"Switched to [cyan]{new_model}[/cyan]")
            else:
                console.print(f"Current model: [cyan]{config.model}[/cyan]")
            continue
        if user_input == "/compact":
            from .context import estimate_tokens
            before = estimate_tokens(agent.messages)
            compressed = agent.context.maybe_compress(agent.messages, agent.llm)
            after = estimate_tokens(agent.messages)
            if compressed:
                console.print(f"[green]Compressed: {before} → {after} tokens ({len(agent.messages)} messages)[/green]")
            else:
                console.print(f"[dim]Nothing to compress ({before} tokens, {len(agent.messages)} messages)[/dim]")
            continue
        if user_input == "/save":
            sid = save_session(agent.messages, config.model)
            console.print(f"[green]Session saved: {sid}[/green]")
            console.print(f"Resume with: encoder -r {sid}")
            continue
        if user_input == "/diff":
            from .tools.edit import _changed_files
            if not _changed_files:
                console.print("[dim]No files modified this session.[/dim]")
            else:
                console.print(f"[bold]Files modified this session ({len(_changed_files)}):[/bold]")
                for f in sorted(_changed_files):
                    console.print(f"  [cyan]{f}[/cyan]")
            continue
        if user_input == "/sessions":
            sessions = list_sessions()
            if not sessions:
                console.print("[dim]No saved sessions.[/dim]")
            else:
                for s in sessions:
                    console.print(f"  [cyan]{s['id']}[/cyan] ({s['model']}, {s['saved_at']}) {s['preview']}")
            continue
        if user_input == "/crontab" or user_input.startswith("/crontab "):
            _cmd_crontab(user_input)
            continue
        if user_input == "/memory" or user_input.startswith("/memory "):
            _cmd_memory(agent, user_input)
            continue
        if user_input == "/task" or user_input.startswith("/task "):
            _cmd_task(agent, user_input)
            continue
        if user_input == "/team" or user_input.startswith("/team "):
            _cmd_team(agent, user_input)
            continue

        # an unknown /command shouldn't be sent to the model as a prompt
        if user_input.startswith("/"):
            console.print(f"[yellow]Unknown command: {user_input.split()[0]} (try /help)[/yellow]")
            continue

        # call the agent
        streamed: list[str] = []

        def on_token(tok):
            streamed.append(tok)
            print(tok, end="", flush=True)

        def on_tool(name, kwargs):
            console.print(f"\n[dim]> {name}({_brief(kwargs)})[/dim]")

        # a new user request starts a fresh todo plan (tasks in .TASK/ persist)
        agent.todos.clear()

        try:
            response = agent.chat(user_input, on_token=on_token, on_tool=on_tool)
            if streamed:
                print()  # newline after streamed tokens
            else:
                # response wasn't streamed (came after tool calls)
                console.print(Markdown(response))
        except KeyboardInterrupt:
            console.print("\n[yellow]Interrupted.[/yellow]")
        except Exception as e:
            console.print(f"\n[red]Error: {e}[/red]")


def _cmd_crontab(user_input: str):
    """Handle the /crontab command (list, or delete <id>)."""
    sched = get_scheduler()
    parts = user_input.strip().split()
    if len(parts) >= 2 and parts[1].lower() in ("del", "delete", "rm"):
        if len(parts) < 3:
            console.print("[yellow]Usage: /crontab delete <task_id>[/yellow]")
            return
        task_id = parts[2]
        if sched.delete_task(task_id):
            console.print(f"[green]Deleted task {task_id}.[/green]")
        else:
            console.print(f"[yellow]No task with id '{task_id}'.[/yellow]")
        return

    tasks = sched.list_tasks()
    if not tasks:
        console.print("[dim]No scheduled tasks. Ask the agent e.g. "
                      "'every day at 8am, collect the AI news and summarize it'.[/dim]")
        return
    console.print(f"[bold]Scheduled tasks ({len(tasks)}):[/bold]")
    for t in tasks:
        last = t.last_fired or "never"
        console.print(f"  [cyan]{t.task_id}[/cyan]  {t.time}  [dim]last fired: {last}[/dim]\n"
                      f"      {t.content}")
    console.print("\n[dim]Delete with: /crontab delete <task_id>[/dim]")


def _cmd_memory(agent: Agent, user_input: str):
    """Handle /memory: list, show, forget, organize, resolve, on/off."""
    mem = getattr(agent, "memory", None)
    if mem is None:
        console.print("[yellow]Memory is not available (agent created with memory_enabled=False).[/yellow]")
        return

    parts = user_input.strip().split(maxsplit=2)
    cmd = parts[1] if len(parts) > 1 else ""
    arg = parts[2] if len(parts) > 2 else ""

    if cmd == "show":
        console.print(Markdown(mem.show(arg)))
    elif cmd == "forget":
        if mem.forget(arg):
            console.print(f"[green]Forgot '{arg}' (snapshot kept under .MEMORY/.snapshots).[/green]")
        else:
            console.print(f"[yellow]No memory named '{arg}'.[/yellow]")
    elif cmd in ("resolve", "organize"):
        if cmd == "resolve":
            _cmd_memory_resolve(mem)
            return

        def ask(question, options):
            console.print(question)
            choice = pt_prompt("选择 (keep_new / keep_old / merge): ").strip().lower()
            return choice if choice in options else "keep_new"

        changed = mem.organize(ask=ask if agent.memory_enabled else None)
        if not changed:
            console.print("[dim]Nothing to organize.[/dim]")
        else:
            for c in changed:
                console.print(f"  [cyan]{c}[/cyan]")
    elif cmd in ("off", "on"):
        agent.memory_enabled = cmd == "on"
        console.print(f"Memory {'enabled' if agent.memory_enabled else 'disabled'} for this session.")
    elif not cmd:
        metas = mem.list_meta()
        if not metas:
            console.print("[dim]No memories yet. Mention a durable preference or ask "
                          "for something worth remembering, then /memory organize.[/dim]")
            return
        console.print(f"[bold]Memories ({len(metas)}):[/bold] [dim]stored in .MEMORY/<category>.md[/dim]")
        for m in sorted(metas, key=lambda x: x.updated, reverse=True):
            console.print(f"  [cyan]{m.category}/{m.title}[/cyan]  ({m.updated})  {m.desc}")
            if m.keywords:
                console.print(f"      keywords: {m.keywords}")
            console.print(f"      [dim]run: /memory show {m.title}[/dim]")
    else:
        console.print("[yellow]Usage: /memory | show <name> | forget <name> | organize | resolve | on | off[/yellow]")


def _cmd_memory_resolve(mem):
    """Interactively resolve pending memory conflicts (keep_new / keep_old / merge)."""
    pending = mem.pending_notices()
    if not pending:
        console.print("[dim]No pending memory conflicts.[/dim]")
        return
    console.print(f"[bold]{len(pending)} pending memory update(s):[/bold]")
    for i, c in enumerate(pending, 1):
        console.print(f"  [{i}] {c.get('title')} ({c.get('category')})\n"
                      f"      旧（{c.get('old_date', '')}）：{c.get('old_entry', '')}\n"
                      f"      新：{c.get('new_text', '')}")

    def ask(question, options):
        console.print(question)
        choice = pt_prompt("选择 (keep_new / keep_old / merge): ").strip().lower()
        return choice if choice in options else "keep_new"

    changed = mem.organize(ask=ask)
    if changed:
        for c in changed:
            console.print(f"  [cyan]{c}[/cyan]")
    else:
        console.print("[dim]Nothing resolved.[/dim]")


def _cmd_task(agent: Agent, user_input: str):
    """Handle /task: list, show <id>, update <id> <state|priority>,
    archive <root_id>, clear."""
    tm = getattr(agent, "tasks", None)
    if tm is None:
        console.print("[yellow]Tasks not available.[/yellow]")
        return

    tokens = user_input.strip().split()
    if tokens and tokens[0] == "/task":
        tokens = tokens[1:]
    cmd = tokens[0] if tokens else ""

    if cmd == "show":
        if len(tokens) < 2:
            console.print("[yellow]Usage: /task show <task_id>[/yellow]")
            return
        task = tm.get(tokens[1])
        if task is None:
            console.print(f"[yellow]No task '{tokens[1]}'.[/yellow]")
            return
        console.print(Markdown(
            f"- **id**: {task.task_id}\n"
            f"- **state**: {task.state}  (priority: {task.priority})\n"
            f"- **agent**: {task.agent or '(unassigned)'}\n"
            f"- **description**: {task.description}\n"
            f"- **blockedBy**: {task.blockedBy or '-'}\n"
            f"- **attempts**: {task.attempts}/{_max_attempts()}\n"
            f"- **last_error**: {task.last_error or '-'}\n"
            f"- **result**: {task.result or '-'}"
        ))
    elif cmd == "update":
        if len(tokens) < 3:
            console.print("[yellow]Usage: /task update <task_id> <state|priority>[/yellow]")
            return
        tid, val = tokens[1], tokens[2]
        try:
            if val in STATES:
                task = tm.update(tid, state=val)
            elif val in PRIORITIES:
                task = tm.update(tid, priority=val)
            else:
                console.print(f"[yellow]'{val}' is not a valid state or priority.[/yellow]")
                return
        except (ValueError, KeyError) as e:
            console.print(f"[yellow]{e}[/yellow]")
            return
        console.print(f"[green]{task.task_id} -> state={task.state} priority={task.priority}[/green]")
    elif cmd == "archive":
        if len(tokens) < 2:
            console.print("[yellow]Usage: /task archive <root_id>[/yellow]")
            return
        try:
            archived = tm.archive(tokens[1])
        except (KeyError, RuntimeError) as e:
            console.print(f"[yellow]{e}[/yellow]")
            return
        console.print(f"[green]Archived {len(archived)} task(s) into .TASK/done/.[/green]")
    elif cmd == "clear":
        confirm = pt_prompt("Delete ALL active tasks in .TASK/ (archive kept)? (yes/no): ").strip().lower()
        if confirm == "yes":
            tm.clear()
            console.print("[green]Cleared all active tasks.[/green]")
        else:
            console.print("[dim]Cancelled.[/dim]")
    else:
        tasks = tm.list()
        if not tasks:
            console.print("[dim]No tasks. Multi-step requests now auto-persist a task "
                          "plan to .TASK/ and run it to completion; leftover unfinished "
                          "tasks are reminded at the start of each request.[/dim]")
            return
        console.print(f"[bold]Tasks ({len(tasks)}):[/bold]")
        for t in tasks:
            line = f"  [cyan]{t.task_id}[/cyan]  {t.state:<11}  {t.description}"
            if t.priority != "normal":
                line += f"  [dim]priority: {t.priority}[/dim]"
            if t.blockedBy:
                line += f"  [dim]blockedBy: {t.blockedBy}[/dim]"
            if t.last_error:
                line += f"  [yellow]last_error: {t.last_error}[/yellow]"
            console.print(line)


def _release_team(agent: Agent):
    """End every teammate before the process exits so no worker thread is leaked."""
    team = getattr(agent, "team", None)
    if team is not None:
        team.release_all()


def _cmd_team(agent: Agent, user_input: str):
    """Handle /team: status, on, off, release [name]."""
    parts = user_input.strip().split()
    cmd = parts[1] if len(parts) > 1 else ""

    if cmd == "on":
        if agent.team is None:
            # teammate mode was off at construction; build the manager now
            from .team import TeamManager
            agent.team = TeamManager(lead=agent,
                                     worktrees=agent.team_worktrees,
                                     max_teammates=3,
                                     team_model=agent.team_model,
                                     team_api_key=agent.team_api_key,
                                     team_base_url=agent.team_base_url,
                                     integration_model=agent.integration_model,
                                     integration_api_key=agent.integration_api_key,
                                     integration_base_url=agent.integration_base_url)
            agent.team_enabled = True
        isolated = "on" if agent.team.worktrees else "off"
        console.print(f"[green]Teammate mode on. You are the Lead; teammates "
                      f"are persistent workers (default worktree isolation: "
                      f"{isolated} — code-editing teammates run in a .worktrees/"
                      f" branch and come back via integrate_results).[/green]")
        return

    if cmd == "off":
        _release_team(agent)
        agent.team = None
        agent.team_enabled = False
        console.print("[yellow]Teammate mode off. Team tools now return a hint "
                      "until re-enabled.[/yellow]")
        return

    team = getattr(agent, "team", None)
    if cmd == "release":
        if team is None:
            console.print("[yellow]Teammate mode is off.[/yellow]")
            return
        if len(parts) < 3:
            console.print("[yellow]Usage: /team release <name>[/yellow]")
            return
        name = parts[2]
        if team.release(name):
            console.print(f"[green]Sent end to {name}.[/green]")
        else:
            console.print(f"[yellow]No teammate named '{name}'.[/yellow]")
        return

    if cmd == "integrate":
        if team is None:
            console.print("[yellow]Teammate mode is off.[/yellow]")
            return
        console.print("[dim]Merging released worktree teammates into the current "
                      "branch... (a genuine conflict will start the Integration "
                      "Agent)[/dim]")
        try:
            out = team.integrate()
        except Exception as e:
            console.print(f"[red]integrate error: {e}[/red]")
            return
        console.print(out)
        return

    # default: status
    if team is None:
        console.print("[dim]Teammate mode is off. Enable with /team on "
                      "(the Lead only suggests; you decide).[/dim]")
        return
    statuses = team.status()
    if not statuses:
        console.print("[dim]Teammate mode on, no teammates yet. The Lead can "
                      "spawn them with spawn_teammate.[/dim]")
        return
    console.print(f"[bold]Teammates ({len(statuses)}):[/bold]")
    for s in statuses:
        line = f"  [cyan]{s['name']}[/cyan]  {s['status']}"
        if s.get("task_id"):
            line += f"  [dim](task {s['task_id']})[/dim]"
        console.print(line)


def _max_attempts() -> int:
    from .task import MAX_ATTEMPTS
    return MAX_ATTEMPTS


def _show_help():
    console.print(Panel(
        "[bold]Commands:[/bold]\n"
        "  /help          Show this help\n"
        "  /reset         Clear conversation history\n"
        "  /model         Show current model\n"
        "  /model <name>  Switch model mid-conversation\n"
        "  /tokens        Show token usage\n"
        "  /compact       Compress conversation context\n"
        "  /diff          Show files modified this session\n"
        "  /save          Save session to disk\n"
        "  /sessions      List saved sessions\n"
        "  /crontab       List scheduled tasks\n"
        "  /crontab del   Delete a scheduled task: /crontab delete <id>\n"
        "  /memory        List memories; /memory show <name> | forget <name>\n"
        "                 /memory organize | resolve | on | off\n"
        "  /task          List tasks; /task show <id> | update <id> <state|priority>\n"
        "                 /task archive <root_id> | clear\n"
        "  /team          Teammate mode: status | on | off | release <name>\n"
        "                 /team integrate   Merge released worktree teammates back\n"
        "  quit           Exit Encoder\n"
        "\n"
        "[bold]Input:[/bold]\n"
        "  Enter          Submit message\n"
        "  Esc+Enter      Insert newline (for pasting code)",
        title="Encoder Help",
        border_style="dim",
    ))


def _brief(kwargs: dict, maxlen: int = 80) -> str:
    s = ", ".join(f"{k}={repr(v)[:40]}" for k, v in kwargs.items())
    return s[:maxlen] + ("..." if len(s) > maxlen else "")
