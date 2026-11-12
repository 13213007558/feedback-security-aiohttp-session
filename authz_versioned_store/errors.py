"""Typed errors for the versioned authorization store.

Every failure mode that must *not* silently degrade a security decision has
its own exception type, so callers (and tests) can distinguish
"refuse downgrade" from "unknown field" and "stale write".
"""


class AuthzError(Exception):
    """Base class for all versioned-authz failures."""


class VersionRollbackError(AuthzError):
    """A process attempted to persist a lower schema version.

    Raised before any write happens; the stored record is untouched.
    """


class DowngradeNotLosslessError(AuthzError):
    """v2 state cannot be represented in v1 without changing semantics.

    The caller must refuse the downgrade rather than truncating permissions
    or flattening constraints.
    """


class UnknownFieldError(AuthzError):
    """Strict-mode reader encountered a field its schema does not know.

    Strict mode is used for authorization-relevant reads. The value is kept
    in the record's opaque ``unknown`` bucket and is never used to grant
    access; strict readers reject instead of ignoring it.
    """


class StaleWriteError(AuthzError):
    """Compare-and-swap failed: the record changed after it was loaded.

    This is what stops an old process from clobbering a newer save.
    """


class PartialMigrationError(AuthzError):
    """At least one record in a migration batch could not be converted.

    A batch migration is all-or-nothing: nothing is committed when any
    record fails.
    """


class IncompatibleRecordError(AuthzError):
    """Envelope/version is malformed or newer than the reader understands."""
