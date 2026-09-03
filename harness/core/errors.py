"""Typed harness failures used to make terminal behavior explicit."""


class HarnessError(Exception):
    """Base class for expected harness failures."""


class ConfigurationError(HarnessError):
    """Configuration or task input is invalid."""


class StateError(HarnessError):
    """Persisted state is missing, corrupt, or incompatible."""


class WorkspaceError(HarnessError):
    """A workspace operation violated containment or lifecycle rules."""


class PolicyDenied(HarnessError):
    """A requested capability is not authorized."""


class ApprovalRequired(HarnessError):
    """A requested capability requires an approval decision."""


class ToolError(HarnessError):
    """A registered tool could not complete its request."""


class ProviderError(HarnessError):
    """Base class for provider failures.

    Carries the HTTP status when there was one, so a caller can decide
    whether another route would do better without parsing the message. A 400
    means our request is wrong and will be wrong everywhere; a 429 means this
    route is out of budget and another one may not be.
    """

    def __init__(self, message: str, status: int = 0, retry_after: float = 0.0) -> None:
        super().__init__(message)
        #: Seconds the provider asked us to wait, from its Retry-After header.
        #: Zero when it did not say, which is when the caller's own backoff is
        #: the best available guess.
        self.retry_after = max(0.0, float(retry_after))
        self.status = status

    @property
    def route_is_exhausted(self) -> bool:
        """True when a different route is worth trying for the same request.

        400 and 422 are the request's fault and would fail identically
        everywhere, so moving on would burn every configured key for nothing.
        Everything else is this route's fault: an expired key, a missing
        model, a quota, an outage, or -- with no status at all -- a refused
        connection, which is exactly what a local server that is not running
        looks like.
        """
        return self.status not in (400, 422)


class TransientProviderError(ProviderError):
    """Provider failure that may be retried or routed around."""


class PermanentProviderError(ProviderError):
    """Provider failure that should not be retried unchanged."""


class ProviderExhausted(ProviderError):
    """All configured routes failed."""


class BudgetExceeded(HarnessError):
    """A declared run budget has been exhausted."""


class VerificationError(HarnessError):
    """The verifier itself could not execute safely."""


class InjectedCrash(HarnessError):
    """Deterministic teaching fault raised after a durable checkpoint."""


class MCPError(HarnessError):
    """MCP transport, lifecycle, or tool invocation failure."""


class RoutingError(HarnessError):
    """LLM Light could not produce a valid route ordering from declared priorities."""

