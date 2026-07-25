from __future__ import annotations

from app.scoring import Candidate


DEPENDENT_OPENINGS = (
    "ama ",
    "ancak ",
    "bu nedenle",
    "bu yüzden",
    "bunun için",
    "çünkü ",
    "dediğim gibi",
    "ikinci neden",
    "ikinci sebep",
    "o yüzden",
    "ve ",
)
INCOMPLETE_ENDINGS = (
    "ama",
    "ancak",
    "çünkü",
    "fakat",
    "ise",
    "ve",
    "veya",
    "yani",
)


def passes_deterministic_critic(candidate: Candidate) -> bool:
    """Conservatively check whether an adjusted clip still has obvious loose edges."""
    text = " ".join(candidate.text.casefold().split())
    if not text:
        return False
    if any(text.startswith(opening) for opening in DEPENDENT_OPENINGS):
        return False
    last_word = text.rstrip(" .!?…\"')").split()[-1]
    return last_word not in INCOMPLETE_ENDINGS

