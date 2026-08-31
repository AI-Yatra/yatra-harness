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
    """Base class for provider failures."""


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

