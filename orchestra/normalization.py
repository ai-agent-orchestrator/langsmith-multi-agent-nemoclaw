"""Defense-in-depth inspection copies; original business payloads remain untouched."""
import unicodedata


def inspection_copy(text: str) -> str:
    """Fold compatibility text, marks, zero-width formatters, whitespace and common leet digits."""
    text = unicodedata.normalize("NFKD", unicodedata.normalize("NFKC", text))
    text = "".join(c for c in text if unicodedata.category(c) not in {"Cf", "Mn", "Mc", "Me"})
    text = unicodedata.normalize("NFKC", text).translate(str.maketrans("10", "io"))
    return " ".join(text.split())
