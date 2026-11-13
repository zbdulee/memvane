"""Learning-host adapter for the shared runtime calibration contract."""


def verdict(*args, **kwargs):
    from memvane.runtime.api import resource
    return resource.calibration_verdict(*args, **kwargs)


def load(*args, **kwargs):
    from memvane.runtime.api import resource
    return resource.load_calibration(*args, **kwargs)


def _sha(*args, **kwargs):
    from memvane.runtime.api import resource
    return resource.calibration_sha(*args, **kwargs)
