"""Orchestration control plane for governed sessions, vendor adapters, approvals, tools, and the supervisor daemon.

Modules here may own processes and sockets by design, so the package is allowlisted for the record-handler guard's subprocess, socket, asyncio, and http.server ban set.
"""
