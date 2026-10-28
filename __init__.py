"""Hermes Agent plugin entrypoint for MemVane."""

from .hermes_adapter.plugin import register

__all__ = ["register"]
