"""Value types for runtime resource admission."""
from memvane.kernel.resource_deferred import ResourceDeferred


class PreSendDeferred(ResourceDeferred):
    """A provider launch was deferred before a child was sent."""
