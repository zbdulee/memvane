"""Public resource admission operations."""
from __future__ import annotations


def qa_budget_initialize(*args, **kwargs):
    from memvane.runtime.qa_model_budget import initialize
    return initialize(*args, **kwargs)


def qa_budget_status(*args, **kwargs):
    from memvane.runtime.qa_model_budget import status
    return status(*args, **kwargs)


def reserve_qa_budget(*args, **kwargs):
    from memvane.runtime.qa_model_budget import reserve_from_env
    return reserve_from_env(*args, **kwargs)


def request_wake(*args, **kwargs):
    from memvane.runtime.runtime_work import request
    return request(*args, **kwargs)
