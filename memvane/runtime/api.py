"""Public resource admission operations."""
from __future__ import annotations


def adapter_for(*args, **kwargs):
    from memvane.runtime.resource_platform import adapter_for as implementation
    return implementation(*args, **kwargs)


def normal_pressure(*args, **kwargs):
    from memvane.runtime.resource_platform import normal_pressure as implementation
    return implementation(*args, **kwargs)


def number(*args, **kwargs):
    from memvane.runtime.resource_platform import number as implementation
    return implementation(*args, **kwargs)


def handler_done(*args, **kwargs):
    from memvane.runtime.runtime_wake_policy import handler_done as implementation
    return implementation(*args, **kwargs)


def sync_owner(*args, **kwargs):
    from memvane.runtime.sync_owner_lock import sync_owner as implementation
    return implementation(*args, **kwargs)


def validate_sync_owner(*args, **kwargs):
    from memvane.runtime.sync_owner_lock import validate_sync_owner as implementation
    return implementation(*args, **kwargs)

from memvane.runtime.types import PreSendDeferred


def qa_budget_initialize(*args, **kwargs):
    from memvane.runtime.qa_model_budget import initialize
    return initialize(*args, **kwargs)


def qa_budget_status(*args, **kwargs):
    from memvane.runtime.qa_model_budget import status
    return status(*args, **kwargs)


def reserve_qa_budget(*args, **kwargs):
    from memvane.runtime.qa_model_budget import reserve_from_env
    return reserve_from_env(*args, **kwargs)


def request(*args, **kwargs):
    from memvane.runtime.runtime_work import request
    return request(*args, **kwargs)


def preparation(*args, **kwargs):
    from memvane.runtime.runtime_admission import preparation as implementation
    return implementation(*args, **kwargs)



def preparation_checkpoint(*args, **kwargs):
    from memvane.runtime.runtime_admission import preparation_checkpoint as implementation
    return implementation(*args, **kwargs)



def prepared(*args, **kwargs):
    from memvane.runtime.runtime_admission import prepared as implementation
    return implementation(*args, **kwargs)



def provider(*args, **kwargs):
    from memvane.runtime.runtime_admission import provider as implementation
    return implementation(*args, **kwargs)



def provider_popen(*args, **kwargs):
    from memvane.runtime.runtime_admission import provider_popen as implementation
    return implementation(*args, **kwargs)



def provider_run(*args, **kwargs):
    from memvane.runtime.runtime_admission import provider_run as implementation
    return implementation(*args, **kwargs)



def provider_job(*args, **kwargs):
    from memvane.runtime.runtime_admission import provider_job as implementation
    return implementation(*args, **kwargs)



def provider_call(*args, **kwargs):
    from memvane.runtime.runtime_admission import provider_call as implementation
    return implementation(*args, **kwargs)



def provider_checkpoint(*args, **kwargs):
    from memvane.runtime.runtime_admission import provider_checkpoint as implementation
    return implementation(*args, **kwargs)



def hash_file(*args, **kwargs):
    from memvane.runtime.runtime_admission import hash_file as implementation
    return implementation(*args, **kwargs)


def hook_wake(*args, **kwargs):
    from memvane.runtime.runtime_work import hook_wake as implementation
    return implementation(*args, **kwargs)



def wake_queue(*args, **kwargs):
    from memvane.runtime.runtime_work import WakeQueue as implementation
    return implementation(*args, **kwargs)



def runtime_main(*args, **kwargs):
    from memvane.runtime.runtime_work import main as implementation
    return implementation(*args, **kwargs)


def configure_torch(*args, **kwargs):
    from memvane.runtime.runtime_cpu import configure_torch as implementation
    return implementation(*args, **kwargs)



def environment(*args, **kwargs):
    from memvane.runtime.runtime_cpu import environment as implementation
    return implementation(*args, **kwargs)



def lease(*args, **kwargs):
    from memvane.runtime.runtime_cpu import lease as implementation
    return implementation(*args, **kwargs)



def in_platform_temp(*args, **kwargs):
    from memvane.runtime.runtime_lease import in_platform_temp as implementation
    return implementation(*args, **kwargs)



def platform_temp_roots(*args, **kwargs):
    from memvane.runtime.runtime_lease import platform_temp_roots as implementation
    return implementation(*args, **kwargs)
