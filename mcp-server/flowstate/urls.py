"""One spelling for URL and issuer strings that are compared or joined: no trailing '/'."""


def no_trailing_slash(url: str) -> str:
    """`url` without any trailing '/' (all of them, like str.rstrip). Not for request paths or
    file paths: those strip '/' for their own reasons where they are used."""
    return url.rstrip("/")
