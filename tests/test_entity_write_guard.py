"""Entities write their state only when something they publish has changed."""

from types import SimpleNamespace

from custom_components.bermuda.entity import BermudaEntity, BermudaGlobalEntity, write_if_changed


class _Published:
    """The surface write_if_changed reads, with a write counter."""

    def __init__(self):
        self.bermuda_last_published = None
        self.writes = 0
        self.available = True
        self.state = "kitchen"
        self.icon = "mdi:map-marker"
        self.extra_state_attributes = {"area_id": "kitchen"}

    def async_write_ha_state(self):
        self.writes += 1


def test_write_if_changed_publishes_the_first_time_and_on_any_change():
    entity = _Published()
    assert write_if_changed(entity) is True
    assert write_if_changed(entity) is False
    assert write_if_changed(entity) is False
    entity.state = "hall"
    assert write_if_changed(entity) is True
    entity.extra_state_attributes["area_id"] = "hall"  # mutated in place: the stored shape is a copy
    assert write_if_changed(entity) is True
    entity.icon = "mdi:bluetooth-off"
    assert write_if_changed(entity) is True
    entity.available = False
    assert write_if_changed(entity) is True
    entity.extra_state_attributes = None
    assert write_if_changed(entity) is True
    assert write_if_changed(entity) is False
    assert entity.writes == 6


def _bermuda_entity(cls):
    """A BermudaEntity without HA: the attributes _handle_coordinator_update touches."""
    entity = cls.__new__(cls)
    entity.coordinator = SimpleNamespace(last_update_success=True)
    entity.devreg_init_done = True
    entity._device = SimpleNamespace(name="thing")
    entity._lastname = "thing"
    entity.bermuda_last_published = None
    entity.writes = []
    entity.async_write_ha_state = lambda: entity.writes.append(entity.state)
    return entity


def test_handle_coordinator_update_skips_an_unchanged_cycle():
    entity = _bermuda_entity(BermudaEntity)
    entity._attr_state = "kitchen"
    for _ in range(3):
        entity._handle_coordinator_update()
    entity._attr_state = "hall"
    entity._handle_coordinator_update()
    entity._handle_coordinator_update()
    assert entity.writes == ["kitchen", "hall"]


def test_global_entity_skips_an_unchanged_cycle_too():
    entity = _bermuda_entity(BermudaGlobalEntity)
    entity._attr_state = 12
    entity._handle_coordinator_update()
    entity._handle_coordinator_update()
    entity._attr_state = 13
    entity._handle_coordinator_update()
    assert entity.writes == [12, 13]
