"""Version du logiciel. Le numéro du milieu suit l'étape de développement (0.4 = étape 4).

Chaque nouvelle version publiée sur GitHub avec l'étiquette ``v<version>`` est proposée
automatiquement dans l'application.
"""
__version__ = "0.5.0"
RELEASE_NAME = "Les six agents"


def parse_version(text: str) -> tuple[int, ...]:
    """'v1.2.3' -> (1, 2, 3). Les suffixes (ex. '-beta') sont ignorés."""
    cleaned = text.strip().lstrip("vV").split("-")[0].split("+")[0]
    parts: list[int] = []
    for piece in cleaned.split("."):
        digits = "".join(ch for ch in piece if ch.isdigit())
        parts.append(int(digits) if digits else 0)
    while len(parts) < 3:
        parts.append(0)
    return tuple(parts)


def is_newer(candidate: str, current: str = __version__) -> bool:
    return parse_version(candidate) > parse_version(current)
