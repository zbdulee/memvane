"""Deterministic prompts for the single bounded synthetic fantasy experiment.

Only launcher outputs, never a supplied project file, become provenance.
Changing this source invalidates the owner-pinned generator specification.
"""
from __future__ import annotations

import hashlib
from pathlib import Path


def source_sha256():
    return hashlib.sha256(Path(__file__).read_bytes()).hexdigest()


def prompt(spec, kind, episode, slot, *, writing=None, main=None, final=None,
           prior_docs=None):
    if not 1 <= episode <= 100 or slot not in (1, 2):
        raise ValueError("invalid_synthetic_episode")
    anchor = hashlib.sha256(f"{spec['seed']}:{episode}".encode()).hexdigest()[:12]
    if kind == "writing" and slot == 1:
        context = ""
        if prior_docs:
            context = "\nPrevious synthetic setting, characters and plot:\n" + "\n".join(
                f"{name}:\n{body}" for name, body in prior_docs.items())
        temporary = ("Create a short temporary omen note." if episode == 1 else
                     "Return temporary as null; the prior temporary omen is retired." if episode == 2 else
                     "Return temporary as null.")
        return (f"Write wholly invented fantasy episode {episode:03d} using seed {anchor}. "
                "Premise: the last library preserves forgotten spells as magic disappears. "
                "First establish the setting, plot and characters, then write the episode. "
                "Preserve continuity from the prior synthetic documents while allowing new "
                "fictional developments. No real documents, people or external sources. "
                "Return only JSON with string keys prose, setting, plot, characters and "
                f"temporary (string or null). {temporary} Write prose in Korean "
                "with at least 5,000 non-whitespace characters in the narrative body alone, "
                "excluding any title, headings, notes or metadata. Return prose "
                "as body text only, without Markdown headings. "
                "Develop scenes without filler or repetitive passages."
                + context)
    if kind == "wiki_create" and slot == 1 and writing:
        return ("From this newly generated fictional episode, create two distinct "
                "fictional fact summaries. Aim for roughly 600 characters per card "
                "while keeping each fact complete. Return exactly JSON with string keys main "
                "and final. Each value is plain body text without YAML or headings. "
                "The two values must describe different facts. Episode:\n" + writing)
    if kind == "wiki_review" and slot == 1 and writing and main and final:
        return ("Check these two synthetic cards against the newly generated "
                "episode. Aim for roughly 600 characters per card while keeping "
                "each fact complete. Return exactly JSON with string keys main and final, "
                "each a corrected plain fact body without YAML or headings. Do not add unrelated "
                "facts. Episode:\n" + writing + "\nMain:\n" + main + "\nFinal:\n" + final)
    if kind == "query" and slot in (1, 2):
        target = "main" if slot == 1 else "final"
        return (f"For the newly invented fantasy episode {episode:03d}, "
                f"which established {target} fact is recorded in its wiki card? "
                f"Reference seed {anchor}.")
    raise ValueError("synthetic_generator_input_missing")
