"""Tests for scripts/blueprint_certs.py: the keyCredentials read/merge/write rules.

Regression context (#124): Graph treats ``PATCH /applications/{id}`` as replacing
the whole ``keyCredentials`` collection, so attaching one machine used to remove
every other machine's certificate.
"""

from __future__ import annotations

import base64
import copy
import hashlib
import json
import re
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import httpx
import keyring
import pytest
from cryptography import x509

from tests.scripts._blueprint_cert_helpers import (
    FAKE_OBJECT_ID,
    FAKE_TOKEN,
    FakeGraph,
    block_real_keyring_and_network,
    blueprint_certs,
    graph_entry,
    load_script,
    make_cert,
    use_fake_graph,
)

rotate_cert_windows = load_script("rotate_cert_windows", alias="_rotate_cert_windows_under_test")

HOST_A = "EntraBot Device Certificate — host-a"
HOST_B = "EntraBot Device Certificate — host-b"
SAME_NAME = "EntraBot Device Certificate — MacBook-Pro"

CERT_A = make_cert("host-a")
CERT_B = make_cert("host-b")
CERT_NEW = make_cert("host-new")

NOW = datetime(2026, 7, 1, tzinfo=UTC)


@pytest.fixture(autouse=True)
def _no_real_keychain_or_network(monkeypatch: pytest.MonkeyPatch) -> None:
    block_real_keyring_and_network(monkeypatch)


def _entries() -> list[dict[str, Any]]:
    return [
        graph_entry(CERT_A, key_id="00000000-0000-0000-0000-00000000000a", display_name=HOST_A),
        graph_entry(CERT_B, key_id="00000000-0000-0000-0000-00000000000b", display_name=HOST_B),
    ]


def _new_entry(display_name: str = "EntraBot Device Certificate — host-new"):
    return blueprint_certs.build_key_credential(CERT_NEW.der, display_name)


def _register(**kwargs: Any) -> Any:
    sleeps: list[float] = []
    kwargs.setdefault("new_entry", _new_entry())
    result = blueprint_certs.register_key_credential(
        object_id=FAKE_OBJECT_ID, token=FAKE_TOKEN, sleep=sleeps.append, **kwargs
    )
    return result, sleeps


class TestSafetyHarness:
    def test_real_keyring_access_raises(self) -> None:
        # Read-only, against a service that does not exist: if the block were
        # ever broken this test fails without touching a real credential.
        with pytest.raises(AssertionError, match="real OS keychain"):
            keyring.get_password("entrabot-test-sentinel", "never-exists")

    def test_real_network_access_raises(self) -> None:
        with pytest.raises(AssertionError, match="real network"):
            httpx.get("https://graph.invalid/", timeout=1.0)


class TestFetchKeyCredentials:
    def test_returns_entries_and_uses_the_single_object_select(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        graph = use_fake_graph(monkeypatch, FakeGraph(_entries()))

        credentials = blueprint_certs.fetch_key_credentials(FAKE_OBJECT_ID, FAKE_TOKEN)

        assert [c.raw for c in credentials] == _entries()
        request = graph.requests[0]
        assert request.method == "GET"
        assert request.url.host == "graph.microsoft.com"
        assert request.url.path == f"/v1.0/applications/{FAKE_OBJECT_ID}"
        assert request.url.params["$select"] == "keyCredentials"
        assert request.headers["authorization"] == f"Bearer {FAKE_TOKEN}"

    def test_an_empty_list_on_200_is_a_real_answer(self, monkeypatch: pytest.MonkeyPatch) -> None:
        use_fake_graph(monkeypatch, FakeGraph([]))
        assert blueprint_certs.fetch_key_credentials(FAKE_OBJECT_ID, FAKE_TOKEN) == []

    @pytest.mark.parametrize("status", [201, 204, 400, 401, 403, 404, 429, 500, 503])
    def test_any_status_other_than_200_raises(
        self, monkeypatch: pytest.MonkeyPatch, status: int
    ) -> None:
        use_fake_graph(monkeypatch, FakeGraph(_entries(), get_script=[status]))
        with pytest.raises(blueprint_certs.KeyCredentialsReadError) as caught:
            blueprint_certs.fetch_key_credentials(FAKE_OBJECT_ID, FAKE_TOKEN)
        assert caught.value.status == status

    def test_transport_failure_raises(self, monkeypatch: pytest.MonkeyPatch) -> None:
        use_fake_graph(monkeypatch, FakeGraph(_entries(), get_script=[httpx.ConnectTimeout("x")]))
        with pytest.raises(blueprint_certs.KeyCredentialsReadError):
            blueprint_certs.fetch_key_credentials(FAKE_OBJECT_ID, FAKE_TOKEN)

    @pytest.mark.parametrize(
        "body",
        [
            b"not json",
            b"[]",
            b"null",
            b"{}",
            b'{"value": []}',
            b'{"keyCredentials": null}',
            b'{"keyCredentials": {}}',
            b'{"keyCredentials": ["not-an-object"]}',
            b'{"keyCredentials": [{"displayName": "no keyId"}]}',
            b'{"error": {"code": "x", "message": "y"}}',
        ],
    )
    def test_malformed_200_is_never_an_empty_list(
        self, monkeypatch: pytest.MonkeyPatch, body: bytes
    ) -> None:
        use_fake_graph(monkeypatch, FakeGraph(_entries(), get_script=[body]))
        with pytest.raises(blueprint_certs.KeyCredentialsReadError):
            blueprint_certs.fetch_key_credentials(FAKE_OBJECT_ID, FAKE_TOKEN)

    def test_empty_object_id_raises_without_a_request(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # An empty id would otherwise turn the GET into a list of every application.
        graph = use_fake_graph(monkeypatch, FakeGraph(_entries()))
        with pytest.raises(blueprint_certs.KeyCredentialsReadError):
            blueprint_certs.fetch_key_credentials("", FAKE_TOKEN)
        assert graph.requests == []

    def test_error_text_never_contains_the_token(self, monkeypatch: pytest.MonkeyPatch) -> None:
        use_fake_graph(monkeypatch, FakeGraph(_entries(), get_script=[403]))
        with pytest.raises(blueprint_certs.KeyCredentialsReadError) as caught:
            blueprint_certs.fetch_key_credentials(FAKE_OBJECT_ID, FAKE_TOKEN)
        assert FAKE_TOKEN not in str(caught.value)
        assert "fake failure 403" in str(caught.value)


class TestKeyCredential:
    def test_thumbprint_is_sha256_base64url_of_the_der(self) -> None:
        credential = blueprint_certs.KeyCredential(raw=_entries()[0])
        digest = hashlib.sha256(CERT_A.der).digest()
        assert credential.thumbprint == base64.urlsafe_b64encode(digest).rstrip(b"=").decode()
        assert credential.der == CERT_A.der

    @pytest.mark.parametrize("key", [None, "", "!!!not-base64!!!", 12345])
    def test_unreadable_key_material_has_no_thumbprint(self, key: Any) -> None:
        entry = {**_entries()[0], "key": key}
        credential = blueprint_certs.KeyCredential(raw=entry)
        assert credential.der is None
        assert credential.thumbprint is None

    def test_summary_shows_only_name_and_expiry(self) -> None:
        entry = graph_entry(
            CERT_A,
            key_id="00000000-0000-0000-0000-00000000000a",
            display_name=HOST_A,
            end="2027-03-04T05:06:07Z",
        )
        summary = blueprint_certs.KeyCredential(raw=entry).summary()
        assert HOST_A in summary
        assert "2027-03-04" in summary
        assert "05:06:07" not in summary
        assert CERT_A.b64 not in summary
        assert entry["keyId"] not in summary
        assert entry["customKeyIdentifier"] not in summary

    def test_summary_tolerates_missing_fields(self) -> None:
        summary = blueprint_certs.KeyCredential(raw={"keyId": "x"}).summary()
        assert "?" in summary


class TestMergeKeyCredentials:
    def _existing(self) -> list[Any]:
        return [blueprint_certs.KeyCredential(raw=entry) for entry in _entries()]

    def test_append_keeps_every_existing_entry_and_adds_the_new_one(self) -> None:
        existing = self._existing()
        new_entry = _new_entry()

        merged = blueprint_certs.merge_key_credentials(existing, new_entry)

        assert [c.raw for c in merged] == [*_entries(), new_entry.raw]

    def test_existing_entries_pass_through_exactly_as_graph_returned_them(self) -> None:
        odd = {
            "keyId": "00000000-0000-0000-0000-0000000000cc",
            "displayName": None,
            "key": None,
            "customKeyIdentifier": "ABCD",
            "someFieldGraphAddsLater": {"nested": [1, 2, 3]},
        }
        existing = [blueprint_certs.KeyCredential(raw=copy.deepcopy(odd))]

        merged = blueprint_certs.merge_key_credentials(existing, _new_entry())

        assert merged[0].raw == odd

    def test_two_hosts_with_the_same_display_name_both_survive(self) -> None:
        # Many Macs are literally named MacBook-Pro: a display-name de-dupe would
        # silently bring back #124.
        existing = [
            blueprint_certs.KeyCredential(
                raw=graph_entry(CERT_A, key_id="k-1", display_name=SAME_NAME)
            ),
            blueprint_certs.KeyCredential(
                raw=graph_entry(CERT_B, key_id="k-2", display_name=SAME_NAME)
            ),
        ]
        new_entry = _new_entry(display_name=SAME_NAME)

        merged = blueprint_certs.merge_key_credentials(existing, new_entry)

        assert [c.key_id for c in merged[:2]] == ["k-1", "k-2"]
        assert len(merged) == 3
        assert merged[2].thumbprint == CERT_NEW.thumbprint

    def test_remove_all_existing_returns_only_the_new_entry(self) -> None:
        new_entry = _new_entry()
        merged = blueprint_certs.merge_key_credentials(
            self._existing(), new_entry, remove_all_existing=True
        )
        assert [c.raw for c in merged] == [new_entry.raw]

    def test_does_not_mutate_its_inputs(self) -> None:
        existing = self._existing()
        snapshot = copy.deepcopy([c.raw for c in existing])
        blueprint_certs.merge_key_credentials(existing, _new_entry())
        assert [c.raw for c in existing] == snapshot
        assert len(existing) == 2


class TestBuildKeyCredential:
    def test_fields_and_dates_come_from_the_certificate(self) -> None:
        credential = blueprint_certs.build_key_credential(CERT_NEW.der, HOST_A)
        cert = x509.load_der_x509_certificate(CERT_NEW.der)

        assert credential.raw == {
            "type": "AsymmetricX509Cert",
            "usage": "Verify",
            "key": CERT_NEW.b64,
            "displayName": HOST_A,
            "startDateTime": cert.not_valid_before_utc.strftime("%Y-%m-%dT%H:%M:%S.0000000Z"),
            "endDateTime": cert.not_valid_after_utc.strftime("%Y-%m-%dT%H:%M:%S.0000000Z"),
        }
        assert re.fullmatch(
            r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}\.0{7}Z", credential.raw["startDateTime"]
        )

    def test_thumbprint_matches_the_der(self) -> None:
        assert (
            blueprint_certs.build_key_credential(CERT_NEW.der, HOST_A).thumbprint
            == CERT_NEW.thumbprint
        )


class TestDisplayName:
    @pytest.mark.parametrize(
        ("hostname", "expected"),
        [
            ("fake-host.local", "fake-host"),
            ("fake-host", "fake-host"),
            ("fake-host.corp.example.com", "fake-host"),
            ("", "unknown"),
        ],
    )
    def test_uses_the_short_hostname(self, hostname: str, expected: str) -> None:
        assert (
            blueprint_certs.device_certificate_display_name(hostname)
            == f"EntraBot Device Certificate — {expected}"
        )


class TestRegisterFailClosed:
    """No PATCH can happen unless the GET returned 200."""

    @pytest.mark.parametrize(
        "outcome",
        [
            401,
            403,
            404,
            429,
            500,
            503,
            httpx.ConnectTimeout("x"),
            httpx.ReadTimeout("x"),
            httpx.ConnectError("x"),
            b"not json",
            b"{}",
            b'{"keyCredentials": null}',
        ],
        ids=repr,
    )
    def test_failed_get_means_patch_is_never_called(
        self, monkeypatch: pytest.MonkeyPatch, outcome: Any
    ) -> None:
        graph = use_fake_graph(monkeypatch, FakeGraph(_entries(), get_script=[outcome]))

        with pytest.raises(blueprint_certs.KeyCredentialsReadError):
            _register()

        assert graph.patches == []
        assert graph.entries == _entries()

    def test_failed_get_also_blocks_a_remove_all(self, monkeypatch: pytest.MonkeyPatch) -> None:
        graph = use_fake_graph(monkeypatch, FakeGraph(_entries(), get_script=[503]))

        with pytest.raises(blueprint_certs.KeyCredentialsReadError):
            _register(remove_all_existing=True)

        assert graph.patches == []


class TestRegisterAppend:
    def test_a_new_entry_without_readable_key_material_is_refused_before_any_request(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # Its thumbprint would be None, and would match any unreadable entry on read-back.
        graph = use_fake_graph(monkeypatch, FakeGraph(_entries()))
        unusable = blueprint_certs.KeyCredential(raw={"displayName": "no key"})

        with pytest.raises(ValueError):
            _register(new_entry=unusable)

        assert graph.requests == []

    def test_patch_sends_every_existing_entry_plus_the_new_one(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        graph = use_fake_graph(monkeypatch, FakeGraph(_entries()))
        new_entry = _new_entry()

        result, sleeps = _register(new_entry=new_entry)

        assert graph.patch_bodies == [[*_entries(), new_entry.raw]]
        assert result.thumbprint == CERT_NEW.thumbprint
        assert {c.key_id for c in result.prior} == {e["keyId"] for e in _entries()}
        assert sleeps == []

    def test_patch_targets_v1_not_beta(self, monkeypatch: pytest.MonkeyPatch) -> None:
        graph = use_fake_graph(monkeypatch, FakeGraph(_entries()))
        _register()
        assert graph.patches[0].url.path == f"/v1.0/applications/{FAKE_OBJECT_ID}"
        assert graph.patches[0].headers["authorization"] == f"Bearer {FAKE_TOKEN}"

    def test_every_request_has_a_bounded_timeout(self, monkeypatch: pytest.MonkeyPatch) -> None:
        graph = use_fake_graph(monkeypatch, FakeGraph(_entries()))
        _register()
        assert graph.requests
        for request in graph.requests:
            assert request.extensions["timeout"]["read"] == blueprint_certs.GRAPH_TIMEOUT_SECONDS
            assert request.extensions["timeout"]["connect"] == blueprint_certs.GRAPH_TIMEOUT_SECONDS

    def test_default_client_has_a_finite_timeout(self) -> None:
        with blueprint_certs._new_graph_client() as client:
            assert client.timeout.read == blueprint_certs.GRAPH_TIMEOUT_SECONDS

    def test_two_hosts_with_the_same_display_name_both_survive(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        graph = use_fake_graph(
            monkeypatch,
            FakeGraph(
                [
                    graph_entry(CERT_A, key_id="k-1", display_name=SAME_NAME),
                    graph_entry(CERT_B, key_id="k-2", display_name=SAME_NAME),
                ]
            ),
        )

        _register(new_entry=_new_entry(display_name=SAME_NAME))

        assert [entry["displayName"] for entry in graph.entries] == [SAME_NAME] * 3
        assert {"k-1", "k-2"} <= {entry["keyId"] for entry in graph.entries}

    def test_first_certificate_on_an_empty_blueprint(self, monkeypatch: pytest.MonkeyPatch) -> None:
        graph = use_fake_graph(monkeypatch, FakeGraph([]))
        new_entry = _new_entry()
        _register(new_entry=new_entry)
        assert graph.patch_bodies == [[new_entry.raw]]


class TestRegisterRemoveAll:
    def test_patch_sends_only_the_new_entry(self, monkeypatch: pytest.MonkeyPatch) -> None:
        graph = use_fake_graph(monkeypatch, FakeGraph(_entries()))
        new_entry = _new_entry()

        result, _ = _register(new_entry=new_entry, remove_all_existing=True)

        assert graph.patch_bodies == [[new_entry.raw]]
        assert result.thumbprint == CERT_NEW.thumbprint
        assert [entry["key"] for entry in graph.entries] == [CERT_NEW.b64]

    def test_new_cert_must_still_appear(self, monkeypatch: pytest.MonkeyPatch) -> None:
        use_fake_graph(monkeypatch, FakeGraph(_entries(), stale_reads=1000))
        with pytest.raises(blueprint_certs.RegistrationUnconfirmedError):
            _register(remove_all_existing=True)


class TestVerifyAfterWrite:
    def test_retries_through_replication_lag_then_passes(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        graph = use_fake_graph(monkeypatch, FakeGraph(_entries(), stale_reads=3))

        result, sleeps = _register()

        assert result.thumbprint == CERT_NEW.thumbprint
        assert sleeps == [blueprint_certs.VERIFY_DELAY_SECONDS] * 3
        assert len(graph.gets) == 1 + 4  # the pre-write read, then four verification reads

    def test_the_default_retry_budget_outlasts_the_observed_lag(self) -> None:
        # #124 observed 30-60 s before the new key appeared on read-back.
        budget = (blueprint_certs.VERIFY_ATTEMPTS - 1) * blueprint_certs.VERIFY_DELAY_SECONDS
        assert budget >= 120

    def test_a_read_failure_during_verification_is_retried(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        graph = FakeGraph(_entries())
        use_fake_graph(monkeypatch, graph)
        original = graph._handle_patch

        def patch_then_fail_reads(request: httpx.Request) -> httpx.Response:
            response = original(request)
            graph.get_script.extend([503, httpx.ReadTimeout("x")])
            return response

        monkeypatch.setattr(graph, "_handle_patch", patch_then_fail_reads)

        result, sleeps = _register()

        assert result.thumbprint == CERT_NEW.thumbprint
        assert len(sleeps) == 2

    def test_a_missing_prior_key_id_raises_naming_it_by_display_name(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        entries = _entries()
        use_fake_graph(monkeypatch, FakeGraph(entries, lost_key_ids=[entries[1]["keyId"]]))

        with pytest.raises(blueprint_certs.RegistrationUnconfirmedError) as caught:
            _register(attempts=3)

        assert HOST_B in str(caught.value)
        assert HOST_A not in str(caught.value)
        assert caught.value.missing == (HOST_B,)

    def test_a_kept_cert_given_a_new_key_id_still_counts_as_kept(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # Nothing documents that Graph keeps keyId when key is resent. The certificate
        # surviving is what matters, so a reissued keyId must not be a false alarm.
        graph = use_fake_graph(monkeypatch, FakeGraph(_entries()))
        monkeypatch.setattr(
            graph,
            "_with_key_id",
            lambda entry: {**entry, "keyId": f"reissued-{entry['displayName']}"},
        )

        result, _sleeps = _register(attempts=1)

        assert result.thumbprint == CERT_NEW.thumbprint

    def test_a_new_cert_that_never_appears_raises_naming_it(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        use_fake_graph(monkeypatch, FakeGraph(_entries(), stale_reads=1000))
        new_entry = _new_entry(display_name=HOST_A + " new")
        sleeps: list[float] = []

        with pytest.raises(blueprint_certs.RegistrationUnconfirmedError) as caught:
            blueprint_certs.register_key_credential(
                object_id=FAKE_OBJECT_ID,
                token=FAKE_TOKEN,
                new_entry=new_entry,
                attempts=4,
                delay_seconds=7.0,
                sleep=sleeps.append,
            )

        assert caught.value.missing == (HOST_A + " new",)
        assert sleeps == [7.0, 7.0, 7.0]

    def test_verification_compares_thumbprints_not_display_names(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # Another machine with this same name is already registered: that must
        # not pass for "the new cert arrived".
        same_name_entry = graph_entry(CERT_A, key_id="k-1", display_name=SAME_NAME)
        use_fake_graph(monkeypatch, FakeGraph([same_name_entry], stale_reads=1000))

        with pytest.raises(blueprint_certs.RegistrationUnconfirmedError):
            _register(new_entry=_new_entry(display_name=SAME_NAME), attempts=2)


class TestPatchFailures:
    @pytest.mark.parametrize("status", [400, 401, 403, 404, 409, 429])
    def test_a_4xx_is_a_definite_rejection_with_no_verification(
        self, monkeypatch: pytest.MonkeyPatch, status: int
    ) -> None:
        graph = use_fake_graph(
            monkeypatch, FakeGraph(_entries(), patch_outcome=status, patch_applies=False)
        )

        with pytest.raises(blueprint_certs.RegistrationRejectedError) as caught:
            _register()

        assert caught.value.status == status
        assert f"fake failure {status}" in str(caught.value)
        assert len(graph.gets) == 1

    @pytest.mark.parametrize(
        "outcome",
        [500, 502, 503, 504, httpx.ReadTimeout("x"), httpx.RemoteProtocolError("x")],
        ids=repr,
    )
    def test_an_ambiguous_failure_that_landed_counts_once_verified(
        self, monkeypatch: pytest.MonkeyPatch, outcome: Any
    ) -> None:
        use_fake_graph(monkeypatch, FakeGraph(_entries(), patch_outcome=outcome))
        result, _ = _register()
        assert result.thumbprint == CERT_NEW.thumbprint

    @pytest.mark.parametrize("outcome", [500, 503, httpx.ReadTimeout("x")], ids=repr)
    def test_an_ambiguous_failure_that_cannot_be_confirmed_is_unconfirmed_not_rejected(
        self, monkeypatch: pytest.MonkeyPatch, outcome: Any
    ) -> None:
        # A 5xx or a timeout does not prove nothing was written, so this must
        # not be reported the same way as a definite rejection.
        use_fake_graph(
            monkeypatch, FakeGraph(_entries(), patch_outcome=outcome, patch_applies=False)
        )
        with pytest.raises(blueprint_certs.RegistrationUnconfirmedError):
            _register(attempts=2)


class TestPublicKeyMatching:
    def test_finds_the_credential_for_a_private_key(self) -> None:
        credentials = [blueprint_certs.KeyCredential(raw=entry) for entry in _entries()]
        public_key = blueprint_certs.public_key_of_private_pem(CERT_B.private_key_pem)
        match = blueprint_certs.find_credential_for_public_key(credentials, public_key)
        assert match is not None
        assert match.thumbprint == CERT_B.thumbprint

    def test_no_match_returns_none(self) -> None:
        credentials = [blueprint_certs.KeyCredential(raw=entry) for entry in _entries()]
        public_key = blueprint_certs.public_key_of_private_pem(CERT_NEW.private_key_pem)
        assert blueprint_certs.find_credential_for_public_key(credentials, public_key) is None

    def test_entries_that_are_not_certificates_are_skipped(self) -> None:
        credentials = [blueprint_certs.KeyCredential(raw={"keyId": "x", "key": "bm90LWEtY2VydA=="})]
        public_key = blueprint_certs.public_key_of_private_pem(CERT_B.private_key_pem)
        assert blueprint_certs.find_credential_for_public_key(credentials, public_key) is None

    @staticmethod
    def _match(*entries: dict[str, Any]) -> Any:
        credentials = [blueprint_certs.KeyCredential(raw=entry) for entry in entries]
        public_key = blueprint_certs.public_key_of_private_pem(CERT_B.private_key_pem)
        return blueprint_certs.find_credential_for_public_key(credentials, public_key, now=NOW)

    @pytest.mark.parametrize(
        "change",
        [
            {"endDateTime": "2026-06-30T23:59:59Z"},
            {"startDateTime": "2026-07-01T00:00:01Z"},
            {"usage": "Sign"},
        ],
        ids=["expired", "not-yet-valid", "not-for-verifying"],
    )
    def test_a_certificate_that_cannot_authenticate_now_is_not_a_match(
        self, change: dict[str, str]
    ) -> None:
        assert self._match(graph_entry(CERT_B, key_id="k-1", display_name=HOST_B, **change)) is None

    @pytest.mark.parametrize(
        "tweak",
        [
            lambda entry: entry,
            lambda entry: {k: v for k, v in entry.items() if "DateTime" not in k},
            lambda entry: {**entry, "startDateTime": "garbage", "endDateTime": None},
            lambda entry: {k: v for k, v in entry.items() if k not in ("type", "usage")},
        ],
        ids=["in-date", "no-dates", "unreadable-dates", "no-type-or-usage"],
    )
    def test_a_certificate_matches_unless_its_dates_clearly_rule_it_out(self, tweak: Any) -> None:
        # A date nobody can read must not trigger a needless re-registration.
        match = self._match(tweak(graph_entry(CERT_B, key_id="k-1", display_name=HOST_B)))
        assert match is not None
        assert match.thumbprint == CERT_B.thumbprint

    def test_a_valid_certificate_is_returned_over_an_expired_one_for_the_same_key(self) -> None:
        expired = graph_entry(
            CERT_B, key_id="k-old", display_name=HOST_B, endDateTime="2026-01-01T00:00:00Z"
        )
        valid = graph_entry(CERT_B, key_id="k-new", display_name=HOST_B)

        match = self._match(expired, valid)

        assert match is not None
        assert match.key_id == "k-new"

    def test_unreadable_certificates_are_reported(self) -> None:
        readable = blueprint_certs.KeyCredential(raw=_entries()[0])
        null_key = blueprint_certs.KeyCredential(raw={**_entries()[1], "key": None})
        other_type = blueprint_certs.KeyCredential(
            raw={"keyId": "x", "key": None, "type": "Symmetric"}
        )
        assert blueprint_certs.unreadable_certificates([readable, null_key, other_type]) == [
            null_key
        ]


class TestSwapForWindowsRotation:
    """The callable rotate_cert_windows.rotate drives, forward and on rollback."""

    def _swap(self, *, der_bytes: bytes = CERT_NEW.der, **kwargs: Any) -> int:
        return blueprint_certs.swap_key_credential(
            object_id=FAKE_OBJECT_ID,
            token=FAKE_TOKEN,
            der_bytes=der_bytes,
            old_der=CERT_A.der,
            new_der=CERT_NEW.der,
            display_name=HOST_A,
            **kwargs,
        )

    def test_forward_keeps_other_machines_and_swaps_old_for_new(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        other = graph_entry(CERT_B, key_id="k-other", display_name=HOST_B, extra="preserved")
        graph = use_fake_graph(
            monkeypatch,
            FakeGraph([graph_entry(CERT_A, key_id="k-old", display_name=HOST_A), other]),
        )

        status = self._swap()

        assert status == 204
        assert len(graph.patch_bodies) == 1
        body = graph.patch_bodies[0]
        assert body[0] == other
        assert len(body) == 2
        assert body[1]["key"] == CERT_NEW.b64
        assert body[1]["displayName"] == HOST_A
        assert all(entry["key"] != CERT_A.b64 for entry in body)

    def test_rollback_keeps_other_machines_and_swaps_new_for_old(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        other = graph_entry(CERT_B, key_id="k-other", display_name=HOST_B)
        graph = use_fake_graph(
            monkeypatch,
            FakeGraph([other, graph_entry(CERT_NEW, key_id="k-new", display_name=HOST_A)]),
        )

        status = self._swap(der_bytes=CERT_A.der)

        assert status == 204
        body = graph.patch_bodies[0]
        assert body[0] == other
        assert [entry["key"] for entry in body] == [CERT_B.b64, CERT_A.b64]

    def test_same_display_name_is_not_treated_as_this_machine(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # Only key material decides what is dropped, never the name.
        namesake = graph_entry(CERT_B, key_id="k-namesake", display_name=HOST_A)
        graph = use_fake_graph(
            monkeypatch,
            FakeGraph([graph_entry(CERT_A, key_id="k-old", display_name=HOST_A), namesake]),
        )

        assert self._swap() == 204
        assert graph.patch_bodies[0][0] == namesake

    def test_entries_without_readable_key_material_are_kept(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        opaque = {**graph_entry(CERT_B, key_id="k-opaque", display_name=HOST_B), "key": None}
        graph = use_fake_graph(monkeypatch, FakeGraph([opaque]))

        assert self._swap() == 204
        assert graph.patch_bodies[0][0] == opaque

    @pytest.mark.parametrize("status", [401, 403, 404, 500, 503])
    def test_read_failure_returns_that_status_and_never_patches(
        self, monkeypatch: pytest.MonkeyPatch, status: int
    ) -> None:
        graph = use_fake_graph(monkeypatch, FakeGraph(_entries(), get_script=[status]))
        assert self._swap() == status
        assert graph.patches == []

    @pytest.mark.parametrize(
        "outcome",
        [httpx.ConnectTimeout("x"), b"not json", b"{}", 204],
        ids=repr,
    )
    def test_other_read_failures_return_at_least_400_and_never_patch(
        self, monkeypatch: pytest.MonkeyPatch, outcome: Any
    ) -> None:
        graph = use_fake_graph(monkeypatch, FakeGraph(_entries(), get_script=[outcome]))
        assert self._swap() >= 400
        assert graph.patches == []

    @pytest.mark.parametrize("status", [400, 403, 500, 503])
    def test_patch_failure_returns_its_status(
        self, monkeypatch: pytest.MonkeyPatch, status: int
    ) -> None:
        use_fake_graph(
            monkeypatch, FakeGraph(_entries(), patch_outcome=status, patch_applies=False)
        )
        assert self._swap() == status

    def test_patch_transport_failure_returns_at_least_400_without_raising(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        use_fake_graph(monkeypatch, FakeGraph(_entries(), patch_outcome=httpx.ReadTimeout("x")))
        assert self._swap() >= 400

    def test_a_non_2xx_below_400_still_reports_failure(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        use_fake_graph(monkeypatch, FakeGraph(_entries(), patch_outcome=302, patch_applies=False))
        assert self._swap() >= 400

    def test_unusable_der_returns_at_least_400_and_never_patches(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        graph = use_fake_graph(monkeypatch, FakeGraph(_entries()))
        assert self._swap(der_bytes=b"not a certificate") >= 400
        assert graph.patches == []

    def test_failure_output_carries_no_token_or_key_material(
        self, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
    ) -> None:
        use_fake_graph(monkeypatch, FakeGraph(_entries(), get_script=[403]))
        self._swap()
        captured = capsys.readouterr()
        assert FAKE_TOKEN not in captured.out + captured.err
        assert CERT_NEW.b64 not in captured.out + captured.err
        assert "403" in captured.err


class TestSwapDrivenByRotate:
    """The real rotate() contract with the real helper, on a fake Graph."""

    def _state(self, tmp_path: Path) -> Any:
        env = tmp_path / ".env"
        env.write_text(
            "ENTRABOT_BLUEPRINT_CERT_THUMBPRINT=ORIG_X5T\n"
            "ENTRABOT_BLUEPRINT_CERT_SHA1=" + "0" * 40 + "\n"
        )
        msal = tmp_path / ".msal-cache.bin"
        msal.write_bytes(b"old-cache")
        return rotate_cert_windows.RotationState(
            env_path=env, msal_cache_path=msal, blueprint_object_id=FAKE_OBJECT_ID
        )

    def _rotate(self, state: Any, *, smoke_passes: bool, deleted: list[str]) -> None:
        def graph_patch(*, token: str, der_bytes: bytes) -> int:
            return blueprint_certs.swap_key_credential(
                object_id=FAKE_OBJECT_ID,
                token=token,
                der_bytes=der_bytes,
                old_der=CERT_A.der,
                new_der=CERT_NEW.der,
                display_name=HOST_A,
            )

        rotate_cert_windows.rotate(
            state=state,
            old_der=CERT_A.der,
            new_thumbprint="A" * 40,
            new_x5t_s256="NEW_X5T",
            new_der=CERT_NEW.der,
            graph_patch=graph_patch,
            smoke_test=lambda: smoke_passes,
            delete_old_cert=deleted.append,
            graph_token_provider=lambda: FAKE_TOKEN,
        )

    def _graph(self) -> FakeGraph:
        return FakeGraph(
            [
                graph_entry(CERT_A, key_id="k-old", display_name=HOST_A),
                graph_entry(CERT_B, key_id="k-other", display_name=HOST_B),
            ]
        )

    def test_successful_rotation_keeps_the_other_machine(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        graph = use_fake_graph(monkeypatch, self._graph())
        deleted: list[str] = []

        self._rotate(self._state(tmp_path), smoke_passes=True, deleted=deleted)

        assert {entry["key"] for entry in graph.entries} == {CERT_B.b64, CERT_NEW.b64}
        assert deleted == ["0" * 40]

    def test_rollback_restores_old_and_keeps_the_other_machine(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        graph = use_fake_graph(monkeypatch, self._graph())
        deleted: list[str] = []
        state = self._state(tmp_path)

        with pytest.raises(rotate_cert_windows.RotationRolledBack):
            self._rotate(state, smoke_passes=False, deleted=deleted)

        assert {entry["key"] for entry in graph.entries} == {CERT_A.b64, CERT_B.b64}
        assert len(graph.patches) == 2
        assert deleted == []
        assert "ENTRABOT_BLUEPRINT_CERT_THUMBPRINT=ORIG_X5T" in state.env_path.read_text()

    def test_read_failure_surfaces_as_rotation_failed_with_nothing_written(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        graph = self._graph()
        graph.get_script.append(503)
        use_fake_graph(monkeypatch, graph)
        deleted: list[str] = []
        state = self._state(tmp_path)

        with pytest.raises(rotate_cert_windows.RotationFailed):
            self._rotate(state, smoke_passes=True, deleted=deleted)

        assert graph.patches == []
        assert deleted == []
        assert "ENTRABOT_BLUEPRINT_CERT_THUMBPRINT=ORIG_X5T" in state.env_path.read_text()

    def test_rollback_read_failure_surfaces_as_manual_intervention(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        graph = use_fake_graph(monkeypatch, self._graph())
        state = self._state(tmp_path)
        real_smoke_fails_and_breaks_reads: list[int] = []

        def graph_patch(*, token: str, der_bytes: bytes) -> int:
            status = blueprint_certs.swap_key_credential(
                object_id=FAKE_OBJECT_ID,
                token=token,
                der_bytes=der_bytes,
                old_der=CERT_A.der,
                new_der=CERT_NEW.der,
                display_name=HOST_A,
            )
            graph.get_script.append(503)
            real_smoke_fails_and_breaks_reads.append(status)
            return status

        with pytest.raises(rotate_cert_windows.ManualInterventionRequired):
            rotate_cert_windows.rotate(
                state=state,
                old_der=CERT_A.der,
                new_thumbprint="A" * 40,
                new_x5t_s256="NEW_X5T",
                new_der=CERT_NEW.der,
                graph_patch=graph_patch,
                smoke_test=lambda: False,
                delete_old_cert=lambda _sha1: None,
                graph_token_provider=lambda: FAKE_TOKEN,
            )

        assert real_smoke_fails_and_breaks_reads == [204, 503]


def test_patch_body_is_json_serializable_round_trip(monkeypatch: pytest.MonkeyPatch) -> None:
    # Guards the wire format: what we sent parses back to exactly what we built.
    graph = use_fake_graph(monkeypatch, FakeGraph(_entries()))
    new_entry = _new_entry()
    _register(new_entry=new_entry)
    assert json.loads(graph.patches[0].content) == {"keyCredentials": [*_entries(), new_entry.raw]}
