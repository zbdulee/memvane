"""Learning-host adapter for the shared runtime calibration contract."""


def verdict(*args, **kwargs):
    from memvane.runtime.api import calibration_verdict
    return calibration_verdict(*args, **kwargs)


def load(*args, **kwargs):
    from memvane.runtime.api import load_calibration
    return load_calibration(*args, **kwargs)


def _sha(*args, **kwargs):
    from memvane.runtime.api import calibration_sha
    return calibration_sha(*args, **kwargs)
