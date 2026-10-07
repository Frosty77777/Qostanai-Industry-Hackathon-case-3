"""Local security detection, isolated from webcam and CV processing."""

from .security_monitor import (
    KeyInput, SecurityConfig, SecurityMonitor, SecurityRule, WindowsSecurityBackend,
)

__all__ = [
    "KeyInput", "SecurityConfig", "SecurityMonitor", "SecurityRule",
    "WindowsSecurityBackend",
]
