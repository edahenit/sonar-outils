"""Control plane for the cross-instance SonarQube migration.

This package only decides and traces: validation, identity resolution,
authorization check, state machine, journal. Any action taken on the
SonarQube servers is the responsibility of the Ansible roles in ``ansible/``.
"""

__all__ = ["__version__"]

__version__ = "0.1.0"
