"""Encoder - Minimal AI coding agent inspired by Claude Code's architecture."""

__version__ = "0.4.0"

from encoder.agent import Agent
from encoder.llm import LLM
from encoder.config import Config
from encoder.tools import ALL_TOOLS

__all__ = ["Agent", "LLM", "Config", "ALL_TOOLS", "__version__"]
