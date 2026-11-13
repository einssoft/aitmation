"""aitmation core.

The technical automation core: state machine over five primitives
(Trigger, AgentStep, HumanGate, SystemAction, Timer), persistence,
audit log, template loader and the generic approval web UI.

Deliberately domain-neutral: reading or producing documents is a
technical capability of the core; what a document *means* (an invoice,
an application, an order) lives in the template layer, never here.
"""

__version__ = "0.0.1"
