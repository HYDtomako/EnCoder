"""Checkpoint tool - let the model mark a recovery point it knows matters.

Most snapshots are event-driven (see ``encoder/checkpoint.py``): the policy layer
decides. But some boundaries only the model can recognise -- "the refactor is
done, now I'll touch the schema", "this is a good place to review before I
continue". ``checkpoint(label)`` is the explicit ⑥ trigger from
``design_ckeckpoint.md``: it always writes a snapshot (fingerprint de-dupe
exempt) and the label becomes the human-readable name in ``/checkpoint list``.

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
        "in /checkpoint list. This records YOUR state - conversation, todos, "
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
        },
        "required": ["label"],
    }

    # set by Agent.__init__ (like AgentTool._parent_agent)
    _parent_agent = None

    def execute(self, label: str) -> str:
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

        # "manual" always writes (it is in FORCE_TRIGGERS), so the head is the
        # checkpoint this call just produced
        cps.record("manual", actor="lead", name=label, data={"label": label})
        cp_id = cps.head_id()
        if not cp_id:
            return "checkpoint 写入失败（已忽略，不影响继续工作）"
        return (f"已记录断点 {cp_id}「{label}」。"
                f"当前共 {cps.count()} 个断点，可用 /checkpoint list 查看、"
                f"/checkpoint restore <id> 回退。")
