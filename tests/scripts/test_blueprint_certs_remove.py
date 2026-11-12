"""Tests for ``blueprint_certs.remove_key_credential``.

Removing one machine's certificate is the same whole-collection PATCH that adding
one is (#124), so the same three rules apply: a failed read is never an empty list,
every other entry goes back exactly as Graph returned it, and the write isn't done
until it has been read back.
"""

from __future__ import annotations

import hashlib
from typing import Any

import httpx
import pytest

from tests.scripts._blueprint_cert_helpers import (
    FAKE_OBJECT_ID,
    FAKE_TOKEN,
    FakeGraph,
    block_real_keyring_and_network,
    blueprint_certs,
    graph_entry,
    make_cert,
    use_fake_graph,
)

CERT_A = make_cert("host-a")
CERT_B = make_cert("host-b")
CERT_C = make_cert("host-c")
KEY_A = "00000000-0000-0000-0000-00000000000a"
KEY_B = "00000000-0000-0000-0000-00000000000b"
KEY_C = "00000000-0000-0000-0000-00000000000c"


@pytest.fixture(autouse=True)
def _no_real_keychain_or_network(monkeypatch: pytest.MonkeyPatch) -> None:
    block_real_keyring_and_network(monkeypatch)


def _entries() -> list[dict[str, Any]]:
    return [
        graph_entry(CERT_A, key_id=KEY_A, display_name="host-a", extraField="kept-as-is"),
        graph_entry(CERT_B, key_id=KEY_B, display_name="host-b"),
        graph_entry(CERT_C, key_id=KEY_C, display_name="host-c"),
    ]


def _sha1(cert: Any) -> str:
    return hashlib.sha1(cert.der).hexdigest().upper()  # noqa: S324


def _remove(**kwargs: Any) -> tuple[Any, list[float]]:
    sleeps: list[float] = []
    result = blueprint_certs.remove_key_credential(
        object_id=FAKE_OBJECT_ID, token=FAKE_TOKEN, sleep=sleeps.append, **kwargs
    )
    return result, sleeps


class TestRemoveKeyCredential:
    def test_removes_exactly_that_certificate_and_sends_the_rest_back_untouched(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        graph = use_fake_graph(monkeypatch, FakeGraph(_entries()))

        result, sleeps = _remove(x5t_s256=CERT_B.thumbprint)

        assert [c.key_id for c in result.removed] == [KEY_B]
        assert [c.key_id for c in result.current] == [KEY_A, KEY_C]
        assert graph.patch_bodies == [[_entries()[0], _entries()[2]]]
        assert sleeps == []

    def test_matches_by_sha1_in_either_case(self, monkeypatch: pytest.MonkeyPatch) -> None:
        graph = use_fake_graph(monkeypatch, FakeGraph(_entries()))

        result, _ = _remove(sha1_hex=_sha1(CERT_C).lower())

        assert [c.key_id for c in result.removed] == [KEY_C]
        assert [entry["keyId"] for entry in graph.entries] == [KEY_A, KEY_B]

    def test_a_certificate_that_is_not_registered_is_a_no_op_without_a_patch(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        graph = use_fake_graph(monkeypatch, FakeGraph(_entries()[:2]))

        result, _ = _remove(x5t_s256=CERT_C.thumbprint)

        assert result.removed == ()
        assert [c.key_id for c in result.current] == [KEY_A, KEY_B]
        assert graph.patches == []

    def test_every_entry_holding_that_certificate_goes(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        entries = _entries() + [
            graph_entry(CERT_B, key_id="00000000-0000-0000-0000-0000000000bb", display_name="dup")
        ]
        graph = use_fake_graph(monkeypatch, FakeGraph(entries))

        result, _ = _remove(x5t_s256=CERT_B.thumbprint)

        assert len(result.removed) == 2
        assert [entry["keyId"] for entry in graph.entries] == [KEY_A, KEY_C]

    def test_the_last_certificate_can_be_removed(self, monkeypatch: pytest.MonkeyPatch) -> None:
        graph = use_fake_graph(monkeypatch, FakeGraph(_entries()[:1]))

        result, _ = _remove(x5t_s256=CERT_A.thumbprint)

        assert graph.patch_bodies == [[]]
        assert result.current == ()

    def test_needs_a_thumbprint(self, monkeypatch: pytest.MonkeyPatch) -> None:
        graph = use_fake_graph(monkeypatch, FakeGraph(_entries()))
        with pytest.raises(ValueError, match="thumbprint"):
            _remove()
        assert graph.requests == []

    @pytest.mark.parametrize("status", [401, 403, 404, 500, 503])
    def test_a_failed_read_sends_nothing(
        self, monkeypatch: pytest.MonkeyPatch, status: int
    ) -> None:
        graph = use_fake_graph(monkeypatch, FakeGraph(_entries(), get_script=[status]))
        with pytest.raises(blueprint_certs.KeyCredentialsReadError):
            _remove(x5t_s256=CERT_B.thumbprint)
        assert graph.patches == []

    def test_no_match_but_an_unreadable_certificate_is_could_not_check(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        entries = _entries()[:1] + [
            {**graph_entry(CERT_B, key_id=KEY_B, display_name="host-b"), "key": None}
        ]
        graph = use_fake_graph(monkeypatch, FakeGraph(entries))
        with pytest.raises(blueprint_certs.KeyCredentialsReadError, match="couldn't be read"):
            _remove(x5t_s256=CERT_B.thumbprint)
        assert graph.patches == []

    def test_an_unreadable_entry_is_kept_when_the_target_is_found(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        unreadable = {**graph_entry(CERT_C, key_id=KEY_C, display_name="host-c"), "key": None}
        graph = use_fake_graph(monkeypatch, FakeGraph(_entries()[:2] + [unreadable]))

        result, _ = _remove(x5t_s256=CERT_B.thumbprint)

        assert [c.key_id for c in result.removed] == [KEY_B]
        assert [entry["keyId"] for entry in graph.entries] == [KEY_A, KEY_C]

    @pytest.mark.parametrize("status", [400, 401, 403, 404, 409, 429])
    def test_a_4xx_is_a_definite_rejection(
        self, monkeypatch: pytest.MonkeyPatch, status: int
    ) -> None:
        graph = use_fake_graph(
            monkeypatch, FakeGraph(_entries(), patch_outcome=status, patch_applies=False)
        )
        with pytest.raises(blueprint_certs.RegistrationRejectedError) as caught:
            _remove(x5t_s256=CERT_B.thumbprint)
        assert caught.value.status == status
        assert len(graph.gets) == 1

    def test_waits_for_the_removal_to_become_visible(self, monkeypatch: pytest.MonkeyPatch) -> None:
        use_fake_graph(monkeypatch, FakeGraph(_entries(), stale_reads=2))

        result, sleeps = _remove(x5t_s256=CERT_B.thumbprint, delay_seconds=7.0)

        assert [c.key_id for c in result.current] == [KEY_A, KEY_C]
        assert sleeps == [7.0, 7.0]

    @pytest.mark.parametrize("outcome", [500, 503, httpx.ReadTimeout("x")], ids=repr)
    def test_an_ambiguous_failure_that_landed_counts_once_verified(
        self, monkeypatch: pytest.MonkeyPatch, outcome: Any
    ) -> None:
        use_fake_graph(monkeypatch, FakeGraph(_entries(), patch_outcome=outcome))
        result, _ = _remove(x5t_s256=CERT_B.thumbprint)
        assert [c.key_id for c in result.removed] == [KEY_B]

    def test_still_listed_after_every_read_is_unconfirmed(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        use_fake_graph(monkeypatch, FakeGraph(_entries(), patch_outcome=503, patch_applies=False))
        with pytest.raises(blueprint_certs.RegistrationUnconfirmedError, match="still listed"):
            _remove(x5t_s256=CERT_B.thumbprint, attempts=2)

    def test_another_machines_certificate_vanishing_is_unconfirmed_and_named(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        use_fake_graph(monkeypatch, FakeGraph(_entries(), lost_key_ids=[KEY_C]))
        with pytest.raises(blueprint_certs.RegistrationUnconfirmedError) as caught:
            _remove(x5t_s256=CERT_B.thumbprint, attempts=2)
        assert caught.value.missing == ("host-c",)

    def test_errors_carry_no_token_or_key_material(self, monkeypatch: pytest.MonkeyPatch) -> None:
        use_fake_graph(monkeypatch, FakeGraph(_entries(), patch_outcome=503, patch_applies=False))
        with pytest.raises(blueprint_certs.RegistrationUnconfirmedError) as caught:
            _remove(x5t_s256=CERT_B.thumbprint, attempts=1)
        text = str(caught.value)
        assert FAKE_TOKEN not in text and CERT_B.b64 not in text
        assert CERT_B.thumbprint not in text


def _stale_body(entries: list[dict[str, Any]]) -> bytes:
    """A 200 answer from a replica that has not caught up: an older list."""
    import json

    return json.dumps({"keyCredentials": entries}).encode()


class TestMustKeep:
    """Removal replaces the whole list with what its own read returned. Graph can take a
    minute to show a certificate that was just added, so a read that lands on a replica
    still missing it would write the list back WITHOUT it: the removal "succeeds" and the
    newly added certificate is gone too. A caller that has just added one names it in
    ``must_keep``, and nothing is written from a list that does not show it.
    """

    def test_a_listed_certificate_that_must_stay_changes_nothing_about_a_normal_removal(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        graph = use_fake_graph(monkeypatch, FakeGraph(_entries()))

        result, sleeps = _remove(x5t_s256=CERT_B.thumbprint, must_keep=[CERT_C.thumbprint])

        assert [c.key_id for c in result.removed] == [KEY_B]
        assert graph.patch_bodies == [[_entries()[0], _entries()[2]]]
        assert sleeps == []

    @pytest.mark.parametrize("form", ["x5t_s256", "sha1_upper", "sha1_lower"])
    def test_it_is_named_by_either_thumbprint(
        self, monkeypatch: pytest.MonkeyPatch, form: str
    ) -> None:
        use_fake_graph(monkeypatch, FakeGraph(_entries()))
        keep = {
            "x5t_s256": CERT_C.thumbprint,
            "sha1_upper": _sha1(CERT_C),
            "sha1_lower": _sha1(CERT_C).lower(),
        }[form]

        result, _ = _remove(sha1_hex=_sha1(CERT_B), must_keep=[keep])

        assert [c.key_id for c in result.current] == [KEY_A, KEY_C]

    def test_nothing_is_written_from_a_list_that_does_not_show_it(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # The replica never catches up: A and B are listed, the just-added C is not.
        graph = use_fake_graph(monkeypatch, FakeGraph(_entries()[:2]))

        with pytest.raises(blueprint_certs.RequiredCertificateMissingError) as caught:
            _remove(x5t_s256=CERT_B.thumbprint, must_keep=[CERT_C.thumbprint], attempts=3)

        assert graph.patches == []
        assert len(graph.gets) == 3  # it waited for the list to catch up, a bounded time
        assert "Nothing was changed" in str(caught.value)
        assert CERT_C.thumbprint not in str(caught.value)

    def test_that_refusal_is_a_read_problem_so_callers_know_nothing_was_sent(self) -> None:
        assert issubclass(
            blueprint_certs.RequiredCertificateMissingError,
            blueprint_certs.KeyCredentialsReadError,
        )

    def test_it_waits_for_a_stale_read_to_catch_up_then_removes(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        stale = _stale_body(_entries()[:2])
        graph = use_fake_graph(monkeypatch, FakeGraph(_entries(), get_script=[stale, stale]))

        result, sleeps = _remove(
            x5t_s256=CERT_B.thumbprint, must_keep=[CERT_C.thumbprint], delay_seconds=7.0
        )

        assert sleeps == [7.0, 7.0]
        # Written from the read that showed all three: C goes back, only B is dropped.
        assert graph.patch_bodies == [[_entries()[0], _entries()[2]]]
        assert [c.key_id for c in result.current] == [KEY_A, KEY_C]

    def test_without_must_keep_a_stale_read_would_have_dropped_it(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # The defect, pinned: this is what the caller is protected from.
        stale = _stale_body(_entries()[:2])
        graph = use_fake_graph(monkeypatch, FakeGraph(_entries(), get_script=[stale]))

        _remove(x5t_s256=CERT_B.thumbprint)

        assert graph.patch_bodies == [[_entries()[0]]]  # C was not sent back
        assert [entry["keyId"] for entry in graph.entries] == [KEY_A]

    def test_absent_afterwards_is_unconfirmed_never_a_success(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        use_fake_graph(monkeypatch, FakeGraph(_entries(), lost_key_ids=[KEY_C]))

        with pytest.raises(blueprint_certs.RegistrationUnconfirmedError) as caught:
            _remove(x5t_s256=CERT_B.thumbprint, must_keep=[CERT_C.thumbprint], attempts=2)

        assert "must stay" in str(caught.value)
        assert caught.value.missing == ("host-c",)

    def test_it_is_checked_even_when_the_certificate_to_remove_is_not_listed(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # A list showing neither is not evidence that the removal already happened.
        graph = use_fake_graph(monkeypatch, FakeGraph(_entries()[:1]))

        with pytest.raises(blueprint_certs.RequiredCertificateMissingError):
            _remove(x5t_s256=CERT_B.thumbprint, must_keep=[CERT_C.thumbprint], attempts=1)

        assert graph.patches == []

    def test_a_certificate_cannot_be_both_removed_and_kept(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        graph = use_fake_graph(monkeypatch, FakeGraph(_entries()))

        with pytest.raises(ValueError, match="must_keep"):
            _remove(sha1_hex=_sha1(CERT_B), must_keep=[_sha1(CERT_B).lower()])

        assert graph.requests == []

    def test_nor_when_it_is_named_by_its_other_thumbprint(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # Only the certificate itself ties its two thumbprints together, so this one is
        # caught once the list has been read, and still before anything is written.
        graph = use_fake_graph(monkeypatch, FakeGraph(_entries()))

        with pytest.raises(ValueError, match="must_keep"):
            _remove(x5t_s256=CERT_B.thumbprint, must_keep=[_sha1(CERT_B)])

        assert graph.patches == []

    def test_a_failed_read_is_still_never_an_empty_list(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        graph = use_fake_graph(monkeypatch, FakeGraph(_entries(), get_script=[503]))

        with pytest.raises(blueprint_certs.KeyCredentialsReadError):
            _remove(x5t_s256=CERT_B.thumbprint, must_keep=[CERT_C.thumbprint])

        assert graph.patches == []
