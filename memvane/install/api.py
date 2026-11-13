"""Public installation operations."""
from __future__ import annotations


def cancel_wake(*args, **kwargs):
    from memvane.install.runtime_wake_cancel import cancel as implementation
    return implementation(*args, **kwargs)


def wake_status(*args, **kwargs):
    from memvane.install.runtime_wake_cancel import status as implementation
    return implementation(*args, **kwargs)


def plan_fresh_wiki_origin(*args, **kwargs):
    from memvane.install.product_install import plan_fresh_wiki_origin as implementation
    return implementation(*args, **kwargs)


def review_local_provider(*args, **kwargs):
    from memvane.install.product_install import review_local_provider as implementation
    return implementation(*args, **kwargs)


def prepare_fresh_wiki_origin(*args, **kwargs):
    from memvane.install.product_install import prepare_fresh_wiki_origin as implementation
    return implementation(*args, **kwargs)


def private_parent(*args, **kwargs):
    from memvane.install.product_install import _private_parent as implementation
    return implementation(*args, **kwargs)


def initial_build_status(*args, **kwargs):
    from memvane.install.product_install import initial_build_status as implementation
    return implementation(*args, **kwargs)


def request_initial_build_rescan(*args, **kwargs):
    from memvane.install.product_install import request_initial_build_rescan as implementation
    return implementation(*args, **kwargs)


def finalize_initial_build(*args, **kwargs):
    from memvane.install.product_install import finalize_initial_build as implementation
    return implementation(*args, **kwargs)


def restore_origin_selection(*args, **kwargs):
    from memvane.install.product_install import restore_origin_selection as implementation
    return implementation(*args, **kwargs)


def read_private(*args, **kwargs):
    from memvane.install.product_install import _read_private as implementation
    return implementation(*args, **kwargs)


def install_lock(*args, **kwargs):
    from memvane.install.product_install import _install_lock as implementation
    return implementation(*args, **kwargs)


def activate_origin_selection(*args, **kwargs):
    from memvane.install.product_install import activate_origin_selection as implementation
    return implementation(*args, **kwargs)


def recover_origin_selection(*args, **kwargs):
    from memvane.install.product_install import recover_origin_selection as implementation
    return implementation(*args, **kwargs)
