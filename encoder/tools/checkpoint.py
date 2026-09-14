"""Checkpoint tool - let the model mark a recovery point it knows matters.

Most snapshots are event-driven (see ``encoder/checkpoint.py``): the policy layer
decides. But some boundaries only the model can recognise -- "the refactor is
done, now I'll touch the schema", "this is a good place to review before I
continue". ``checkpoint(label)`` is the explicit ⑥ trigger from
``design_ckeckpoint.md``: it always writes a snapshot (fingerprint de-dupe
exempt) and the label becomes the human-readable name in ``/checkpoint list``.
The optional ``next``/``reason`` arguments are v2 §11.3: the model writes down
what only it knows -- the step it is about to take and why -- so that a restored
session continues the work rather than re-deciding it.

This is *not* a git commit or a file snapshot: it records the agent's state
(messages, todos, task view, environment fingerprint), never file contents.
"""

from .base import Tool


class CheckpointTool(Tool):
    name = "checkpoint"
    description = (
        "Mark a recovery point before a risky or hard-to-undo stretch of work. "
        "Call it when you finish a coherent stage (and would want to come back "
        "to this exact state if the next stage goes wrong), before a large "
        "refactor, or before handing work off. The label becomes the name shown "
        "in /checkpoint list. Pass `next` when you already know the next tool "
        "call - the session may be restored later and will then continue "
        "instead of asking. This records YOUR state - conversation, todos, "
        "task view, environment - it is NOT a git commit and does NOT snapshot "
        "or restore files."
    )
    parameters = {
        "type": "object",
        "properties": {
            "label": {
                "type": "string",
                "description": (
                    "Short human-readable name for this point, e.g. "
                    "'重构完成，准备改 schema'"
                ),
            },
            "next": {
                "type": "string",
                "description": (
                    "Optional: the tool you intend to call next, e.g. "
                    "'review_teammate'. Only you can see this boundary, so "
                    "writing it down is what lets a restored session pick the "
                    "work up instead of re-deciding from scratch."
                ),
            },
            "next_args": {
                "type": "object",
                "description": "Optional: arguments for that next call.",
            },
            "reason": {
                "type": "string",
                "description": (
                    "Optional: why you are stopping here and what this stage "
                    "was for. Recorded with the recovery point."
                ),
            },
        },
        "required": ["label"],
    }

    # set by Agent.__init__ (like AgentTool._parent_agent)
    _parent_agent = None

    def execute(self, label: str, next: str = "", next_args=None,
                reason: str = "") -> str:
        parent = self._parent_agent
        if parent is None:
            return "Error: checkpoint tool not initialized"
        label = str(label or "").strip()
        if not label:
            return "Error: label must not be empty"

        cps = getattr(parent, "checkpoints", None)
        if cps is None:
            return ("checkpoint 未启用（ENCODER_CHECKPOINT_ENABLED=0）；"
                    "这一节点没有被记录，继续工作即可。")

        data = {"label": label, "reason": str(reason or "").strip()}
        nxt = str(next or "").strip()
        if nxt:
            # shape-checked only. The registry lists what exists; whether the
            # call *makes sense* next is the model's call, and it is no less
            # trustworthy here than on an ordinary tool call.
            data["next"] = nxt
            data["next_args"] = next_args if isinstance(next_args, dict) else {}

        # "manual" always writes (it is in FORCE_TRIGGERS), so the head is the
        # checkpoint this call just produced
        cps.record("manual", actor="lead", name=label, data=data)
        cp_id = cps.head_id()
        if not cp_id:
            return "checkpoint 写入失败（已忽略，不影响继续工作）"
        return (f"已记录断点 {cp_id}「{label}」。"
                f"当前共 {cps.count()} 个断点，可用 /checkpoint list 查看、"
                f"/checkpoint restore <id> 回退。")
