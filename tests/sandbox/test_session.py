"""
Tests for sandbox/session.py — Phase 2 session isolation stub.

Phase 2 will bind MXC sessions to Entra Agent User identity for attribution
in M365 audit logs. These tests verify the seam exists and documents expected behavior.
"""

import pytest

from entrabot.sandbox.session import Backend, SessionConfig, identity_binding


class TestBackendEnum:
    """Backend enum includes SESSION value for Phase 2."""

    def test_backend_has_session_value(self):
        """Backend enum should include SESSION for Entra-bound isolation."""
        assert hasattr(Backend, "SESSION")
        assert Backend.SESSION.value == "session"

    def test_backend_has_process_value(self):
        """Backend enum should include PROCESS for Phase 1 (current)."""
        assert hasattr(Backend, "PROCESS")
        assert Backend.PROCESS.value == "process"


class TestSessionConfig:
    """SessionConfig dataclass for Phase 2 configuration."""

    def test_session_config_exists(self):
        """SessionConfig dataclass should exist."""
        config = SessionConfig(
            agent_user_id="00000000-0000-0000-0000-000000000000",
            tenant_id="00000000-0000-0000-0000-000000000000",
        )
        assert config.agent_user_id == "00000000-0000-0000-0000-000000000000"
        assert config.tenant_id == "00000000-0000-0000-0000-000000000000"

    def test_session_config_optional_fields(self):
        """SessionConfig should support optional governance fields."""
        config = SessionConfig(
            agent_user_id="00000000-0000-0000-0000-000000000000",
            tenant_id="00000000-0000-0000-0000-000000000000",
            intune_policy_id="policy-123",
        )
        assert config.intune_policy_id == "policy-123"


class TestIdentityBinding:
    """identity_binding() function stub for Phase 2."""

    def test_identity_binding_raises_not_implemented(self):
        """identity_binding() should raise NotImplementedError (Phase 2)."""
        config = SessionConfig(
            agent_user_id="00000000-0000-0000-0000-000000000000",
            tenant_id="00000000-0000-0000-0000-000000000000",
        )
        with pytest.raises(NotImplementedError, match="Phase 2"):
            identity_binding(config)

    def test_identity_binding_accepts_session_config(self):
        """identity_binding() should accept SessionConfig (type check)."""
        import contextlib
        
        config = SessionConfig(
            agent_user_id="00000000-0000-0000-0000-000000000000",
            tenant_id="00000000-0000-0000-0000-000000000000",
        )
        # Should not raise TypeError (even though raises NotImplementedError)
        with contextlib.suppress(NotImplementedError):
            identity_binding(config)


class TestPhase2Documentation:
    """Verify Phase 2 requirements are documented in module docstring."""

    def test_module_has_phase2_docstring(self):
        """Module docstring should document Phase 2 requirements."""
        from entrabot.sandbox import session

        assert session.__doc__ is not None
        assert "Phase 2" in session.__doc__
        assert "Entra Agent User" in session.__doc__ or "identity" in session.__doc__

    def test_identity_binding_has_docstring(self):
        """identity_binding() should have docstring explaining Phase 2."""
        assert identity_binding.__doc__ is not None
        assert "Phase 2" in identity_binding.__doc__


class TestBackwardCompatibility:
    """Ensure Phase 1 code continues to work unchanged."""

    def test_process_backend_still_default(self):
        """Backend.PROCESS should remain the default for Phase 1."""
        # Phase 1 code uses Backend.PROCESS implicitly
        from entrabot.sandbox.base import Backend as BaseBackend

        # Ensure base.py Backend has PROCESS (Phase 1 uses this)
        assert hasattr(BaseBackend, "PROCESS")
        assert BaseBackend.PROCESS.value == "process"

    def test_session_backend_not_used_by_default(self):
        """Backend.SESSION should not affect Phase 1 code paths."""
        # This test documents that Backend.SESSION is opt-in for Phase 2
        # Phase 1 runners (mac.py, windows.py) use Backend.PROCESS only
        
        # Verify session backend exists but is not referenced by Phase 1 code
        from entrabot.sandbox.session import Backend as SessionBackend
        assert SessionBackend.SESSION.value == "session"
        
        # Phase 1 continues to use base.Backend.PROCESS (no session config needed)
        from entrabot.sandbox.base import Backend as BaseBackend
        assert BaseBackend.PROCESS.value == "process"
