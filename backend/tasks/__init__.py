"""Reserved implementations of the browser-task version 1 ports.

Factory exports: control.create_control(settings), executor.create_executor(settings),
assistance.create_assistance(settings), observation.create_observation(settings).
Factories are synchronous and side-effect free. Services bind through TaskBindings;
only startup performs I/O. routes.create_router(bindings) and
assistance.create_router(bindings) return relative APIRouters mounted once at the
task API prefix. Profile/help routes precede the dynamic task-ID route.
Use '/' for the admission route; the bootstrap also serves the canonical prefix
without a trailing slash, without redirecting or replaying the POST body.
"""
