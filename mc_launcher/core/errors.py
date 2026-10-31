"""Exceptions used by the launcher core."""


class LauncherError(Exception):
    """A user-facing launcher error."""


class VersionError(LauncherError):
    """The version JSON or one of its files is invalid or incomplete."""
