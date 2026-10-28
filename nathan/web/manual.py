"""The manual lane: bring ChatGPT / Claude website answers into a vote, legally.

There's no free API for those models, and scraping their websites breaks their terms
(hard rule 1). So Nathan writes a ready-to-paste prompt, you paste it into the site
yourself, and paste the answer back. It then counts as one more candidate for the judge.
"""

from __future__ import annotations

SITES = {
    "claude.ai": "https://claude.ai/new",
    "chatgpt.com": "https://chatgpt.com/",
    "gemini.google.com": "https://gemini.google.com/app",
}


def manual_prompt(task: str) -> str:
    return (
        "Please answer the task below as well as you can. Give a complete, self-contained "
        "answer; if something is uncertain, say so briefly.\n\n"
        f"TASK:\n{task.strip()}\n"
    )
