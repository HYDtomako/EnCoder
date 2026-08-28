"""Tests for the file-based memory system (encoder/memory.py), v2 layout.

v2 layout (per DESIGN_memory_v2.md / xuigai.md):
    .MEMORY/index.json          JSON index (derived, rebuilt from .md)
    .MEMORY/<category>.md       one file per category, topics as ``## [title]`` blocks
    .MEMORY/.snapshots/<ts>/    snapshots before destructive edits
    .MEMORY/.conflicts.json     contradictory memories awaiting the user

All cases run under a tmp dir so nothing real is ever written to .MEMORY/.
min-LLM calls are either absent (llm=None -> keyword fallback) or scripted with
ScriptedLLM so no network is touched.
"""

import json

from encoder.llm import LLMResponse, ScriptedLLM
from encoder.memory import MemoryManager


def _mgr(tmp_path, llm=None, **kw):
    return MemoryManager(base_dir=tmp_path / "mem", llm=llm, **kw)


def _store(mm, title, category, desc, update="new", text=None):
    """Store via the same path extract uses, without needing an LLM."""
    return mm._apply_decision(
        {"store": True, "title": title, "category": category,
         "desc": desc, "update": update},
        text or desc,
    )


# ---------------------------------------------------------------- store

def test_store_creates_category_file_and_index(tmp_path):
    mm = _mgr(tmp_path)
    _store(mm, "饮食偏好", "personal_prefer", "用户对花生过敏且不能吃辣")

    # one file per category (no user-xxx.md fragmentation)
    assert (tmp_path / "mem" / "personal_prefer.md").exists()
    assert not (tmp_path / "mem" / "personal_prefer").exists()
    text = (tmp_path / "mem" / "personal_prefer.md").read_text(encoding="utf-8")
    assert "## [饮食偏好]" in text

    metas = mm.list_meta()
    assert len(metas) == 1
    assert metas[0].title == "饮食偏好"
    assert metas[0].category == "personal_prefer"

    # index.json exists and is valid JSON with the derived record
    idx = json.loads((tmp_path / "mem" / "index.json").read_text(encoding="utf-8"))
    assert idx["version"] == 2
    assert len(idx["memories"]) == 1
    assert idx["memories"][0]["title"] == "饮食偏好"
    assert idx["memories"][0]["category"] == "personal_prefer"


def test_store_survives_reload_and_hand_edit(tmp_path):
    mm = _mgr(tmp_path)
    _store(mm, "deploy", "work", "生产部署需要 export ENCODER_ENV=prod")

    mm2 = _mgr(tmp_path)  # fresh instance -> reload from disk
    assert mm2.list_meta()[0].title == "deploy"

    # a hand edit to the .md (truth source) is picked up, never overwritten
    fp = tmp_path / "mem" / "work.md"
    fp.write_text(fp.read_text(encoding="utf-8") + "\n- 2026-08-25  手工补充的条目\n",
                  encoding="utf-8")
    assert len(mm2.list_meta()[0].entries) == 2


def test_category_outside_set_falls_back_to_general(tmp_path):
    mm = _mgr(tmp_path)
    _store(mm, "mystery", "bogus", "未知分类")
    assert mm.list_meta()[0].category == "general"


def test_keywords_describe_the_topic(tmp_path):
    """keywords come from the topic's own title/desc, never an unrelated input."""
    mm = _mgr(tmp_path)
    _store(mm, "Python 使用偏好", "personal_prefer", "用户写代码习惯使用 Python 语言")
    t = mm.list_meta()[0]
    assert any("python" in k for k in t.keywords)
    # no bigram junk like "火锅" from an unrelated sentence can leak in
    assert all("火锅" not in k for k in t.keywords)


# ---------------------------------------------------------------- recall

def test_recall_keyword_fallback(tmp_path):
    mm = _mgr(tmp_path)
    _store(mm, "deploy", "work", "生产部署需要 export ENCODER_ENV=prod")
    block = mm.recall("怎么生产部署？")
    assert block and "deploy" in block


def test_recall_empty_index_returns_none(tmp_path):
    assert _mgr(tmp_path).recall("你好") is None


def test_recall_follow_up_reuses_block(tmp_path):
    mm = _mgr(tmp_path)
    _store(mm, "deploy", "work", "生产部署需要 export ENCODER_ENV=prod")
    b1 = mm.recall("生产部署流程")
    b2 = mm.recall("继续")  # short follow-up -> reuse existing block
    assert b1 is not None and b2 == b1


# ---------------------------------------------------------------- extract

def test_extract_stores_via_min_llm(tmp_path):
    script = ScriptedLLM([LLMResponse(
        content='{"store": true, "title": "中文注释偏好", "category": "personal_prefer", '
               '"desc": "用户写代码喜欢用中文注释", "update": "new"}')])
    mm = _mgr(tmp_path, llm=script)
    result = mm.extract_and_store([
        {"role": "user", "content": "记住：我写代码喜欢用中文注释，请帮我以后都这样。"},
        {"role": "assistant", "content": "好的，以后都用中文注释写作。"},
    ])
    assert result.stored and result.action == "new"
    assert mm.list_meta()[0].title == "中文注释偏好"


def test_extract_temporary_words_block(tmp_path):
    mm = _mgr(tmp_path)
    result = mm.extract_and_store([
        {"role": "user", "content": "本次我们先不提交，暂时把代码放工作区，等明天再处理相关细节。"},
    ])
    assert not result.stored
    assert mm.list_meta() == []


def test_extract_weak_hints_skipped(tmp_path):
    mm = _mgr(tmp_path)
    result = mm.extract_and_store([
        {"role": "user", "content": "帮我看看 src/main.py 第 42 行的函数签名是什么，再顺便跑一下测试。"},
    ])
    assert not result.stored


def test_extract_keyword_placement_appends_to_existing(tmp_path):
    mm = _mgr(tmp_path)
    _store(mm, "deploy", "work", "生产部署用 qwen 模型")
    result = mm.extract_and_store([
        {"role": "user", "content": "记住：生产部署前要先登录内网机器，这一步必须由开发人员人工执行，"
                                    "不能用机器人自动跑。"},
    ])
    assert result.stored
    # still one topic (no new topic created), now with a second entry
    assert len(mm.list_meta()) == 1
    assert len(mm.list_meta()[0].entries) == 2


def test_duplicate_entry_is_not_reappended(tmp_path):
    mm = _mgr(tmp_path)
    _store(mm, "deploy", "work", "生产部署用 qwen 模型")
    # appending the exact same fact -> skipped as duplicate
    result = mm._apply_decision(
        {"store": True, "title": "deploy", "category": "work",
         "desc": "生产部署用 qwen 模型", "update": "append"},
        "生产部署用 qwen 模型")
    assert result.stored
    assert len(mm.list_meta()[0].entries) == 1


# ---------------------------------------------------------------- organize

def test_organize_integrates_over_capacity(tmp_path):
    script = ScriptedLLM([
        LLMResponse(content="- 2026-08-23 整合后的长期目标记忆"),
    ])
    mm = _mgr(tmp_path, llm=script, max_entries=3)
    for n in ["日志方案 alpha", "构建流程 beta", "部署脚本 gamma", "测试框架 delta"]:
        _store(mm, "goal", "long", n)

    changed = mm.organize()
    assert any("integrated goal" in c for c in changed)
    assert len(mm.list_meta()[0].entries) <= 3


def test_organize_merges_duplicates(tmp_path):
    mm = _mgr(tmp_path)
    _store(mm, "a", "work", "部署使用 qwen 模型跑")
    _store(mm, "b", "work", "部署也使用 qwen 模型跑")

    changed = mm.organize()
    assert any("merged duplicate" in c for c in changed)
    assert any((tmp_path / "mem" / ".snapshots").glob("**/*.md"))
    assert len(mm.list_meta()) == 1


def test_conflict_queued_then_resolved_keep_new(tmp_path):
    mm = _mgr(tmp_path)
    _store(mm, "pref", "personal_prefer", "喜欢用 vim 编辑")

    # a contradictory statement -> queued, file NOT silently overwritten
    result = _store(mm, "pref", "personal_prefer", "讨厌用 vim 编辑", update="conflict")
    assert result.action == "conflict"
    assert not result.stored
    assert mm.conflicts_path.exists()

    # the stored file still holds only the old fact
    assert "讨厌用 vim" not in (tmp_path / "mem" / "personal_prefer.md").read_text(encoding="utf-8")

    resolved = mm.organize(ask=lambda q, o: "keep_new")
    assert any("conflict pref" in c for c in resolved)
    assert any((tmp_path / "mem" / ".snapshots").glob("**/*.md"))
    # pending list cleared, and the file now records the update
    assert mm._load_conflicts() == []
    assert "更新取代" in (tmp_path / "mem" / "personal_prefer.md").read_text(encoding="utf-8")


def test_conflict_resolve_keep_old_drops_new(tmp_path):
    mm = _mgr(tmp_path)
    _store(mm, "pref", "personal_prefer", "喜欢用 vim 编辑")
    _store(mm, "pref", "personal_prefer", "讨厌用 vim 编辑", update="conflict")

    resolved = mm.organize(ask=lambda q, o: "keep_old")
    assert any("kept old" in c for c in resolved)
    assert "讨厌用 vim" not in (tmp_path / "mem" / "personal_prefer.md").read_text(encoding="utf-8")
    assert mm._load_conflicts() == []


# ---------------------------------------------------------------- summary

def test_ingest_summary_stores_durable_facts(tmp_path):
    script = ScriptedLLM([LLMResponse(
        content='{"store": true, "title": "项目技术栈", "category": "work", '
               '"desc": "项目使用 Python 3.12 与 FastAPI", "update": "new"}')])
    mm = _mgr(tmp_path, llm=script)
    # summary path is NOT blocked by a temporary word that appears inside it
    n = mm.ingest_summary("用户现在决定项目使用 Python 3.12 与 FastAPI 作为技术栈。")
    assert n == 1
    assert mm.list_meta()[0].category == "work"


def test_ingest_summary_ignores_nothing_durable(tmp_path):
    script = ScriptedLLM([LLMResponse(content='{"store": false}')])
    mm = _mgr(tmp_path, llm=script)
    assert mm.ingest_summary("这条摘要里没有值得长期记忆的内容") == 0


# ---------------------------------------------------------------- migration

def test_legacy_layout_is_migrated(tmp_path):
    mem = tmp_path / "mem"
    (mem / "personal_prefer").mkdir(parents=True)
    (mem / "personal_prefer" / "user-allergy-diet.md").write_text(
        "- 2026-08-24  用户对花生过敏且不能吃辣\n", encoding="utf-8")
    (mem / "memory.md").write_text(
        "- name: user-python-preference | type: personal_prefer | "
        "keywords: user,习惯 | desc: 用户习惯使用Python语言 | "
        "content: 用户习惯使用Python语言 | created: 2026-08-24 | updated: 2026-08-24\n",
        encoding="utf-8")

    mm = MemoryManager(base_dir=mem)  # migration runs on construction
    metas = mm.list_meta()
    assert metas, "migration should have produced topics"

    # single category file, no per-topic files, no old index
    assert (mem / "personal_prefer.md").exists()
    assert not (mem / "memory.md").exists()
    assert not (mem / "personal_prefer").exists()
    assert any((mem / ".snapshots").glob("legacy_*"))


# ---------------------------------------------------------------- CLI support

def test_forget_snapshots_then_removes(tmp_path):
    mm = _mgr(tmp_path)
    _store(mm, "old", "work", "老记忆")
    assert mm.forget("old")
    assert "## [old]" not in (tmp_path / "mem" / "work.md").read_text(encoding="utf-8")
    assert mm.list_meta() == []
    assert any((tmp_path / "mem" / ".snapshots").glob("**/*.md"))


def test_show_renders_meta_and_entries(tmp_path):
    mm = _mgr(tmp_path)
    _store(mm, "goal", "long", "目标是可教学")
    s = mm.show("goal")
    assert "type: long" in s and "name: goal" in s and "目标是可教学" in s
    assert "no memory" in mm.show("missing")
