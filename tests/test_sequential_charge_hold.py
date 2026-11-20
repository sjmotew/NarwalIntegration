"""Captured sequential-clean family4 recharge and departure."""

import importlib

import pytest
import tests.ha_stubs

tests.ha_stubs.install()


@pytest.mark.parametrize("prefix", ["narwal_client", "custom_components.narwal.narwal_client"])
def test_sequential_task_holds_charge_until_actual_departure(prefix):
    module = importlib.import_module(prefix + ".client")
    client = module.NarwalClient("127.0.0.1")
    client._update_from_base_status_broadcast({"3": {"1": 4, "7": 7}, "11": 1, "47": 2, "26": 5})
    assert client.state.is_cleaning
    client._update_from_base_status_broadcast(
        {"3": {"1": 4, "7": 6, "11": 3}, "11": 3, "47": 1, "26": 5}
    )
    assert client.state.is_charging_to_resume
    assert not client.state.is_cleaning
    client._update_from_base_status_broadcast({"3": {"1": 4, "7": 6, "11": 3}})
    assert client.state.is_charging_to_resume
    client._update_from_base_status_broadcast({"3": {"1": 4, "7": 7}, "11": 1, "47": 2})
    assert client.state.is_cleaning
    assert not client.state.is_charging_to_resume


@pytest.mark.parametrize("prefix", ["narwal_client", "custom_components.narwal.narwal_client"])
def test_legacy_clean_without_sequential_phase_keeps_legacy_behavior(prefix):
    client = importlib.import_module(prefix + ".client").NarwalClient("127.0.0.1")
    client._update_from_base_status_broadcast({"3": {"1": 4}, "11": 1, "47": 2})
    assert client.state.is_cleaning
    assert not client.state.has_native_clean_task


@pytest.mark.parametrize("prefix", ["narwal_client", "custom_components.narwal.narwal_client"])
def test_partial_legacy_flag_cannot_release_an_established_charge_hold(prefix):
    client = importlib.import_module(prefix + ".client").NarwalClient("127.0.0.1")
    client._update_from_base_status_broadcast({"3": {"1": 4, "7": 6, "11": 3}, "11": 3, "47": 1})
    client._update_from_base_status_broadcast({"3": {"1": 4, "7": 1}})
    assert client.state.has_native_clean_task
    assert client.state.clean_task_phase.value == "unknown"
    assert not client.state.is_cleaning


@pytest.mark.parametrize("prefix", ["narwal_client", "custom_components.narwal.narwal_client"])
def test_captured_sequential_charge_approach(prefix):
    client = importlib.import_module(prefix + ".client").NarwalClient("127.0.0.1")
    for stage in (1, 2):
        client._update_from_base_status_broadcast(
            {"3": {"1": 4, "7": 6, "11": stage}, "11": 1, "47": 2}
        )
        assert client.state.clean_task_phase.value == "returning_to_charge"
        assert not client.state.is_charging_to_resume
    client._update_from_base_status_broadcast({"3": {"1": 4, "7": 6, "11": 3}, "11": 3, "47": 1})
    assert client.state.is_charging_to_resume


@pytest.mark.parametrize("prefix", ["narwal_client", "custom_components.narwal.narwal_client"])
@pytest.mark.parametrize("intermediate", [False, True])
def test_partial_clean_phase_needs_current_departure_evidence(prefix, intermediate):
    client = importlib.import_module(prefix + ".client").NarwalClient("127.0.0.1")
    client._update_from_base_status_broadcast({"3": {"1": 4, "7": 6, "11": 3}, "11": 3, "47": 1})
    if intermediate:
        client._update_from_base_status_broadcast({"3": {"1": 4, "7": 1}})
    client._update_from_base_status_broadcast({"3": {"1": 4, "7": 7}})
    assert client.state.has_native_clean_task
    assert not client.state.is_cleaning
    client._update_from_base_status_broadcast({"3": {"1": 4, "7": 7}, "11": 1, "47": 2})
    assert client.state.is_cleaning


@pytest.mark.parametrize("prefix", ["narwal_client", "custom_components.narwal.narwal_client"])
def test_contact_only_packets_resolve_pending_charge_and_departure(prefix):
    client = importlib.import_module(prefix + ".client").NarwalClient("127.0.0.1")
    client._update_from_base_status_broadcast({"3": {"1": 4, "7": 6, "11": 3}})
    assert client.state.clean_task_phase.value == "unknown"
    client._update_from_base_status_broadcast({"11": 3, "47": 1})
    assert client.state.is_charging_to_resume
    client._update_from_base_status_broadcast({"3": {"1": 4, "7": 7}})
    assert not client.state.is_cleaning
    client._update_from_base_status_broadcast({"11": 1, "47": 2})
    assert client.state.is_cleaning


@pytest.mark.parametrize("prefix", ["narwal_client", "custom_components.narwal.narwal_client"])
def test_unproven_switch_does_not_use_initial_legacy_detection(prefix):
    client = importlib.import_module(prefix + ".client").NarwalClient("127.0.0.1")
    client._update_from_base_status_broadcast({"3": {"1": 17, "17": 7}, "11": 1, "47": 2})
    client._update_from_base_status_broadcast({"3": {"1": 4, "7": 1}, "11": 1, "47": 2})
    assert client.state.has_native_clean_task
    assert client.state.clean_task_phase.value == "unknown"


@pytest.mark.parametrize("prefix", ["narwal_client", "custom_components.narwal.narwal_client"])
@pytest.mark.parametrize("departure", [False, True])
def test_expired_phase_is_not_revived_by_contact_only(prefix, departure, monkeypatch):
    from types import SimpleNamespace

    models = importlib.import_module(prefix + ".models")
    clock = [100.0]
    monkeypatch.setattr(models, "time", SimpleNamespace(monotonic=lambda: clock[0]))
    client = importlib.import_module(prefix + ".client").NarwalClient("127.0.0.1")
    if departure:
        client._update_from_base_status_broadcast(
            {"3": {"1": 4, "7": 6, "11": 3}, "11": 3, "47": 1}
        )
        client._update_from_base_status_broadcast({"3": {"1": 4, "7": 7}})
    else:
        client._update_from_base_status_broadcast({"3": {"1": 4, "7": 6, "11": 3}})
    clock[0] = 116.0
    client._update_from_base_status_broadcast(
        {"11": 1 if departure else 3, "47": 2 if departure else 1}
    )
    assert client.state.has_native_clean_task
    assert client.state.clean_task_phase.value == "unknown"
    assert not client.state.is_cleaning
    assert client.state.native_clean_broadcast_time == 100.0


@pytest.mark.parametrize("prefix", ["narwal_client", "custom_components.narwal.narwal_client"])
def test_other_native_task_to_legacy_without_phase(prefix):
    client = importlib.import_module(prefix + ".client").NarwalClient("127.0.0.1")
    client._update_from_base_status_broadcast({"3": {"1": 17, "17": 7}, "11": 1, "47": 2})
    client._update_from_base_status_broadcast({"3": {"1": 4}, "11": 1, "47": 2})
    assert client.state.has_native_clean_task
    assert client.state.clean_task_phase.value == "unknown"
    assert not client.state.is_cleaning


@pytest.mark.parametrize("prefix", ["narwal_client", "custom_components.narwal.narwal_client"])
def test_expired_sequential_task_accepts_explicit_idle(prefix, monkeypatch):
    from types import SimpleNamespace

    models = importlib.import_module(prefix + ".models")
    clock = [100.0]
    monkeypatch.setattr(models, "time", SimpleNamespace(monotonic=lambda: clock[0]))
    client = importlib.import_module(prefix + ".client").NarwalClient("127.0.0.1")
    client._update_from_base_status_broadcast({"3": {"1": 4, "7": 6, "11": 3}, "11": 3, "47": 1})
    clock[0] = 116.0
    client._update_from_base_status_broadcast({"3": {"1": 1, "3": 1}, "11": 3, "47": 1})
    assert not client.state.has_native_clean_task
    assert not client.state.is_cleaning


@pytest.mark.parametrize("prefix", ["narwal_client", "custom_components.narwal.narwal_client"])
def test_expired_departure_cannot_be_revived_by_charge_stage(prefix, monkeypatch):
    from types import SimpleNamespace

    models = importlib.import_module(prefix + ".models")
    clock = [100.0]
    monkeypatch.setattr(models, "time", SimpleNamespace(monotonic=lambda: clock[0]))
    client = importlib.import_module(prefix + ".client").NarwalClient("127.0.0.1")
    client._update_from_base_status_broadcast({"3": {"1": 4, "7": 6, "11": 3}, "11": 3, "47": 1})
    client._update_from_base_status_broadcast({"3": {"1": 4, "7": 7}})
    clock[0] = 116.0
    client._update_from_base_status_broadcast({"3": {"1": 4, "11": 3}, "11": 1, "47": 2})
    assert client.state.clean_task_phase.value == "unknown"
    assert not client.state.is_cleaning
    assert client.state.native_clean_broadcast_time == 100.0


@pytest.mark.parametrize("prefix", ["narwal_client", "custom_components.narwal.narwal_client"])
def test_expired_clean_phase_requires_new_departure_evidence(prefix, monkeypatch):
    from types import SimpleNamespace

    models = importlib.import_module(prefix + ".models")
    clock = [100.0]
    monkeypatch.setattr(models, "time", SimpleNamespace(monotonic=lambda: clock[0]))
    client = importlib.import_module(prefix + ".client").NarwalClient("127.0.0.1")
    client._update_from_base_status_broadcast({"3": {"1": 4, "7": 7}, "11": 1, "47": 2})
    assert client.state.is_cleaning
    clock[0] = 116.0
    client._update_from_base_status_broadcast({"3": {"1": 4, "7": 7}})
    assert client.state.clean_task_phase.value == "unknown"
    assert not client.state.is_cleaning
    client._update_from_base_status_broadcast({"11": 1, "47": 2})
    assert client.state.is_cleaning


@pytest.mark.parametrize("prefix", ["narwal_client", "custom_components.narwal.narwal_client"])
def test_initial_sequential_clean_waits_for_separate_contact(prefix):
    client = importlib.import_module(prefix + ".client").NarwalClient("127.0.0.1")
    client._update_from_base_status_broadcast({"3": {"1": 4, "7": 7}})
    assert client.state.has_native_clean_task
    assert client.state.clean_task_phase.value == "unknown"
    assert not client.state.is_cleaning
    client._update_from_base_status_broadcast({"11": 1, "47": 2})
    assert client.state.is_cleaning


@pytest.mark.parametrize("prefix", ["narwal_client", "custom_components.narwal.narwal_client"])
def test_phase7_docked_preserves_charge_until_departure(prefix):
    client = importlib.import_module(prefix + ".client").NarwalClient("127.0.0.1")
    client._update_from_base_status_broadcast({"3": {"1": 4, "7": 6, "11": 3}, "11": 3, "47": 1})
    client._update_from_base_status_broadcast({"3": {"1": 4, "7": 7}, "11": 3, "47": 1})
    assert client.state.is_charging_to_resume
    assert not client.state.is_cleaning
    client._update_from_base_status_broadcast({"11": 1, "47": 2})
    assert client.state.is_cleaning
