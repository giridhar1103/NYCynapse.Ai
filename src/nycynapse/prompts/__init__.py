"""Prompts live as text files next to this module so they can be read, diffed and versioned.
Each run records the hash of every prompt it used."""

import hashlib
from functools import cache
from importlib import resources


@cache
def prompt(name: str) -> str:
    return resources.files(__name__).joinpath(f"{name}.md").read_text()


def prompt_version(name: str) -> str:
    return hashlib.sha256(prompt(name).encode()).hexdigest()[:8]
